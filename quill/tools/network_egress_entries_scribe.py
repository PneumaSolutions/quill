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
        "human-review requests; Scribe's public sample recording of a voice, for Play Sample in "
        "the Convert dialog (no token sent); and downloads of finished outputs (the API's 302 "
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
}
