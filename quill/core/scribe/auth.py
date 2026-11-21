"""Signing in to Scribe for Documents (wx-free; Scribe PRD 7.2).

OAuth 2.0 with PKCE (:mod:`quill.core.auth.pkce`) in the person's own
browser. QUILL listens on ``127.0.0.1`` on a free port for the one redirect;
Scribe accepts any port on the loopback address as long as the scheme, host
and path match the registration (``redirect_uri.ex``: "the port doesn't count
for a loopback URI"). QUILL's registration (approved 2026-11-20) is
``http://127.0.0.1:47613/callback``, so the path is ``/callback``.

The sign-in page opens unchanged (SCR-POL-2) through ``/auth/logout?next=``,
as Pneuma's own client does, so a browser session left from an earlier
account cannot finish the sign-in without asking.

Tokens live in the OS secret vault under namespace ``scribe`` (one sign-in
for the whole family), never in settings. An access token lasts two hours;
QUILL refreshes it five minutes early and once on a 401. Sign Out revokes
the refresh token (RFC 7009) and wipes the namespace.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from quill.core.auth.pkce import generate_pkce_pair
from quill.core.scribe import http
from quill.core.scribe.model import (
    SCRIBE_BASE_URL,
    ScribeCancelled,
    ScribeError,
    ScribeOff,
    ScribeSignInLocal,
    ScribeSignInRequired,
)
from quill.core.secrets import SecretRef, SecretsManager

logger = logging.getLogger(__name__)

#: QUILL's public client ID (a PKCE client has no secret, so it may ship).
CLIENT_ID = "8b535f02eb781e4ccab66bf88b93fc47a8605303bdf352d197788812bbb140da"
CLIENT_ID_PLACEHOLDER = "REPLACE-WITH-QUILL-CLIENT-ID"
CLIENT_ID_ENV = "QUILL_SCRIBE_CLIENT_ID"
BASE_URL_ENV = "QUILL_SCRIBE_BASE_URL"  # development and the tests' fake server
REDIRECT_PATH = "/callback"
SIGN_IN_TIMEOUT_S = 300.0
REFRESH_MARGIN_S = 300
SECRETS_NAMESPACE = "scribe"
_TOKENS_REF = SecretRef(SECRETS_NAMESPACE, "tokens")
_POLL_S = 0.25


def client_id() -> str:
    return os.environ.get(CLIENT_ID_ENV, "").strip() or CLIENT_ID


def client_id_configured() -> bool:
    value = client_id()
    return bool(value) and value != CLIENT_ID_PLACEHOLDER


def base_url() -> str:
    """Scribe's address; a dev build may point it at a test server."""
    from quill.core import paths

    override = os.environ.get(BASE_URL_ENV, "").strip()
    if override and paths._DEV_BUILD:
        return override.rstrip("/")
    return SCRIBE_BASE_URL


def safe_mode_active() -> bool:
    return os.environ.get("QUILL_SAFE_MODE") == "1"


def check_allowed() -> None:
    """Safe Mode turns every Scribe command off (PRD 10.1, QUILL-SCRIBE-OFF)."""
    if safe_mode_active():
        raise ScribeOff("Scribe for Documents is off in Safe Mode.")
    if not client_id_configured():
        raise ScribeOff("This copy of QUILL is not registered with Scribe for Documents.")


# ------------------------------------------------------------------ tokens


@dataclass(frozen=True, slots=True)
class StoredTokens:
    access_token: str
    refresh_token: str
    expires_at: float | None = None

    def expires_soon(self, now: float) -> bool:
        return self.expires_at is not None and self.expires_at - now < REFRESH_MARGIN_S


