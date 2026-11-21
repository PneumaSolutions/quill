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
  the person presses Cancel. The listener handles one request at a time with a
  0.25-second poll, accepts a single result, answers any later hit with 410, and
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
  The order is now:
  1. the Options path, which must be absolute;
  2. `tools\scanner` beside the installed app;
  3. the Mac app's `Resources/tools/scanner`;
  4. a source checkout's `tools/scanner`;
  5. `QUILL_SCANNER_TOOL`, which must be absolute;
  6. the data folder.

  The bundled copy now comes before the environment variable. A relative path
  in Options or in the variable is ignored. Tests:
  `test_path_and_relative_paths_are_never_searched` and
  `test_the_bundled_tool_wins_over_the_environment_variable`.
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
- A download is written to `<name>.part` and renamed when complete.
  `_write_whole` uses `write_bytes_atomic`.
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
  damaged file is read as an empty queue and is not executed.

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
