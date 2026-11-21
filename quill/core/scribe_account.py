"""Scribe for Documents: the account side (wx-free).

Who QUILL is to Scribe (the client ID and loopback redirect), the person's
sign-in (the OAuth token set, kept in the OS secret vault through
:mod:`quill.core.secrets`, never in ``settings.json``), the errors every Scribe
step raises, and the authenticated client. Split from
:mod:`quill.core.scribe_documents` (GATE-11), which re-exports all of it.

The client ID: Scribe registers each app at https://scribeit.io/developer as a
public PKCE client (no secret). Pneuma Solutions registered "QUILL" there
(Windows and macOS, approved 2026-11-20) and :data:`SCRIBE_CLIENT_ID` holds
its public ID. If the ID is ever the placeholder, the feature says the build
is not registered instead of opening a sign-in page that would fail.
``QUILL_SCRIBE_CLIENT_ID`` overrides it for development and testing.

GATE-9 / network egress: :func:`_make_client` is one of the two Scribe entries
in ``_NON_AST_EGRESS`` (``scribe-client`` networks inside a compiled
extension, out of the AST scan's sight).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from quill.core.error_codes import CodedError
from quill.core.secrets import SecretRef, SecretsManager

SCRIBE_BASE_URL = "https://scribeit.io"

#: Shown as "not registered" if SCRIBE_CLIENT_ID is ever reset to it.
SCRIBE_CLIENT_ID_PLACEHOLDER = "REPLACE-WITH-QUILL-CLIENT-ID"
SCRIBE_CLIENT_ID = "8b535f02eb781e4ccab66bf88b93fc47a8605303bdf352d197788812bbb140da"
SCRIBE_CLIENT_ID_ENV = "QUILL_SCRIBE_CLIENT_ID"

#: The loopback redirect registered for QUILL. Scribe only redirects to a URI
#: that matches a registered one exactly, so the port is fixed.
SCRIBE_REDIRECT_PORT = 47613
SCRIBE_REDIRECT_PATH = "/callback"
SCRIBE_REDIRECT_URI = f"http://127.0.0.1:{SCRIBE_REDIRECT_PORT}{SCRIBE_REDIRECT_PATH}"

#: The attribution Scribe's developer policy asks every integration to show.
POWERED_BY = "Powered by Scribe for Documents"
SERVICE_NAME = "Scribe for Documents"

SCRIBE_LINKS: dict[str, str] = {
    "website": "https://scribeit.io",
    "developer": "https://scribeit.io/developer",
}

_TOKENS_REF = SecretRef("scribe", "tokens")
_SECRETS_NAMESPACE = "scribe"

#: Refresh a stored token this many seconds before it expires.
_REFRESH_MARGIN_S = 600


# ------------------------------------------------------------------ errors


class ScribeDocumentsError(CodedError):
    """A Scribe step failed, with a user-ready message free of secrets."""

    code = "QUILL-CONVERT-SCRIBE-FAILED"


class ScribeSignInRequired(ScribeDocumentsError):
    """No usable sign-in: never signed in, signed out, or the grant expired."""

    code = "QUILL-CONVERT-SCRIBE-SIGNIN"


class ScribeCancelled(ScribeDocumentsError):
    """The person stopped waiting. The conversion may carry on in Scribe."""

    code = "QUILL-CONVERT-SCRIBE-CANCELLED"


# ------------------------------------------------------------- client id


def client_id() -> str:
    """The registered client ID, or the development override."""
    override = os.environ.get(SCRIBE_CLIENT_ID_ENV, "").strip()
    return override or SCRIBE_CLIENT_ID


def client_id_configured() -> bool:
    """False while this build still carries the placeholder."""
    value = client_id()
    return bool(value) and value != SCRIBE_CLIENT_ID_PLACEHOLDER


def safe_mode_active() -> bool:
    return os.environ.get("QUILL_SAFE_MODE") == "1"


# ----------------------------------------------------------------- tokens


@dataclass(frozen=True, slots=True)
class StoredTokens:
    """The OAuth token set, as kept in the secret vault."""

    access_token: str
    refresh_token: str
    expires_at: int | None = None

    def expires_soon(self, now: float) -> bool:
        return self.expires_at is not None and self.expires_at - now < _REFRESH_MARGIN_S

    @classmethod
    def from_token_set(cls, tokens: Any) -> StoredTokens:
        expires = getattr(tokens, "expires_at", None)
        return cls(
            access_token=str(getattr(tokens, "access_token", "") or ""),
            refresh_token=str(getattr(tokens, "refresh_token", "") or ""),
            expires_at=int(expires) if expires is not None else None,
        )


def _secrets(manager: SecretsManager | None) -> SecretsManager:
    return manager if manager is not None else SecretsManager()


def load_tokens(manager: SecretsManager | None = None) -> StoredTokens | None:
    """The stored token set, or ``None`` when signed out or unreadable."""
    try:
        data = _secrets(manager).get_json(_TOKENS_REF)
    except Exception:  # noqa: BLE001 - a locked or missing vault is "signed out"
        return None
    if not data or not data.get("access_token"):
        return None
    expires = data.get("expires_at")
    return StoredTokens(
        access_token=str(data.get("access_token", "")),
        refresh_token=str(data.get("refresh_token", "") or ""),
        expires_at=int(expires) if isinstance(expires, (int, float)) else None,
    )


def save_tokens(tokens: StoredTokens, manager: SecretsManager | None = None) -> None:
    _secrets(manager).set_json(
        _TOKENS_REF,
        {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_at": tokens.expires_at,
        },
    )


def signed_in(manager: SecretsManager | None = None) -> bool:
    return load_tokens(manager) is not None


def sign_out(manager: SecretsManager | None = None) -> bool:
    """Forget the stored sign-in. Returns True when there was one."""
    had = signed_in(manager)
    try:
        _secrets(manager).wipe_namespace(_SECRETS_NAMESPACE)
    except Exception:  # noqa: BLE001 - nothing stored is the same as signed out
        return False
    return had


# ----------------------------------------------------------------- client


def client_module() -> Any:
    """Import ``scribe_client``; tests replace this with a fake."""
    try:
        import scribe_client
    except ImportError:
        raise ScribeDocumentsError(
            "This copy of QUILL does not include the Scribe for Documents client "
            "(the scribe-client package), so it cannot convert with Scribe."
        ) from None
    return scribe_client


def _make_client(
    module: Any,
    *,
    manager: SecretsManager | None = None,
    now: Callable[[], float] = time.time,
) -> Any:
    """A ``ScribeClient`` on the stored tokens, refreshed first if needed.

    GATE-9 (``_NON_AST_EGRESS``): the client this returns uploads, converts
    and downloads over HTTPS and a WebSocket to :data:`SCRIBE_BASE_URL`. The
    refresh is explicit because the Python binding does not hand back tokens
    it refreshes on its own, and a refreshed set that is never stored would
    leave the vault holding a spent refresh token.
    """
    if safe_mode_active():
        raise ScribeDocumentsError("Scribe for Documents is off in Safe Mode.")
    if not client_id_configured():
        raise ScribeDocumentsError(
            "This copy of QUILL is not registered with Scribe for Documents yet."
        )
    tokens = load_tokens(manager)
    if tokens is None:
        raise ScribeSignInRequired("Sign in to Scribe for Documents first.")
    if tokens.expires_soon(now()) and tokens.refresh_token:
        auth = module.AuthClient(SCRIBE_BASE_URL, client_id())
        try:
            fresh = auth.refresh(tokens.refresh_token)
        except module.InvalidGrantError:
            sign_out(manager)
            raise ScribeSignInRequired(
                "Your Scribe for Documents sign-in has expired. Sign in again."
            ) from None
        except module.ScribeApiError as exc:
            raise ScribeDocumentsError(_connection_sentence(exc)) from None
        tokens = StoredTokens.from_token_set(fresh)
        save_tokens(tokens, manager)
    token_set = module.TokenSet(tokens.access_token, tokens.refresh_token, tokens.expires_at)
    return module.ScribeClient(SCRIBE_BASE_URL, client_id(), token_set)


def _connection_sentence(exc: BaseException) -> str:
    return (
        "QUILL could not reach Scribe for Documents. Check your internet "
        f"connection and try again. ({exc})"
    )


def friendly_error(
    module: Any, exc: BaseException, manager: SecretsManager | None = None
) -> ScribeDocumentsError:
    """Map a ``scribe_client`` exception to a sentence a person can act on."""
    if isinstance(exc, ScribeDocumentsError):
        return exc
    detail = str(exc).strip()
    if isinstance(exc, module.NeedsPurchaseError):
        return ScribeDocumentsError(
            "Your Scribe for Documents account does not have enough page credits "
            "for this conversion. Buy pages or a subscription at scribeit.io, then "
            f"try again. ({detail})"
        )
    if isinstance(exc, module.InvalidGrantError):
        sign_out(manager)
        return ScribeSignInRequired(
            "Your Scribe for Documents sign-in is no longer valid. Sign in again."
        )
    if isinstance(exc, module.RateLimitedError):
        return ScribeDocumentsError(
            "Scribe for Documents is busy with requests from your account. Wait a "
            "minute and try again."
        )
    if isinstance(exc, (module.NotFoundError, module.ForbiddenError)):
        return ScribeDocumentsError(
            "Scribe for Documents could not find that document in your account. "
            "It may have been deleted. Start the conversion again."
        )
    return ScribeDocumentsError(f"Scribe for Documents reported a problem: {detail}")


__all__ = [
    "POWERED_BY",
    "SCRIBE_BASE_URL",
    "SCRIBE_CLIENT_ID",
    "SCRIBE_CLIENT_ID_ENV",
    "SCRIBE_CLIENT_ID_PLACEHOLDER",
    "SCRIBE_LINKS",
    "SCRIBE_REDIRECT_PATH",
    "SCRIBE_REDIRECT_PORT",
    "SCRIBE_REDIRECT_URI",
    "SERVICE_NAME",
    "ScribeCancelled",
    "ScribeDocumentsError",
    "ScribeSignInRequired",
    "StoredTokens",
    "client_id",
    "client_id_configured",
    "client_module",
    "friendly_error",
    "load_tokens",
    "safe_mode_active",
    "save_tokens",
    "sign_out",
    "signed_in",
]
