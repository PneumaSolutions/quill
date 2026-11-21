# Security review: Scribe for Documents and scanning in QUILL

Reviewed 2026-11-21 on the `scribe-documents` branch, before it was pushed.
Scope: the code this branch adds, `quill/core/scribe/` (sign-in, REST client,
document channel, queue and runner), the Scribe windows in `quill/ui/`, and
`quill/core/scanning/` with its `pagescan` subprocess backend. Each check below
says what was found and what was changed. Every fix has a unit test.

## Summary

Five things were fixed in this review:

1. The sign-in now sends and checks an OAuth `state`. A redirect with a wrong
   or missing `state` is refused and the wait goes on.
2. QUILL no longer searches `PATH` for `pagescan`. A configured path must be
   absolute, and QUILL's bundled copy now wins over the environment variable.
3. QUILL only takes scanned pages that are inside the folder it gave
   `pagescan`.
4. A file name sent by the server can no longer be a Windows device name
   (`CON`, `NUL`, `COM1` and so on). Path traversal was already blocked.
5. Convert from a Link now checks the link in core code: `http` or `https`
   with a host. Before, only the dialog checked it, and only the prefix.

The document channel now also caps the size of a WebSocket message at 16 MB;
before, there was no limit.

Nothing that needs a server change was found. Two risks are accepted and
explained at the end.

An independent review later the same day found more, in reliability as well as
security; each finding and what was done about it is in "Independent review"
at the end. Where that changed something described below, the text below says
so.

## Tokens

- **Where they live.** `auth.TokenStore` keeps the access token, the refresh
  token and the expiry as one JSON value in the OS secret vault through
  `SecretsManager`, under the namespace `scribe` (Windows Credential Manager,
  the macOS Keychain). They never go in settings, the queue file or any other
  file. Sign Out revokes the refresh token (RFC 7009) and wipes the namespace.
- **Logging.** The `quill.core.scribe` package logs seven fixed sentences. None
  of them has a token, a URL with a query string, a password or a file's
  contents. The loopback listener's `log_message` is silenced, because the
  request line carries the authorization code. The `websockets` library logs
  the request line (with `?token=`) and frames at DEBUG, so the channel gives it
  its own logger that never goes below WARNING.
- **Errors.** Error sentences are built from the server's own message and never
  from the request. Crash bundles also go through `quill/stability/redaction.py`,
  which removes `token=` and `password=` values and long token-shaped strings.

## The loopback sign-in listener

- **Address.** It binds to `127.0.0.1` only, on a free port, never `0.0.0.0`.
  It does not bind `[::1]`: the redirect QUILL sends is always
  `http://127.0.0.1:<port>/callback`, so the IPv6 loopback is never needed, and
  one address is less to defend. On Windows the socket is opened with
  `SO_EXCLUSIVEADDRUSE`, so another program cannot bind the same port.
- **PKCE.** Every sign-in makes a fresh S256 verifier and challenge
  (`quill.core.auth.pkce`). The code is exchanged with the verifier, so a code
  that was stolen or planted cannot be used without it.
- **State (fixed).** There was no `state` parameter. PKCE already stopped a
  planted code from being exchanged, but any program on the computer could call
  the listener first with `?error=...` or a bad code and end the sign-in. Now
  `SignInFlow` makes a 32-byte random `state` (`secrets.token_urlsafe`) and
  sends it with the authorize request. The listener compares it in constant time
  (`hmac.compare_digest`). If it is wrong or missing, the listener answers 400
  and keeps waiting for the real redirect. Scribe's OAuth server
  (ex_oauth2_provider, `Response.add_state`) sends `state` back on success and
  on error, so nothing changes for the person signing in. Test:
  `test_a_redirect_without_the_right_state_is_refused_and_the_wait_goes_on`.
- **Time limit.** The wait stops after 5 minutes (`SIGN_IN_TIMEOUT_S`) or when
  the person presses Cancel. The listener serves each connection on its own
  thread and drops one that sends nothing for 5 seconds, so an idle local
  connection can no longer hold it (independent review Q8). It polls every
  0.25 seconds, accepts a single result, answers any later hit with 410, and
  is closed in a `finally`.
