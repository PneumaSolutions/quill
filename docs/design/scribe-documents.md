# Scribe for Documents in QUILL: design note

This note explains how QUILL converts documents with Scribe for Documents, the
cloud conversion service Pneuma Solutions runs at scribeit.io, and why it is
built the way it is. The user-facing description is in Chapter 3 of the user
guide, under "Converting with Scribe for Documents".

## What it does

Three commands sit in Tools > Reading and Dictation > OCR and Document
Conversion, beside the Datalab cloud tier:

- **Convert with Scribe for Documents...** converts a file, the current
  document or a web address. Accessible HTML, the default, opens in a new
  unsaved QUILL document for editing. Word, tagged PDF, EPUB, DAISY, MP3,
  braille (BRF), offline HTML and MOBI are saved to a folder the person
  chooses.
- **Sign In to Scribe for Documents...** runs OAuth 2.0 with PKCE in the
  system browser.
- **Sign Out of Scribe for Documents** forgets the stored tokens.

None of the three has a default key. They are once-in-a-while commands
reached from the menu or the command palette, like the other cloud OCR
commands beside them.

## Why a core feature and not a bundled Quillin

The bundled transcription Quillins (Groq, ElevenLabs) only *declare* a
provider in their manifest; the host owns every byte of the upload, under the
network egress audit. Doing the same for Scribe would have meant a new
contribution type (a schema change, validation, the model, quillin_lint
rules) whose only implementation is still host code. A manifest with nothing
in it but a declaration adds a moving part without moving any code out of the
host.

QUILL's existing cloud document conversion, Datalab, is already a core
feature: a wx-free client in `quill/core/datalab_ocr.py` and its UI in the
`main_frame_docconvert.py` mixin. Scribe follows that shape exactly, so a
reviewer who knows one knows the other. Scribe also needs a browser sign-in
with a loopback listener, a secret-vault token store and a long-lived
WebSocket, none of which a Quillin sandbox could hold.

## Where the code is

- `quill/core/scribe_account.py` (wx-free): the client ID and redirect, the
  token store (through `quill.core.secrets`), the error sentences, and the
  authenticated client.
- `quill/core/scribe_documents.py` (wx-free): the input and output format
  tables and `run_conversion`, which uploads, converts, follows the status
  channel and downloads. It re-exports the account module, so callers import
  one module.
- `quill/core/scribe_signin.py` (wx-free): the one-request loopback listener
  on `127.0.0.1` and `sign_in`.
- `quill/ui/main_frame_scribe.py`: the `ScribeDocumentsMixin` with the three
  commands. It asks questions with stock dialogs and runs every network step
  on the background task pool.
- Tests: `tests/unit/core/test_scribe_documents.py`,
  `tests/unit/core/test_scribe_signin.py`,
  `tests/unit/ui/test_main_frame_scribe.py`, with a fake `scribe_client` in
  `tests/scribe_fakes.py`. No test talks to Scribe.

## The client and its dependency

QUILL uses the official `scribe-client` package from PyPI (a Rust core with
Python bindings, MIT or Apache 2.0, wheels for Windows, macOS and Linux). It
is imported lazily. If it is missing, the command says so in a sentence
instead of failing. It is not yet in `pyproject.toml` or the shared Windows
runtime; adding it there is part of getting the feature ready to ship.

## The client ID

Scribe registers each app at scribeit.io/developer as a public client that
uses PKCE and has no secret. `SCRIBE_CLIENT_ID` in
`quill/core/scribe_account.py` holds a placeholder until Pneuma Solutions
registers QUILL and supplies the real ID, with the redirect URI
`http://127.0.0.1:47613/callback`. While the placeholder is there, every
command says the build is not registered yet and opens nothing. The
environment variable `QUILL_SCRIBE_CLIENT_ID` overrides it for development.

## Scribe's developer policy, and how QUILL meets it

- **Say it is powered by Scribe for Documents.** The format question, the
  consent window, the services overview and the F1 help all say so.
- **Open the sign-in page unmodified in the system browser.** QUILL opens
  exactly the address `scribe_client.AuthClient.authorization_url` builds,
  with Python's `webbrowser`, and never embeds it.
- **One document at a time.** A job is one file or one address. A second
  Convert while one is running is refused with a sentence.
- **Pass along every status update.** Each distinct status event Scribe sends
  becomes a sentence, such as "Scribe for Documents: HTML converting, 40
  percent.", which goes to the status bar and is spoken. Only exact repeats
  are dropped.

## Consent, privacy and network egress

- A consent window opens before every conversion. It names the service, says
  the document is kept in the person's Scribe library and uses their page
  credits, and adds the same CAUTION line as Datalab for a sensitive-looking
  file name. There is no "don't ask again".
- Tokens are stored in the OS secret vault through `quill.core.secrets`
  (namespace `scribe`), never in settings. Sign Out wipes the namespace.
- Safe Mode refuses every Scribe command.
- Logs carry document ids, formats and stages only.
- `scribe-client` does its HTTPS and WebSocket work inside a compiled
  extension, so the AST egress scan cannot see it. The two integration
  points, `scribe_account._make_client` and `scribe_signin.sign_in`, are in
  `_NON_AST_EGRESS` (through `quill/tools/network_egress_entries_scribe.py`)
  with their rationale.
- The loopback listener binds `127.0.0.1` on the registered port, serves one
  request on `/callback`, never logs the request line (it carries the code),
  and on Windows asks for the port exclusively so another program cannot
  answer in its place.

## Status, passwords and lost connections

- A new document is busy with its preview at first, so starting the
  conversion waits for that to finish and then tries again.
- A protected PDF ends the background job with "password needed". QUILL asks
  for the password with a native password box, then resumes with the same
  document id, so nothing is uploaded twice. A wrong password is reported as
  wrong and asked again.
- If the live channel drops, QUILL says so and reconnects up to five times
  with growing pauses. Each time it first asks Scribe for the document's
  outputs, because the conversion carries on without the channel and may
  have finished meanwhile; that covers conversions longer than the channel's
  idle limit of about 66 seconds.
- Out of page credits, an expired sign-in, rate limits and a damaged file each
  have their own sentence. An expired sign-in offers to sign in again.

## Accessibility

- Every question is a stock control (a single-choice list, the file and
  folder boxes, a text box, a password box) run through `_show_modal_dialog`
  and registered in the dialog inventory. The text and password boxes carry
  F1 help.
- Menu items carry access keys that are free in their submenu (C, G and O).
  The mixin adds its own rows and registrations
  (`_append_scribe_rows`, `_register_scribe_commands`), so the menu and
  command modules grow by one line each.
- Announcements say only what the screen reader cannot know: status updates,
  the result, and errors.
- The page the browser shows after sign-in has a language, a title, a main
  landmark and one heading.

## Still to do before release

- The real client ID from Pneuma Solutions, and the redirect registered.
- `scribe-client` added to `pyproject.toml`, `requirements.txt`, `uv.lock` and
  the shared Windows runtime inventory.
- A screen-reader pass on Windows with JAWS and NVDA, and on macOS with
  VoiceOver, against a real Scribe account.
- The Python binding does not hand back tokens it refreshes on its own during
  a long conversion. QUILL refreshes explicitly before each conversion and
  stores the result, but a token rotated mid-conversion is lost, which costs
  one extra sign-in. A small change to `scribe-client` (expose the current
  token set) would remove that.
