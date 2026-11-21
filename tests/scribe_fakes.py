"""A fake ``scribe_client`` module for the Scribe for Documents tests.

Mirrors the real package's surface (scribe-client 0.3.1): the exception
hierarchy, ``AuthClient``, ``PkceChallenge``, ``TokenSet``, ``ScribeClient``
and its document channel. Nothing here opens a socket; a scripted channel
replays events, and every call is recorded so tests can assert, for example,
that a reconnect never uploads the document twice.
"""

from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

from quill.core.secrets import SecretsManager


class MemoryBackend:
    """An in-memory secret vault."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def load(self, cred_name: str) -> str:
        return self.values.get(cred_name, "")

    def save(self, cred_name: str, value: str) -> None:
        self.values[cred_name] = value

    def delete(self, cred_name: str) -> bool:
        return self.values.pop(cred_name, None) is not None


def memory_secrets() -> SecretsManager:
    return SecretsManager(MemoryBackend())


class ScribeApiError(Exception):
    pass


class InvalidGrantError(ScribeApiError):
    pass


class NotFoundError(ScribeApiError):
    pass


class ForbiddenError(ScribeApiError):
    pass


class ConversionNotCompleteError(ScribeApiError):
    pass


class ConversionInProgressError(ScribeApiError):
    pass


class RateLimitedError(ScribeApiError):
    pass


class NeedsPurchaseError(ScribeApiError):
    pass


class PasswordRequiredError(ScribeApiError):
    pass


class TokenSet:
    def __init__(self, access_token: str, refresh_token: str | None, expires_at: int | None):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at


class PkceChallenge:
    def __init__(self) -> None:
        self.verifier = "verifier-123"
        self.challenge = "challenge-abc"


class FakeChannel:
    """Replays ``script``: each item is an event dict, or an exception to raise
    from ``next_event``. ``starts`` is consumed by ``start_conversion`` in the
    same way (an exception to raise, or ``None`` to succeed)."""

    def __init__(self, events: Iterable[Any], starts: Iterable[Any] = ()) -> None:
        self.events = list(events)
        self.starts = list(starts)
        self.started: list[tuple[str, str | None]] = []
        self.closed = False

    def start_conversion(self, fmt: str, password: str | None = None) -> str:
        self.started.append((fmt, password))
        if self.starts:
            outcome = self.starts.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
        return "output-1"

    def next_event(self) -> dict[str, Any]:
        if not self.events:
            raise ScribeApiError("channel closed")
        item = self.events.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


class FakeScribeClient:
    def __init__(self, module: FakeModule, base_url: str, client_id: str, tokens: TokenSet):
        self.module = module
        self.base_url = base_url
        self.client_id = client_id
        self.tokens = tokens

    def create_document_from_file(self, name: str, data: bytes, password: str | None = None):
        self.module.calls.append(("upload", name, len(data), password))
        if self.module.upload_error is not None:
            raise self.module.upload_error
        return "doc-1"

    def create_document_from_url(self, url: str, password: str | None = None):
        self.module.calls.append(("url", url, password))
        return "doc-1"

    def open_document_channel(self, document_id: str) -> FakeChannel:
        self.module.calls.append(("channel", document_id))
        if not self.module.channels:
            raise ScribeApiError("no channel scripted")
        item = self.module.channels.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def list_outputs(self, document_id: str):
        self.module.calls.append(("list_outputs", document_id))
        outputs = [
            SimpleNamespace(format=fmt, stage=stage, progress=1.0)
            for fmt, stage in self.module.outputs
        ]
        return SimpleNamespace(outputs=outputs, is_password_needed=False)

    def download_output(self, document_id: str, fmt: str) -> bytes:
        self.module.calls.append(("download", document_id, fmt))
        return self.module.download_bytes


class AuthClient:
    module: FakeModule

    def __init__(self, base_url: str, client_id: str, locale: str | None = None):
        self.base_url = base_url
        self.client_id = client_id

    def authorization_url(self, redirect_uri: str, pkce: PkceChallenge) -> str:
        self.module.calls.append(("authorize", redirect_uri))
        return f"{self.base_url}/auth/logout?next=/oauth/authorize&redirect_uri={redirect_uri}"

    def exchange_code(self, redirect_uri: str, code: str, verifier: str) -> TokenSet:
        self.module.calls.append(("exchange", redirect_uri, code, verifier))
        if self.module.exchange_error is not None:
            raise self.module.exchange_error
        return TokenSet("access-new", "refresh-new", 2_000_000_000)

    def refresh(self, refresh_token: str) -> TokenSet:
        self.module.calls.append(("refresh", refresh_token))
        if self.module.refresh_error is not None:
            raise self.module.refresh_error
        return TokenSet("access-fresh", "refresh-fresh", 2_000_000_000)


class FakeModule(SimpleNamespace):
    """Stands in for ``import scribe_client``."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[Any, ...]] = []
        self.channels: list[Any] = []
        self.outputs: list[tuple[str, str]] = []
        self.download_bytes = b"<html><body><h1>Converted</h1><p>Hello.</p></body></html>"
        self.upload_error: BaseException | None = None
        self.exchange_error: BaseException | None = None
        self.refresh_error: BaseException | None = None
        for exc in (
            ScribeApiError,
            InvalidGrantError,
            NotFoundError,
            ForbiddenError,
            ConversionNotCompleteError,
            ConversionInProgressError,
            RateLimitedError,
            NeedsPurchaseError,
            PasswordRequiredError,
        ):
            setattr(self, exc.__name__, exc)
        self.TokenSet = TokenSet
        self.PkceChallenge = PkceChallenge
        module = self

        class _Auth(AuthClient):
            pass

        _Auth.module = module
        self.AuthClient = _Auth
        self.ScribeClient = lambda base, cid, tokens, locale=None: FakeScribeClient(
            module, base, cid, tokens
        )


def status(fmt: str, stage: str, progress: float) -> dict[str, Any]:
    return {"type": "status", "format": fmt, "stage": stage, "progress": progress}


def complete(fmt: str) -> dict[str, Any]:
    return {"type": "conversion_complete", "format": fmt, "output_id": f"out-{fmt}"}