- **Page.** The reply page reflects nothing from the request. It sends
  `Cache-Control: no-store` and `Referrer-Policy: no-referrer`.

## PDF passwords

- A password is typed in the password dialog, after Send and Convert. It is
  never pre-filled and never saved.
- While the job runs, it is kept in memory in `ScribeRunner._passwords`, keyed
  by job id. It goes to Scribe in the upload's multipart body (HTTPS) or in the
  channel's `start_conversion` message (WSS), and nowhere else.
- When another app in the family runs the queue, the password is handed over
  through the secret vault (`hand_over_password`), not a file. The app reads it
  once and deletes it (`take_handed_password`). A refused password, a finished
  job or a cancelled job wipes both copies.
- `queue.Job` has no password field, so the password cannot reach
  `queue.json`. No log line includes it, and the `websockets` logger cannot
  print the frame (see Tokens).

## The pagescan subprocess

- **No shell.** Both calls (`list` and `scan`) pass an argument list to
  `quill.stability.safe_subprocess`, which runs `subprocess.run` or `Popen`
  without `shell=True`, with `CREATE_NO_WINDOW` and a timeout. The device id,
  resolution and output folder are separate arguments. `pagescan`'s parser
  takes the word after `--device` as the value even if it starts with `-`. The
  source is always `flatbed` or `feeder`, never text from the device.
- **Which binary runs (fixed).** The last place QUILL looked was `PATH`
  (`shutil.which`). On Windows, that also searches the current folder first, so
  a planted `pagescan.exe` could run in QUILL's place. That fallback is gone.
  The order is now (after the independent review, Q2):
  1. `tools\scanner` beside the installed app;
  2. the Mac app's `Resources/tools/scanner`;
  3. a source checkout's `tools/scanner`;
  4. the Options path;
  5. `QUILL_SCANNER_TOOL`;
  6. the data folder.

  The bundled copy comes first, so the Options path only stands in when there
  is none. The Options path and the variable must be absolute paths on this
  computer (never a UNC share or a mapped network drive), and a file must be
  named `pagescan` (or `wia-scan.exe`). `scanner_tool_path` is a machine-local
  setting, so a portable settings import never carries it. Tests:
  `test_path_and_relative_paths_are_never_searched`,
  `test_the_bundled_tool_wins_over_the_environment_variable` and
  `test_options_can_never_point_at_a_network_or_another_program`.
- **Its answer (fixed).** `pagescan` returns the paths of the pages it saved,
  and those pages can go on to OCR or to Scribe. QUILL now only takes paths that
  resolve inside the output folder it created. A broken or replaced tool
  therefore cannot get another file read or uploaded. Test:
  `test_only_pages_inside_the_scan_folder_are_taken`.
- stderr is never parsed. stdout is read as JSON lines, and only the last
  object counts.

## Downloaded files

- A result's file name comes from the server (`Content-Disposition`, or the
  presigned URL). `safe_filename` turns `< > : " / \ | ? *` and control
  characters into `_` and trims leading and trailing dots and spaces, so
  `../x`, `..\x`, `/etc/x` and `C:\x` all stay inside the chosen folder. A name
  with nothing left becomes "Scribe result".
- **Fixed:** a Windows device name (`CON`, `PRN`, `AUX`, `NUL`, `COM0` to
  `COM9`, `LPT0` to `LPT9`, with or without an extension) gets a leading `_`.
  Test: `test_server_file_names_never_leave_the_folder`.
- `unique_target` never overwrites a file. It adds " (2)", " (3)" and so on.
- A download is written to a partial file named for the job, and moved into
  place in one `os.replace` only when every byte the server announced is on
  disk (independent review Q5, `download.py`). `_write_whole` uses
  `write_bytes_atomic`.
- The presigned storage URL is fetched without the Scribe bearer token.
  Redirects are never followed automatically (`_NoRedirect`), so the token
  cannot leak to another host.
- A result opens in QUILL's editor or Media Player, or its folder is shown. A
  downloaded file is never run.

## Queue and lock files

