"""Sign in to Scribe for Documents: OAuth 2.0 + PKCE through the browser.

Scribe has no password grant. The person signs in on Scribe's own page, in
their own browser, and Scribe redirects to a loopback address QUILL listens
on for that one request. Scribe's developer policy asks that the sign-in page
be opened unmodified in the system browser, so QUILL opens exactly the URL
``scribe_client.AuthClient.authorization_url`` builds and never embeds it.

The listener binds ``127.0.0.1`` (never ``localhost``; RFC 8252 section 8.3)
on the fixed port registered for QUILL
(:data:`quill.core.scribe_account.SCRIBE_REDIRECT_PORT`), serves one
request on the callback path, answers with a short accessible page, and
stops. It never logs a request line: the URL carries the authorization code.
It is not network egress (nothing leaves the machine), so the egress audit
records only :func:`sign_in`, which exchanges the code with Scribe.

wx-free. The browser opener, the listener and the client module are all
injectable so tests run with no socket to Scribe and no browser.
"""

from __future__ import annotations

import logging
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from quill.core.scribe_account import (
    SCRIBE_BASE_URL,
    SCRIBE_REDIRECT_PATH,
    SCRIBE_REDIRECT_PORT,
    ScribeCancelled,
    ScribeDocumentsError,
    StoredTokens,
    client_id,
    client_id_configured,
    client_module,
    safe_mode_active,
    save_tokens,
)
from quill.core.secrets import SecretsManager

logger = logging.getLogger(__name__)

#: How long the listener waits for the person to finish signing in.
SIGN_IN_TIMEOUT_S = 300.0
_POLL_S = 0.25

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - QUILL</title>
</head>
<body>
<main>
<h1>{heading}</h1>
<p>{message}</p>
</main>
</body>
</html>
"""

CancelFn = Callable[[], bool]


class _LoopbackServer(HTTPServer):
    """Refuses a port another program holds, on every platform.

    Windows lets ``SO_REUSEADDR`` bind a port that is already listening, which
    would leave two listeners racing for the redirect, so there QUILL asks for
    the port exclusively instead. Elsewhere ``SO_REUSEADDR`` only skips the
    wait after an earlier sign-in, which is what it is for.
    """

    allow_reuse_address = sys.platform != "win32"

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if sys.platform == "win32" and exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


class LoopbackCatcher:
    """A one-request HTTP listener for Scribe's redirect back to QUILL."""

    def __init__(self, port: int = SCRIBE_REDIRECT_PORT, path: str = SCRIBE_REDIRECT_PATH) -> None:
        self._port = port
        self._path = path
        self._server: _LoopbackServer | None = None
        self._lock = threading.Lock()
        self._result: dict[str, str] = {}

    @property
    def port(self) -> int:
        if self._server is None:
            return self._port
        return int(self._server.server_address[1])

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{self._path}"

    def start(self) -> None:
        catcher = self

        class _Handler(BaseHTTPRequestHandler):
            server_version = "QUILL"
            sys_version = ""

            def do_GET(self) -> None:  # noqa: N802 - http.server's name
                catcher._handle(self)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return  # the request line carries the authorization code

        try:
            server = _LoopbackServer(("127.0.0.1", self._port), _Handler)
        except OSError as exc:
            raise ScribeDocumentsError(
                f"QUILL could not listen for the Scribe sign-in on port {self._port}. "
                "Another program may be using it. Close it and try again. "
                f"({exc.strerror or exc})"
            ) from None
        server.timeout = _POLL_S
        self._server = server

    def wait(self, *, cancel_requested: CancelFn | None, timeout: float) -> str:
        """Serve until the redirect arrives; return the authorization code."""
        server = self._server
        if server is None:
            raise ScribeDocumentsError("The sign-in listener was not started.")
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                result = dict(self._result)
            if result:
                break
            if cancel_requested is not None and cancel_requested():
                raise ScribeCancelled("Sign-in to Scribe for Documents was cancelled.")
            if time.monotonic() >= deadline:
                raise ScribeDocumentsError(
                    "Sign-in to Scribe for Documents timed out. Try again when you "
                    "are ready to sign in."
                )
            server.handle_request()
        if result.get("error"):
            raise ScribeDocumentsError(
                f"Scribe for Documents did not sign you in: {result['error']}."
            )
        return result["code"]

    def close(self) -> None:
        if self._server is not None:
            self._server.server_close()
            self._server = None

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        parts = urlsplit(handler.path)
        if parts.path != self._path:
            self._respond(handler, 404, "Not found", "Not found", "")
            return
        query = parse_qs(parts.query)
        code = (query.get("code") or [""])[0]
        error = (query.get("error_description") or query.get("error") or [""])[0]
        with self._lock:
            if self._result:
                self._respond(handler, 410, "Already used", "Already used", "")
                return
            self._result = {"code": code} if code else {"error": error or "no code"}
        if code:
            self._respond(
                handler,
                200,
                "Signed in",
                "You're signed in to Scribe for Documents",
                "You can close this tab and go back to QUILL.",
            )
        else:
            self._respond(
                handler,
                400,
                "Not signed in",
                "QUILL was not signed in to Scribe for Documents",
                "You can close this tab and go back to QUILL to try again.",
            )

    @staticmethod
    def _respond(
        handler: BaseHTTPRequestHandler, status: int, title: str, heading: str, message: str
    ) -> None:
        body = _PAGE.format(title=title, heading=heading, message=message).encode("utf-8")
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "text/html; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Referrer-Policy", "no-referrer")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(body)
        except OSError:
            pass  # the browser went away; the code is what matters


