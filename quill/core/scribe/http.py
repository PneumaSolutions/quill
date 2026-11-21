"""The one HTTP door every Scribe REST call goes through (wx-free).

Plain ``urllib``, so GATE-9's scan sees every call: :func:`request` is the
only place this package opens a URL (``quill/tools/network_egress_entries_scribe.py``
records it with its reason). HTTPS uses :func:`quill.core.net.verified_ssl_context`;
plain HTTP is allowed only to the loopback address the tests' fake server
listens on.

Redirects are never followed automatically: a download answers with a 302
to a presigned storage URL, and that second request must go **without** the
Scribe bearer token (``client.py`` makes it).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from quill.core.scribe.model import ScribeError, ScribeOffline, error_for

logger = logging.getLogger(__name__)

TIMEOUT_S = 60.0
USER_AGENT = "QUILL (Scribe for Documents client)"


@dataclass(slots=True)
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    stream: BinaryIO | None = None  # set when ``stream=True``; the caller closes it

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body.decode("utf-8", errors="replace"))
        except ValueError:
            return {}

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _check_scheme(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and parts.hostname in ("127.0.0.1", "::1", "localhost"):
        return
    raise ScribeError(f"QUILL only talks to Scribe for Documents over HTTPS, not {url!r}.")


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | BinaryIO | None = None,
    timeout: float = TIMEOUT_S,
    stream: bool = False,
) -> Response:
    """One HTTP request. Returns any status (the caller decides); raises
    :class:`ScribeOffline` only when no answer came back at all.

    GATE-9: the single Scribe network call site. Reached only from a Scribe
    command the person chose (sign-in, convert, My Scribe Documents, Options),
    on a worker thread, never in Safe Mode.
    """
    from quill.core.net import context_for

    _check_scheme(url)
    all_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    req = urllib.request.Request(url, data=body, method=method, headers=all_headers)
    handlers: list[Any] = [_NoRedirect()]
    context = context_for(url)
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    try:
        raw = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raw = exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        logger.info("Scribe request failed to connect method=%s", method)
        raise ScribeOffline(
            "QUILL could not reach Scribe for Documents. Check your internet connection.",
            server_code="offline",
        ) from exc
    status = int(getattr(raw, "status", None) or raw.getcode() or 0)
    found = {k.lower(): v for k, v in (raw.headers.items() if raw.headers else [])}
    if stream and 200 <= status < 300:
        return Response(status, found, b"", raw)
    try:
        data = raw.read()
    finally:
        raw.close()
    return Response(status, found, data)


def raise_for(response: Response, *, what: str = "") -> None:
    """Turn an error status into the coded error, keeping the server's sentence.

    The envelope is ``{"error": {"code": "...", "message": "..."}}``; the OAuth
    token endpoint answers ``{"error": "...", "error_description": "..."}``.
    """
    if response.status < 400:
        return
    data = response.json()
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code") or "")
        message = str(error.get("message") or "")
    else:
        code = str(error or "")
        message = (
            str(data.get("error_description") or data.get("message") or "")
            if isinstance(data, dict)
            else ""
        )
    if not code:
        code = {
            401: "unauthenticated",
            403: "forbidden",
            404: "not_found",
            429: "rate_limited",
        }.get(response.status, "server_error")
    if not message:
        message = _default_message(code, what)
    raise error_for(code, message, status=response.status)


def _default_message(code: str, what: str) -> str:
    subject = f" {what}" if what else ""
    return {
        "unauthenticated": (
            "Your Scribe for Documents sign-in has ended. Conversions are paused "
            "until you sign in again."
        ),
        "invalid_grant": (
            "Your Scribe for Documents sign-in has ended. Conversions are paused "
            "until you sign in again."
        ),
        "not_found": "That document is no longer in your Scribe account.",
        "forbidden": "That document is no longer in your Scribe account.",
        "rate_limited": "Scribe for Documents asked QUILL to slow down.",
    }.get(code, f"Scribe for Documents could not finish{subject}.")


def multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes]]) -> tuple[bytes, str]:
    """A ``multipart/form-data`` body and its content type."""
    import secrets

    boundary = "quill-" + secrets.token_hex(12)
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            + value.encode("utf-8")
            + b"\r\n"
        )
    for name, (filename, data) in files.items():
        safe = filename.replace('"', "'").replace("\r", " ").replace("\n", " ")
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{safe}"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode()
            + data
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class MultipartFile:
    """A ``multipart/form-data`` upload read from disk as it is sent (Q18).

    :func:`multipart` builds the whole body in memory -- two or three copies of
    a 300 MB scan. This sends the same bytes in pieces: :meth:`open` gives a
    fresh stream (a retry after a 401 starts again) and :attr:`length` is the
    exact ``Content-Length``.
    """

    def __init__(self, fields: dict[str, str], name: str, path: Any) -> None:
        import secrets
        from pathlib import Path

        self.path = Path(path)
        boundary = "quill-" + secrets.token_hex(12)
        self.content_type = f"multipart/form-data; boundary={boundary}"
        lead = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
            + v.encode("utf-8")
            + b"\r\n"
            for k, v in fields.items()
        )
        safe = self.path.name.replace('"', "'").replace("\r", " ").replace("\n", " ")
        self._prefix = (
            lead
            + (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{safe}"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode()
        )
        self._suffix = f"\r\n--{boundary}--\r\n".encode()
        self.length = len(self._prefix) + self.path.stat().st_size + len(self._suffix)

    def open(self) -> BinaryIO:
        return _Chained([self._prefix, self.path, self._suffix])  # type: ignore[return-value]


class _Chained:
    """Bytes, then a file's contents, then bytes, read in order."""

    def __init__(self, parts: list[Any]) -> None:
        self._parts = parts
        self._handle: Any = None
        self._buffer = b""

    def _next(self) -> bool:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        if not self._parts:
            return False
        part = self._parts.pop(0)
        if isinstance(part, bytes):
            self._buffer = part
        else:
            self._handle = open(part, "rb")  # noqa: SIM115 - closed in _next/close
        return True

    def read(self, size: int = -1) -> bytes:
        while True:
            if self._buffer:
                take = self._buffer if size < 0 else self._buffer[:size]
                self._buffer = self._buffer[len(take) :]
                return take
            if self._handle is not None:
                data: bytes = self._handle.read(size if size > 0 else 1024 * 1024)
                if data:
                    return data
            if not self._next():
                return b""

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


__all__ = ["TIMEOUT_S", "MultipartFile", "Response", "multipart", "raise_for", "request"]
