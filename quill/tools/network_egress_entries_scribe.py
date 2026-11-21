"""Reviewed egress for Scribe for Documents (GATE-9), split from
``network_egress_entries`` (GATE-11). ``scribe-client`` does its HTTPS and
WebSocket work inside a compiled extension, so these live in
``_NON_AST_EGRESS``: each key names the function that reaches it.
"""

from __future__ import annotations

SCRIBE_EGRESS: dict[str, str] = {
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
