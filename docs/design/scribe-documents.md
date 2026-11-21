# Scribe for Documents in QUILL: implementation notes

The specification is Jeff Bishop's Scribe PRD ("Scribe for Documents in the
QUILL family", 2026-11-20). This note records how QUILL's Phase 1 build maps
onto it, where the real service behaves differently from what the PRD could
know, and what is left. The user-facing description is in Chapter 3 of the
user guide, under "Converting with Scribe for Documents".

## Where the code is

Shared foundation, `quill/core/scribe/` (wx-free, each module under 600 lines):

- `model.py`: formats, build order, stages, settings groups, `DocumentSettings`,
  and the section 11 errors (`QUILL-SCRIBE-SIGNIN`, `-PAGES`, `-PASSWORD`,
  `-BUSY`, `-GONE`, `-OFFLINE`, `-SIGNIN-LOCAL`, `-SAVE`, `-OFF`, `-FAILED`).
- `http.py`: the one `urllib` door every REST call goes through (GATE-9).
- `auth.py`: PKCE sign-in, the loopback listener, tokens in the secret vault.
- `client.py`: the REST API. `channel.py`: the Phoenix channel.
- `queue.py` and `runner.py`: the family queue and the one runner.
- `library.py`: download folders and which document made which file.
- `consent.py`: the Convert dialog's wording; `settings_fields.py`: Options;
  `keys.py`: the four default keys.

QUILL's surfaces: `quill/ui/main_frame_scribe.py` (commands, menus, entry
points), `main_frame_scribe_queue.py` (runner, speech, results), and the
windows `scribe_signin_dialog.py`, `scribe_convert_dialog.py` with
`scribe_settings_panel.py`, `scribe_conversions_window.py` and
`scribe_documents_dialog.py`. Tests run QUILL's real client against
`tests/scribe_fake_server.py`, a real HTTP and WebSocket fake.

## Where the real service differs from the PRD

Read from Pneuma's server and client source on 2026-11-21.

- **Redirect path.** QUILL's approved registration is
  `http://127.0.0.1:47613/callback`. The server ignores the port for a loopback
  address but matches the path exactly, so QUILL listens on any free port at
  `/callback`, not `/scribe/callback`.
- **Accessible HTML.** The API does not hand out the `html` output for download.
  QUILL converts `offline_html` (the same document with its pictures), unpacks
  it next to the other results, and opens its page.
- **Repeat formats are free.** Pages are charged once per document, for its
  first full conversion; the dialog's "already made, no pages used" is right,
  and so is "further formats are free".
- **DAISY** is DAISY 3 (an NCX), which Media Player already reads.
- **Rate limit.** Uploads and `start_conversion` share one bucket of 10 a
  minute per person, so a job asks the REST API whether a format is already
  made before starting it.
- **Pages left** come from `/api/billing`; `/api/account_info` has no count.
- **Logging.** `websockets` logs the request line (with the token) and every
  frame (with a PDF password) at DEBUG; QUILL gives it a logger held at WARNING.

- **Accessible HTML and the offline page are the same file.** Both come from
  the `offline_html` output, which Scribe sends as one self-contained HTML
  file (pictures inline), not a zip. QUILL downloads it once for both.
- **MP3 is one `.mp3`**; DAISY is a zip of a DAISY 3 book (`dtb.ncx`,
  `dtb.opf`, `dtbook.xml`, SMIL and numbered MP3s).
- **Word results use `w:jc w:val="start"`**, which python-docx cannot map; QUILL's
  rich Word read used to fail on every Scribe result (fixed in `docx_reader.py`).
- **Voice samples** are public at `/sounds/tts-samples/<short name>.mp3`. The
  voice list's third field says whether one exists (43 of 84 US English
  voices on 2026-11-21); the others answer 404.
- **A wrong PDF password after upload** comes back as an `ok` reply to
  `start_conversion` followed by an `error` event, `invalid_password`.

## Live runs (2026-11-21, Mike's account, on the HP)