class TokenStore:
    """The sign-in in the secret vault, refreshed when it is about to expire."""

    def __init__(
        self, manager: SecretsManager | None = None, *, now: Callable[[], float] = time.time
    ) -> None:
        self._manager = manager
        self._now = now
        self._lock = threading.Lock()

    @property
    def manager(self) -> SecretsManager:
        if self._manager is None:
            self._manager = SecretsManager()
        return self._manager

    def load(self) -> StoredTokens | None:
        try:
            data = self.manager.get_json(_TOKENS_REF)
        except Exception:  # noqa: BLE001 - a locked vault is "signed out"
            return None
        if not data or not data.get("access_token"):
            return None
        expires = data.get("expires_at")
        return StoredTokens(
            str(data["access_token"]),
            str(data.get("refresh_token") or ""),
            float(expires) if isinstance(expires, (int, float)) else None,
        )

    def save(self, tokens: StoredTokens) -> None:
        self.manager.set_json(
            _TOKENS_REF,
            {
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "expires_at": tokens.expires_at,
            },
        )

    def save_reply(self, reply: dict[str, Any]) -> StoredTokens:
        """Store a ``/oauth/token`` answer."""
        expires_in = reply.get("expires_in")
        tokens = StoredTokens(
            str(reply.get("access_token") or ""),
            str(reply.get("refresh_token") or ""),
            self._now() + float(expires_in) if isinstance(expires_in, (int, float)) else None,
        )
        if not tokens.access_token:
            raise ScribeError("Scribe for Documents did not send a sign-in. Try again.")
        self.save(tokens)
        return tokens

    def signed_in(self) -> bool:
        return self.load() is not None

    def wipe(self) -> None:
        try:
            self.manager.wipe_namespace(SECRETS_NAMESPACE)
        except Exception:  # noqa: BLE001 - nothing stored is signed out
            logger.info("Scribe sign-in was already gone")

    def access_token(self, *, force_refresh: bool = False) -> str:
        """A usable access token, refreshed first when it is close to expiry."""
        with self._lock:
            tokens = self.load()
            if tokens is None:
                raise ScribeSignInRequired(
                    "Sign in to Scribe for Documents first.", server_code="unauthenticated"
                )
            if (force_refresh or tokens.expires_soon(self._now())) and tokens.refresh_token:
                tokens = self._refresh(tokens)
            return tokens.access_token

    def _refresh(self, tokens: StoredTokens) -> StoredTokens:
        reply = token_request({
            "grant_type": "refresh_token",
            "client_id": client_id(),
            "refresh_token": tokens.refresh_token,
        })
        fresh = self.save_reply(reply)
        if not fresh.refresh_token:  # the server keeps the old one valid
            fresh = StoredTokens(fresh.access_token, tokens.refresh_token, fresh.expires_at)
            self.save(fresh)
        return fresh


def token_request(form: dict[str, str]) -> dict[str, Any]:
    """``POST /oauth/token``; an ``invalid_grant`` means sign in again."""
    response = http.request(
        "POST",
        f"{base_url()}/oauth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        body=urlencode(form).encode("ascii"),
    )
    if response.status >= 400:
        data = response.json()
        if isinstance(data, dict) and data.get("error") == "invalid_grant":
            raise ScribeSignInRequired(
                "Your Scribe for Documents sign-in has ended. Conversions are paused "
                "until you sign in again.",
                server_code="invalid_grant",
            )
        http.raise_for(response, what="signing in")
    data = response.json()
    if not isinstance(data, dict):
        raise ScribeError("Scribe for Documents sent an answer QUILL could not read.")
    return data


# ------------------------------------------------------------------ the listener