- `queue.json` and `runner.lock` live in `<QUILL data>/scribe`, the person's
  own profile folder (`%APPDATA%` on Windows, `~/Library/Application Support`
  on a Mac), which only that account can write to.
- Every change is a locked read-modify-write (`file_lock.guarded_write`). The
  file is written with `storage.write_json_atomic`: `mkstemp` in the same
  folder, `fsync`, then an atomic replace. On macOS and Linux, `mkstemp` creates
  the file with mode 0600, and the replace keeps it.
- What the queue holds: source paths, links, Scribe document ids, the chosen
  formats and the folder for results. It holds no token and no password. A
  damaged file is never executed, and since the independent review (Q11) it is
  never written over either: writes refuse, and say so.

## Links and network

- **Convert from a Link (fixed).** Only the dialog checked the link, by its
  prefix, so `http://` with no host passed. `client.check_link` now requires an
  `http` or `https` scheme and a host. `ScribeClient.create_from_url` calls it,
  so every path goes through the check. `file:`, `javascript:`, `ftp:` and bare
  names are refused with a sentence. Test:
  `test_convert_from_a_link_takes_only_web_addresses`.
- Every REST call goes through `scribe/http.request`. It accepts HTTPS only,
  with certificate checks (`quill.core.net`); plain HTTP is allowed only to the
  loopback address the tests' fake server uses. A dev-build environment
  variable that points QUILL at another server is ignored in release builds.
- **WebSocket size (fixed).** The channel used `max_size=None`. Scribe's events
  are small JSON, so the limit is now 16 MB, and a broken or hostile server can
  no longer fill memory.

## Dependencies

- `websockets` 16.1.1 is the one Python dependency the branch adds (to the `ui`
  extra; the shared runtime already had it). `pip-audit` found no known
  vulnerabilities on 2026-11-21.
- `pyobjc-framework-ImageCaptureCore` was removed. Mac scanning now runs
  `pagescan`.
- `pagescan` (Rust, MIT): `cargo audit` on its `Cargo.lock` reports one warning,
  RUSTSEC-2026-0486 (`rustix` 1.1.5, unsound, fixed in 1.1.6). `rustix` comes in
  only through `tempfile`, which is a dev-dependency used by tests, so it is not
  in the shipped binary. It is noted for the pagescan repo, which this review
  did not change.

## Accepted risks

- **Account page link.** Open My Scribe Account calls
  `/auth/browser_from_oauth?token=<access token>`, the way Pneuma's own client
  does, so the browser opens already signed in. The token can then appear in
  the browser's history. It expires within two hours, and Sign Out revokes the
  refresh token. Fixing this needs a server endpoint that takes a one-time code
  instead.
- **Token in the WebSocket URL.** Phoenix sockets authenticate with
  `?token=`. The connection is WSS, the library logger is held at WARNING, and
  a refused handshake is reported to the person as a fixed sentence.

## Independent review

A second reviewer, who had not written this code, reviewed the branch at
`8c644eca5` on 2026-11-21 and reproduced ten failures with tests against the
branch's own fake server. Those reproductions are now real tests, mostly in
`tests/unit/core/test_scribe_review.py`, named for the finding they pin. Each
finding the review ranked critical, high or medium-and-asked-for is listed with
what was done; the reviewer's numbers are kept.

### Critical

- **Q1. Two apps could upload the same job.** The runner lock went stale after
  20 seconds and was beaten only while the queue was idle, so any longer job let
  a second app take it. *Fixed:* the runner beats the lock and renews the job's
  claim from its own thread every 5 seconds while a job runs; `JobQueue.claim`
  marks the job with the runner's pid and a lease under the strict lock, so
  claiming is atomic across processes; and before the upload, each conversion
  and each download the runner checks it still owns both, stopping quietly if
  not. Tests run two runners against a job three times longer than the lock,
  and an app that was frozen past the lock's age.

### High

- **Q2. An imported settings file could choose the scanner program.**
  *Fixed* as described under "The pagescan subprocess" above: the bundled tool
  first, only a local absolute path named `pagescan`, and the setting is
  machine-local (with `scanner_device`, `scanner_device_name` and
  `scribe_download_folder`).
