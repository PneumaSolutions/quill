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

## Phase 1: done

Sign-in (7.2), the Convert dialog (7.3), the family queue and Scribe
Conversions (7.4, 7.4a), My Scribe Documents (7.5), download folders and the
local record (7.6), Options > Scribe for Documents (7.7), and in QUILL the
Import tier, the low-text PDF offer, Export with Scribe, Braille > Translate
with Scribe, and Tools > Scribe for Documents as 8.1.8 lists it.

## Phase 1: left

- Play Sample beside the voice (7.3) is not built: the voice list has samples,
  but QUILL has no MP3 playback path the dialog can use without a new
  dependency.
- Enter Password from a Scribe Conversions window in an app that is not the
  one running the queue keeps the password in that app; the running app does
  not see it. In practice QUILL is the only app with Scribe in Phase 1.
- MP3 and DAISY results open with the computer's default player rather than
  Quill Media Player (Phase 2 adds Player's own entry point).
- Opening a Word result as rich text (8.1.1) uses QUILL's import readers, so it
  opens as Markdown text.
- The streaming read tab (8.1.3) and the watch-folder action (8.1.6) are
  Phase 3 in the PRD and not started.
- Screen-reader passes with NVDA, JAWS and Narrator, and the live test script on
  a real Scribe account, are still to do.