Driven by QUILL's own `QueueRunner`, `ScribeClient` and `DocumentChannel`,
unmodified, from a script (`~/work/hp/scribe_live.py` on the Spark) with a
private queue folder so the running QUILL never saw the jobs. Every test
document was moved to Trash and deleted forever afterwards.

| Source | Formats | Result |
|---|---|---|
| A real PDF (the NVIDIA model licence, 2 pages to Scribe) | all nine | All made and downloaded in 123 s: HTML, Word, tagged PDF 1.7, EPUB, MOBI, BRF, offline page, MP3 (8 MB), DAISY 3 zip |
| The scanned page `test-scan.png` | all nine | All made in 45 s; OCR text correct, including the Arabic line |
| Password-protected PDF | Word | `password_required` waited for the person; a wrong password was refused; the right one converted |
| A link to a public PDF | Word, HTML | Made in 15 s, nothing uploaded from the PC |
| Export with Scribe (a QUILL document saved as Word first) | tagged PDF, MOBI, Word | All made |
| Braille > Translate with Scribe | BRF | Made; UEB grade 2, 40 cells |
| Per-document settings (1.5x speech, 32 cells, large print) | BRF, MP3 | Made with the settings sent |
| Two jobs at once | several | Hit `rate_limited`; QUILL said "asked QUILL to slow down" once and carried on after a minute |
| Play Sample (`VoiceSample` in a real wx window) | | Sample fetched (220 KB), played through the shared engine, stopped |

The Word results open in QUILL's rich editor with no fidelity warning (no
comments, tracked changes or headers flagged). Each run used one to two
pages; 7286 pages before, 7276 after.

## Phase 1: done

Sign-in (7.2), the Convert dialog (7.3) with Play Sample, the family queue
and Scribe Conversions (7.4, 7.4a), My Scribe Documents (7.5), download
folders and the local record (7.6), Options > Scribe for Documents (7.7), and
in QUILL the Import tier (Word opens as rich text), the low-text PDF offer,
Export with Scribe, Braille > Translate with Scribe, and Tools > Scribe for
Documents as 8.1.8 lists it.

- **Who speaks.** Each stage is said once a job (every format passes through
  "converting"); percentages at 25, 50 and 75.
- **Passwords across apps (7.4a).** Enter Password in an app that does not run
  the queue hands the password to the app that does through the OS secret
  vault (namespace `scribe`, so Sign Out wipes it too), which reads it once
  and deletes it. It is never in the queue file or a log. A wrong password
  counts a try; the third ends the job with Retry.
- **Media Player (8.3).** MP3 and DAISY results open in Quill Media Player
  where the build has it (`app_launcher.is_app_released("player")`: dev builds
  today, since the Player is still gated). A DAISY zip is unpacked once beside
  itself and the Player gets its `.ncx`, which it now opens from the command
  line or a second launch without a picker. Without the Player, the
  computer's default player opens them.

## Scanning as a way in

File > Import > Scan Pages feeds the same Import tier, so a scanned page that
QUILL's OCR reads poorly gets the Scribe offer like any other import. The scanner tool is pagescan
(Pneuma Solutions, MIT), one executable QUILL runs as a separate process on
both Windows and macOS through `quill/core/scanning/pagescan_cli.py`; it
replaced the Windows-only `wia-scan.exe` and the PyObjC ImageCaptureCore
backend on 2026-11-21. QUILL always names the source by kind (`--source
flatbed` or `--source feeder`), never by id. The design, bundling and open
questions are in `scribe-scanning-addendum.md`.

## Phase 1: left

- A Word result opened from Scribe Conversions (not the Import tier) goes
  through File > Open, so a Word file with comments or tracked changes would
  ask the fidelity question. Scribe's results have had none so far.
- Play Sample uses whichever engine `create_engine` picks (wx.media on the HP);
  it has not been tried with a screen reader yet.
- The streaming read tab (8.1.3) and the watch-folder action (8.1.6) are
  Phase 3 in the PRD and not started.
- Screen-reader passes with NVDA, JAWS and Narrator are still to do. The live
  runs above drove the core, not the windows.
- Scanning on a Mac goes through pagescan's macOS backend, which nobody has
  run on a Mac with a scanner yet.