def sign_in(
    *,
    module: Any | None = None,
    manager: SecretsManager | None = None,
    open_browser: Callable[[str], object] = webbrowser.open,
    catcher: LoopbackCatcher | None = None,
    cancel_requested: CancelFn | None = None,
    timeout: float = SIGN_IN_TIMEOUT_S,
) -> StoredTokens:
    """Run the browser sign-in and store the resulting tokens.

    GATE-9 (``_NON_AST_EGRESS``): ``AuthClient.exchange_code`` posts the
    authorization code and PKCE verifier to Scribe's token endpoint over
    HTTPS. Reached only from Sign In to Scribe for Documents (or the sign-in
    the Convert command offers), on a worker thread, never in Safe Mode.
    """
    if safe_mode_active():
        raise ScribeDocumentsError("Scribe for Documents is off in Safe Mode.")
    if not client_id_configured():
        raise ScribeDocumentsError(
            "This copy of QUILL is not registered with Scribe for Documents yet."
        )
    mod = module if module is not None else client_module()
    listener = catcher or LoopbackCatcher()
    listener.start()  # listen before the browser opens, so no redirect is missed
    try:
        redirect_uri = listener.redirect_uri
        auth = mod.AuthClient(SCRIBE_BASE_URL, client_id())
        pkce = mod.PkceChallenge()
        open_browser(auth.authorization_url(redirect_uri, pkce))
        code = listener.wait(cancel_requested=cancel_requested, timeout=timeout)
    finally:
        listener.close()
    try:
        token_set = auth.exchange_code(redirect_uri, code, pkce.verifier)
    except mod.InvalidGrantError:
        raise ScribeDocumentsError(
            "Scribe for Documents did not accept the sign-in. It may have taken too "
            "long. Try again."
        ) from None
    except mod.ScribeApiError as exc:
        raise ScribeDocumentsError(
            f"QUILL could not finish signing in to Scribe for Documents. ({exc})"
        ) from None
    tokens = StoredTokens.from_token_set(token_set)
    save_tokens(tokens, manager)
    logger.info("Signed in to Scribe for Documents")
    return tokens


__all__ = ["SIGN_IN_TIMEOUT_S", "LoopbackCatcher", "sign_in"]
