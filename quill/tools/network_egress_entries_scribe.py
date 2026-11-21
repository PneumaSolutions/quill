"""Reviewed egress for Scribe for Documents (GATE-9), split from
``network_egress_entries`` (GATE-11).

QUILL's own client (``quill/core/scribe/``, Scribe PRD 7.1) has exactly two
doors: :func:`quill.core.scribe.http.request` for every REST call (seen by the
AST scan, so it is in ``SCRIBE_REVIEWED_EGRESS``) and
:func:`quill.core.scribe.channel.connect` for the document channel's
WebSocket (``websockets`` hides the socket from the scan, so it is in
``SCRIBE_EGRESS``, which joins ``_NON_AST_EGRESS``).
"""

from __future__ import annotations

_SCRIBE_TRIGGERS = (
    "Reached only from a Scribe for Documents command the person chose (sign in, "
    "the Convert dialog after its Send and Convert button, which names the service, "
    "the account's retention and page use, and a caution for sensitive file names; "
    "My Scribe Documents; Options), or from the family queue running a job that "
    "command started; one document at a time; on a worker thread; refused in Safe "
    "Mode. Fixed host https://scribeit.io (a dev build may point it at the tests' "
    "loopback fake). Tokens live in the OS secret vault, never settings; nothing "
    "logs tokens, passwords, document text or titles of sensitive files."
)

SCRIBE_REVIEWED_EGRESS: dict[str, str] = {
    "core/scribe/http.py::request": (
        "Scribe for Documents' REST API (Pneuma Solutions): OAuth token exchange, "
        "refresh and revoke; account and billing; upload of the file the person "
        "chose (or the link they typed); the document list, rename, trash, recover "
        "and delete; per-document and account settings and their choice lists; "
        "human-review requests; and downloads of finished outputs (the API's 302 "
        "to presigned storage is followed without the bearer token). " + _SCRIBE_TRIGGERS
    ),
}

SCRIBE_EGRESS: dict[str, str] = {
    "core/scribe/channel.py::connect": (
        "Scribe for Documents' document channel: a Phoenix WebSocket at "
        "wss://scribeit.io/socket/websocket carrying start_conversion and the "
        "status, chunk, conversion_complete, error and password_required events "
        "for a document the person sent, with a 30-second heartbeat while QUILL "
        "waits. Its websockets logger is held at WARNING so the token in the URL "
        "and a PDF password in a frame are never logged. " + _SCRIBE_TRIGGERS
    ),
    "core/scribe_account.py::_make_client": (
        "Convert with Scribe for Documents (Pneuma Solutions, scribeit.io). The "
        "official scribe-client package (a compiled Rust extension) does the HTTPS "
        "and WebSocket work, so no urlopen appears in quill/. The client this "
        "function builds uploads the one file the person chose (or sends the one "
        "web address they typed), starts the conversion over Scribe's document "
        "channel, follows its status, refreshes the OAuth token, and downloads "
        "the result. Fixed endpoint https://scribeit.io. Reached only from the "
        "command, after a per-use consent dialog that names the service, says "
        "the document is kept in the person's Scribe library and uses their page "
        "credits, and warns about sensitive file names; one document at a time; "
        "on a worker thread; refused in Safe Mode and while the build carries the "
        "placeholder client ID. Tokens live in the OS secret vault "
        "(quill.core.secrets), never settings.json. Logs document ids, formats "
        "and stages only, never contents, tokens or passwords."
    ),
    "core/scribe_signin.py::sign_in": (
        "Sign In to Scribe for Documents: opens Scribe's own sign-in page, "
        "unmodified, in the system browser (OAuth 2.0 + PKCE, public client, no "
        "secret), catches the redirect on a one-request 127.0.0.1 listener on the "
        "registered port, and exchanges the code and PKCE verifier with "
        "https://scribeit.io through scribe-client's AuthClient. Explicit user "
        "action only, on a worker thread, cancellable, refused in Safe Mode. The "
        "token set goes to the OS secret vault; Sign Out wipes it."
    ),
}