_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - {app}</title></head>
<body><main><h1>{title}</h1><p>{message}</p></main></body></html>
"""


class _LoopbackServer(HTTPServer):
    """Exclusive on Windows (``SO_REUSEADDR`` there lets two listeners race)."""

    allow_reuse_address = sys.platform != "win32"

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if sys.platform == "win32" and exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


class LoopbackCatcher:
    """A one-request listener on ``127.0.0.1`` (any free port) for the redirect."""

    def __init__(self, *, port: int = 0, app_name: str = "QUILL") -> None:
        self._port = port
        self._app = app_name
        self._server: _LoopbackServer | None = None
        self._lock = threading.Lock()
        self._result: dict[str, str] = {}

    @property
    def redirect_uri(self) -> str:
        port = self._server.server_address[1] if self._server else self._port
        return f"http://127.0.0.1:{port}{REDIRECT_PATH}"

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
            self._server = _LoopbackServer(("127.0.0.1", self._port), _Handler)
        except OSError as exc:
            raise ScribeSignInLocal(
                f"{self._app} could not listen for the Scribe for Documents sign-in on this "
                "computer. Security software may be blocking local connections. "
                f"({exc.strerror or exc})"
            ) from None
        self._server.timeout = _POLL_S

    def wait(self, *, cancel_requested: Callable[[], bool] | None, timeout: float) -> str:
        """Serve until the redirect arrives; return the authorization code."""
        server = self._server
        if server is None:
            raise ScribeSignInLocal("The sign-in listener was not started.")
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                result = dict(self._result)
            if result:
                break
            if cancel_requested is not None and cancel_requested():
                raise ScribeCancelled("Sign-in to Scribe for Documents was cancelled.")
            if time.monotonic() >= deadline:
                raise ScribeError("Sign-in timed out. Try again?", server_code="timeout")
            server.handle_request()
        if result.get("error"):
            raise ScribeSignInRequired(
                f"Scribe for Documents did not sign you in: {result['error']}.",
                server_code="access_denied",
            )
        return result["code"]

    def close(self) -> None:
        if self._server is not None:
            self._server.server_close()
            self._server = None

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        parts = urlsplit(handler.path)
        if parts.path != REDIRECT_PATH:
            self._respond(handler, 404, "Not found", "")
            return
        query = parse_qs(parts.query)
        code = (query.get("code") or [""])[0]
        error = (query.get("error_description") or query.get("error") or [""])[0]
        with self._lock:
            if self._result:
                self._respond(handler, 410, "Already used", "")
                return
            self._result = {"code": code} if code else {"error": error or "no code"}
        if code:
            self._respond(
                handler, 200, "Signed in", f"You can close this tab and return to {self._app}."
            )
        else:
            self._respond(
                handler, 400, "Not signed in", f"You can close this tab and return to {self._app}."
            )

    def _respond(self, handler: BaseHTTPRequestHandler, status: int, title: str, msg: str) -> None:
        body = _PAGE.format(title=title, message=msg, app=self._app).encode("utf-8")
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


# ------------------------------------------------------------------ sign in / out


def authorization_url(redirect_uri: str, challenge: str, *, locale: str = "") -> str:
    """Scribe's sign-in page, through ``/auth/logout?next=`` (as the official client)."""
    authorize = "/oauth/authorize?" + urlencode({
        "response_type": "code",
        "client_id": client_id(),
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    query = {"next": authorize}
    if locale:
        query["locale"] = locale
    return f"{base_url()}/auth/logout?{urlencode(query)}"


class SignInFlow:
    """One sign-in: :meth:`start` (listener up, URL ready), then :meth:`finish`.

    Split so the waiting dialog can show Copy Sign-in Link before the wait.
    """

    def __init__(
        self,
        store: TokenStore | None = None,
        *,
        catcher: LoopbackCatcher | None = None,
        app_name: str = "QUILL",
    ) -> None:
        self.store = store or TokenStore()
        self._catcher = catcher or LoopbackCatcher(app_name=app_name)
        self._pkce = generate_pkce_pair()
        self.url = ""

    def start(self) -> str:
        check_allowed()
        self._catcher.start()  # listen before the browser opens
        self.url = authorization_url(self._catcher.redirect_uri, self._pkce.challenge)
        return self.url

    def finish(
        self,
        *,
        cancel_requested: Callable[[], bool] | None = None,
        timeout: float = SIGN_IN_TIMEOUT_S,
    ) -> StoredTokens:
        """Wait for the redirect, exchange the code, store the tokens.

        GATE-9: through :func:`token_request` (``POST /oauth/token``).
        """
        redirect_uri = self._catcher.redirect_uri
        try:
            code = self._catcher.wait(cancel_requested=cancel_requested, timeout=timeout)
        finally:
            self._catcher.close()
        reply = token_request({
            "grant_type": "authorization_code",
            "client_id": client_id(),
            "redirect_uri": redirect_uri,
            "code": code,
            "code_verifier": self._pkce.verifier,
        })
        tokens = self.store.save_reply(reply)
        logger.info("Signed in to Scribe for Documents")
        return tokens

    def cancel(self) -> None:
        self._catcher.close()


def sign_out(store: TokenStore | None = None) -> bool:
    """Revoke the refresh token and wipe the namespace. True if signed in before."""
    store = store or TokenStore()
    tokens = store.load()
    if tokens is None:
        store.wipe()
        return False
    token = tokens.refresh_token or tokens.access_token
    try:
        http.request(
            "POST",
            f"{base_url()}/oauth/revoke",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=urlencode({"client_id": client_id(), "token": token}).encode("ascii"),
            timeout=15.0,
        )
    except ScribeError:
        logger.info("Could not reach Scribe to revoke the sign-in; forgetting it here")
    store.wipe()
    return True


def browser_url(access_token: str, return_to: str = "/settings") -> str:
    """``/auth/browser_from_oauth``: the browser opens already signed in."""
    state = json.dumps({"return_to": return_to})
    return (
        f"{base_url()}/auth/browser_from_oauth?{urlencode({'token': access_token, 'state': state})}"
    )


__all__ = [
    "BASE_URL_ENV",
    "CLIENT_ID",
    "CLIENT_ID_ENV",
    "CLIENT_ID_PLACEHOLDER",
    "REDIRECT_PATH",
    "SECRETS_NAMESPACE",
    "SIGN_IN_TIMEOUT_S",
    "LoopbackCatcher",
    "SignInFlow",
    "StoredTokens",
    "TokenStore",
    "authorization_url",
    "base_url",
    "browser_url",
    "check_allowed",
    "client_id",
    "client_id_configured",
    "safe_mode_active",
    "sign_out",
    "token_request",
]