- **Q3. An unexpected error retried every second, forever, saving a new copy
  each time.** *Fixed:* an error that is not one of Scribe's coded answers is
  retried after 10 seconds, 1 minute and 5 minutes (set on the job, so jobs
  behind it run), then the job ends "did not finish" with Retry. A saved format
  is recorded on the job and never fetched again; the local library record is
  best-effort; the runner loop backs off too.
- **Q4. A job waiting for sign-in never resumed.** *Fixed:* signing in (in this
  app or another, or a sign-in found at start) moves every paused job back to
  waiting, and Retry is offered in that state.
- **Q5. Downloads could be saved short, spliced from a stale partial, or blocked
  by a complete one.** *Fixed* in `download.py`: the partial is named for the
  job and resumed only when its sidecar records the server's `ETag` and total;
  `Range` goes with `If-Range`; a 206 must start where the partial ends; a 416
  on a complete partial finishes it; nothing is renamed until the announced
  length is on disk; a drop mid-body (`IncompleteRead` included) resumes, and
  after too many tries the job fails with the partial kept for Retry.
- **Q6. A reconnect after the conversion had finished waited forever.**
  *Fixed:* after waiting on a conversion already in progress the outputs are
  read over REST before starting again, every 30 seconds of channel silence they
  are read again (PRD 3.5's polling fallback), and `phx_close` counts as a
  dropped channel.
- **Q7. Channel `error` events all ended as a plain failure.** *Fixed:* they
  map to the same coded errors as replies. `needs_purchase` leaves the job
  needing pages with Buy Pages and the server's link (accepted only when it is
  Scribe's own `https` page, which also answers low finding Q27);
  `rate_limited` waits 1, 2, 4 and up to 10 minutes and is said once a job, on
  upload too; `conversion_in_progress` keeps waiting.

### Medium

- **Q8. One idle local connection froze the sign-in listener.** *Fixed:* see
  "The loopback sign-in listener".
- **Q9. A PDF password skipped Send Next File.** *Fixed:* a password moves only
  a job that is waiting for its password; a later file of a batch still waits.
- **Q10. A lock file left by a crash disabled locking and stalled writes.**
  *Fixed* in `file_lock.py`: the holder writes its pid, a lock older than 10
  seconds is broken, and the strict form used for claims never writes
  unguarded.
- **Q11. One unreadable read erased the queue or the library.** *Fixed:* only a
  missing file is empty; writes retry briefly and then refuse
  (`QueueUnreadable`), a newer schema is read but not changed, and unknown
  fields and rows are written back untouched.
- **Q12. The Scribe offer replaced the Datalab offer.** *Fixed:* with both
  available, one question offers Scribe for Documents, Datalab cloud OCR, or
  keeping the local result.
- **Q13. A result opened while the person was typing.** *Fixed:* PRD 7.4a's
  typing check. A result opens on its own only after 3 seconds without a key
  or click (`GetLastInputInfo`, which says when, never what).
- **Q15. The details field was rewritten every second.** *Fixed:* it is
  rewritten only when its text changes, keeps the reader's place, and never
  takes focus.
- **Q16. The "AI is off" note was not reachable by Tab.** *Fixed:* the sentence
  is in the What will be sent field, and Send no longer saves the AI-off
  starting value of Describe the pictures as the person's default.
- **Q19. Escape on a scan dialog deleted every scanned page.** *Fixed:*
  cancelling a save goes back to the choices; leaving the choices asks
  "Discard the scanned pages?" with No as the default; a failure while saving
  is said and the pages kept.
- **Q20. A scanner with a feeder and a glass could not use its feeder.**
  *Fixed:* such a scanner asks where to scan from, offering the last answer
  first.
- **Q23. Titles with a full stop got the wrong folder.** *Fixed:* only a real
  input extension is cut from a title; a long file name keeps its extension and
  is cut by bytes; Windows device names are guarded for folders too.

### Second round: the rest of the findings

Mike's rule is that a review is done when its findings are fixed, so the
findings left open after the first round were taken up the same day. Tests are
again named for the finding, in `tests/unit/core/test_scribe_review.py`,
`tests/unit/ui/test_main_frame_scribe.py` and, for the windows themselves,
`tests/unit/ui/test_scribe_windows_real_wx.py` (real wx; run on Windows).

- **Q14. A job run by another open app was said as "finished while QUILL was
  closed".** *Fixed:* the app that started a job says and opens its result
  even when another app ran it (each finished job is claimed once, `delivered`
  on the job). Only when that app's process is gone does the app running the
  queue say it, as "finished while QUILL was closed".
- **Q17. Send before the lists loaded saved "detect" as the language.**
  *Fixed:* a list that has not loaded, or failed, keeps the incoming choice.
- **Q18. Large files read whole, some on the UI thread.** *Fixed:* the upload
  is streamed from disk with its exact `Content-Length` (`http.MultipartFile`;
  a retry after a 401 opens it again); a PDF over 25 MB is described from its
  first and last megabyte, never read whole; scanned pages are decoded one at a
  time and joined with pypdf, and Save as a PDF runs on the task pool.
- **Q21. The scan could deadlock on a full pipe.** *Fixed:* pagescan's stderr
  goes nowhere, stdout is drained on a thread, and a scan stopped for time keeps
  the pages already saved.
- **Q22. My Scribe Documents could show live documents under Trash.** *Fixed:*
  each load is numbered; only the newest, for the view it asked for, is shown.
- **Q24. Password handling.** *Fixed:* a wrong password stays selected in the
  same dialog with the tries left said, and the third ends it (PRD 7.3); a job
  from a link or from My Scribe Documents can be given its password (Scribe
  checks it); a password handed to another app expires after ten minutes and
  is wiped when the app that left it closes. Tests check that it reaches
  neither the log nor `queue.json`.
- **Q25. Offline gave up after about two and a half minutes, and uploads were
  not resumed.** *Fixed:* reconnecting goes on for two hours, each wait up to a
  minute, with the next try shown in the job's details. A dropped upload is
  said once ("Upload of Notes.pdf was interrupted; QUILL will resume it.") and
  resumed. Scribe's API has no resumable upload, so a resumed upload starts
  over; before it does, QUILL looks for a document an earlier try created (same
  name, made since the upload started, held by no other job) and takes it, so
  nothing is sent or charged twice.
- **Q33. "Open Preview" was promised but did not exist.** *Fixed:* a job that
  ran out of pages has Open Preview, which opens the document's own page on
  Scribe, signed in, where the free first two pages can be read. The page's
  address (`/documents/<id>`) is inferred from Scribe's purchase links and
  needs one live check.
- **Low findings.** Fixed: Q26 (a result always gets its format's extension),
  Q27 (first round), Q29 (a refresh token another app just rotated is read back
  from the vault, not a sign-out), Q30 (only a 401 or 403 handshake means signed
  out), Q31 (the private Word copy is removed when the job is done; scanned
  pages kept for the OCR review are left, as that review reads them), Q32 (no
  Scribe log line carries a path, a token or a password: failures log the
  error's type only), Q35 (Open in QUILL without a Word version shows the
  Convert dialog), Q36 (first round), Q38 (Remove keeps its place and says what
  removing a running job means; Retry and Remove no longer share R), Q39, Q40,
  Q41, Q42, Q43 (links named by their file and stripped of their query once
  fetched; finished jobs leave the list after 30 days), Q44 (dates read as
  dates), Q46 (Sign Out always wipes).

### Still open, and why

- **Q28** (the access token in the account page's URL) stays an accepted risk,
  as above; it needs a server endpoint that takes a one-time code.
- **Q34** (`TIER_SCRIBE` in `import_convert`) is not added: the Scribe import
  is announced by the one result sentence (FUX-04), and a second sentence from
  the import tiers would say it twice.
- **Q37** ("in front" counts only this app's own window, not the family's)
  needs the apps to share which one is in front; there is no channel for that
  yet.
- **Q44**, the rest: most Scribe strings still bypass `_()`, as the family's
  other strings do, and file names are not bidi-isolated.
- **Q45** (pinning pagescan's hash in the build scripts) waits for a pagescan
  release whose hash can be pinned.
- The NVDA, JAWS and Narrator passes on the changed windows are still to do.
