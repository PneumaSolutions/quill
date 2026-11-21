"""A document's live channel on Scribe for Documents (wx-free; PRD 3.5, 7.1).

Phoenix channels over a WebSocket (``websockets``' synchronous client, already
in QUILL's shared runtime for live dictation): connect to
``/socket/websocket?vsn=2.0.0&token=<access token>``, join
``documents:<id>``, send ``start_conversion``, and read ``status``,
``chunk``, ``conversion_complete``, ``error`` and ``password_required``
pushes. Frames are ``[join_ref, ref, topic, event, payload]``. A heartbeat
goes every 30 seconds while QUILL waits (the server drops a silent socket
after about 66).

If the connection drops, the conversion carries on at Scribe: open a new
channel on the same document and never upload again (:meth:`reconnect`).

GATE-9: :func:`connect` is the one WebSocket call site; the egress audit
records it with its reason.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from quill.core.scribe.auth import TokenStore, base_url, check_allowed
from quill.core.scribe.model import (
    ScribeError,
    ScribeOffline,
    ScribePasswordNeeded,
    error_for,
)

logger = logging.getLogger(__name__)

HEARTBEAT_S = 30.0
REPLY_TIMEOUT_S = 30.0
_RECV_SLICE_S = 1.0

Connector = Callable[[str], Any]


def socket_url(token: str, locale: str = "") -> str:
    parts = urlsplit(base_url())
    scheme = "wss" if parts.scheme == "https" else "ws"
    query = {"vsn": "2.0.0", "token": token}
    if locale:
        query["locale"] = locale
    return urlunsplit((scheme, parts.netloc, "/socket/websocket", urlencode(query), ""))


def connect(url: str) -> Any:
    """A WebSocket to Scribe, certificate-checked.

    GATE-9: the single Scribe WebSocket call site. Reached only while a
    conversion the person started is running, on a worker thread.
    """
    from quill.core.net import context_for

    try:
        from websockets.sync.client import (  # type: ignore[import-not-found,unused-ignore]
            connect as ws_connect,
        )
    except ImportError as error:
        raise ScribeError(
            "This copy of QUILL is missing the part it needs to follow a Scribe "
            "for Documents conversion (websockets)."
        ) from error
    https_form = url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
    try:
        return ws_connect(
            url,
            ssl=context_for(https_form),
            open_timeout=20,
            max_size=None,
            logger=_quiet_logger(),
        )
    except OSError as exc:
        raise ScribeOffline(
            "QUILL could not reach Scribe for Documents. Check your internet connection.",
            server_code="offline",
        ) from exc
    except Exception as exc:  # noqa: BLE001 - a refused handshake is a bad token
        if "401" in str(exc) or "403" in str(exc):
            raise error_for("unauthenticated", "") from exc
        raise ScribeOffline(
            "QUILL could not reach Scribe for Documents. Check your internet connection.",
            server_code="offline",
        ) from exc


def _quiet_logger() -> logging.Logger:
    """``websockets`` logs frames and the request line at DEBUG, and both carry
    secrets here (the token in the URL, a PDF password in ``start_conversion``).
    Its logger for Scribe never goes below WARNING (PRD 10.2)."""
    quiet = logging.getLogger("quill.core.scribe.websocket")
    quiet.setLevel(logging.WARNING)
    return quiet


class DocumentChannel:
    """``documents:<id>``, joined. Read events with :meth:`next_event`."""

    def __init__(
        self,
        document_id: str,
        *,
        store: TokenStore | None = None,
        connector: Connector | None = None,
        clock: Callable[[], float] = time.monotonic,
        locale: str = "",
    ) -> None:
        self.document_id = document_id
        self.topic = f"documents:{document_id}"
        self._store = store or TokenStore()
        self._connector = connector or connect
        self._clock = clock
        self._locale = locale
        self._ws: Any = None
        self._ref = 1
        self._join_ref = "1"
        self._pending: deque[dict[str, Any]] = deque()
        self._next_heartbeat = 0.0

    # ------------------------------------------------------------ lifecycle

    def open(self) -> DocumentChannel:
        check_allowed()
        token = self._store.access_token()
        ws = self._connector(socket_url(token, self._locale))
        # websockets 17 wants its connection entered as a context manager;
        # entering returns the same connection on every version.
        enter = getattr(ws, "__enter__", None)
        self._ws = enter() if callable(enter) else ws
        self._ref = 1
        self._join_ref = "1"
        self._next_heartbeat = self._clock() + HEARTBEAT_S
        reply = self._push("phx_join", {}, join=True)
        self._check_reply(reply, joining=True)
        return self

    def reconnect(self) -> None:
        """Open a fresh socket and join again; the conversion carried on meanwhile."""
        self.close()
        self.open()

    def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - closing a dead socket is not an error
                pass

    def __enter__(self) -> DocumentChannel:
        return self.open()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ requests

    def start_conversion(self, format_id: str, password: str | None = None) -> str:
        """Ask Scribe for one format. Returns the output id.

        Raises :class:`ScribePasswordNeeded` (``password_required``),
        :class:`~quill.core.scribe.model.ScribeNeedsPages` (``needs_purchase``,
        with ``extra['purchase_url']``), :class:`~quill.core.scribe.model.ScribeBusy`,
        or :class:`ScribeError` with ``server_code='conversion_in_progress'``
        (QUILL waits; never shown as an error).
        """
        payload: dict[str, Any] = {"format": format_id}
        if password:
            payload["password"] = password
        reply = self._push("start_conversion", payload)
        response = self._check_reply(reply)
        return str(response.get("output_id") or "")

    def next_event(self, timeout: float = 5.0) -> dict[str, Any] | None:
        """The next push as ``{"type": event, **payload}``, or ``None`` on timeout.

        Sends heartbeats while it waits. Raises :class:`ScribeOffline` when the
        connection is lost; call :meth:`reconnect` and carry on.
        """
        if self._pending:
            return self._pending.popleft()
        deadline = self._clock() + timeout
        while True:
            frame = self._recv_frame(min(_RECV_SLICE_S, max(0.0, deadline - self._clock())))
            if frame is not None:
                event = self._as_event(frame)
                if event is not None:
                    return event
            if self._clock() >= deadline:
                return None

    # ------------------------------------------------------------ the wire

    def _push(self, event: str, payload: dict[str, Any], *, join: bool = False) -> dict[str, Any]:
        self._ref += 1 if not join else 0
        ref = str(self._ref)
        frame = [self._join_ref, ref, self.topic, event, payload]
        self._send(frame)
        deadline = self._clock() + REPLY_TIMEOUT_S
        while self._clock() < deadline:
            raw = self._recv_frame(_RECV_SLICE_S)
            if raw is None:
                continue
            _join, reply_ref, topic, name, body = raw
            if name == "phx_reply" and reply_ref == ref and topic == self.topic:
                return body if isinstance(body, dict) else {}
            event_dict = self._as_event(raw)
            if event_dict is not None:
                self._pending.append(event_dict)
        raise ScribeOffline(
            "Scribe for Documents did not answer. Reconnecting.", server_code="timeout"
        )

    def _check_reply(self, reply: dict[str, Any], *, joining: bool = False) -> dict[str, Any]:
        raw = reply.get("response")
        response: dict[str, Any] = raw if isinstance(raw, dict) else {}
        if reply.get("status") == "ok":
            return response or {}
        reason = str(response.get("reason") or "server_error")
        message = str(response.get("message") or "")
        if reason == "password_required":
            raise ScribePasswordNeeded(
                message or "This document needs its password.", server_code=reason, wrong=False
            )
        if reason == "conversion_in_progress":
            raise ScribeError(message or "A conversion is already running.", server_code=reason)
        extra: dict[str, Any] = {}
        if response.get("purchase_url"):
            extra["purchase_url"] = str(response["purchase_url"])
        raise error_for(reason, message, **extra)

    def _send(self, frame: list[Any]) -> None:
        if self._ws is None:
            raise ScribeOffline(
                "The Scribe for Documents channel is closed.", server_code="offline"
            )
        try:
            self._ws.send(json.dumps(frame))
        except Exception as exc:  # noqa: BLE001 - any send failure is a lost connection
            raise ScribeOffline(
                "Reconnecting to Scribe for Documents; your conversion is still running there.",
                server_code="offline",
            ) from exc

    def _heartbeat_if_due(self) -> None:
        if self._clock() >= self._next_heartbeat:
            self._ref += 1
            self._send([None, str(self._ref), "phoenix", "heartbeat", {}])
            self._next_heartbeat = self._clock() + HEARTBEAT_S

    def _recv_frame(self, timeout: float) -> list[Any] | None:
        self._heartbeat_if_due()
        if self._ws is None:
            raise ScribeOffline(
                "The Scribe for Documents channel is closed.", server_code="offline"
            )
        try:
            raw = self._ws.recv(timeout=timeout)
        except TimeoutError:
            return None
        except Exception as exc:  # noqa: BLE001 - ConnectionClosed and friends
            raise ScribeOffline(
                "Reconnecting to Scribe for Documents; your conversion is still running there.",
                server_code="offline",
            ) from exc
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if isinstance(frame, list) and len(frame) == 5:
            return frame
        return None

    def _as_event(self, frame: list[Any]) -> dict[str, Any] | None:
        _join, _ref, topic, name, payload = frame
        if topic != self.topic or name in ("phx_reply", "phx_close"):
            if name == "phx_error" and topic == self.topic:
                raise ScribeOffline(
                    "The Scribe for Documents channel closed.", server_code="offline"
                )
            return None
        if name == "phx_error":
            raise ScribeOffline("The Scribe for Documents channel closed.", server_code="offline")
        event = {"type": str(name)}
        if isinstance(payload, dict):
            event.update(payload)
        return event


__all__ = ["HEARTBEAT_S", "DocumentChannel", "connect", "socket_url"]
