"""A fake Scribe for Documents server for tests (Scribe PRD section 13).

REST on a real ``http.server`` and the Phoenix channel on a real
``websockets`` server, both on ``127.0.0.1``, so QUILL's own client, channel
and runner run unmodified against it. Shapes are the real server's
(pneumalith ``document_controller.ex`` / ``documents_channel.ex``, read
2026-11-21). Behaviour is scripted per test: stages, chunks, errors, a
dropped socket, ``password_required``, ``needs_purchase``, ``rate_limited``,
an expired access token.

Use :func:`running_fake` (a context manager), point QUILL at it with
``QUILL_SCRIBE_BASE_URL`` (honoured in dev builds, which tests are), and pass
``fake.connector`` to :class:`~quill.core.scribe.channel.DocumentChannel`
(the WebSocket listens on its own port).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit


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


def memory_store(signed_in: bool = True) -> Any:
    """A TokenStore on an in-memory vault, signed in to the fake by default."""
    from quill.core.scribe.auth import StoredTokens, TokenStore
    from quill.core.secrets import SecretsManager

    store = TokenStore(SecretsManager(MemoryBackend()))
    if signed_in:
        store.save(StoredTokens("access-1", "refresh-1", 4_000_000_000))
    return store


DEFAULT_SCRIPT = (
    ("status", "queue", 0.0),
    ("status", "convert", 0.5),
    ("status", "add_image_descriptions", 0.8),
    ("status", "complete", 1.0),
)


@dataclass
class FakeDocument:
    id: str
    title: str
    page_count: int = 3
    password: str = ""
    unlocked: bool = True
    outputs: dict[str, str] = field(default_factory=dict)  # format -> stage
    trashed: bool = False
    settings: dict[str, Any] = field(default_factory=dict)
    data: bytes = b""


class FakeScribe:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.uploads: list[dict[str, Any]] = []
        self.documents: dict[str, FakeDocument] = {}
        self.valid_tokens: set[str] = {"access-1"}
        self.refresh_tokens: set[str] = {"refresh-1"}
        self.revoked: list[str] = []
        self.pages_remaining = 100
        self.account = {
            "first_name": "Mike",
            "last_name": "Calvo",
            "email": "mike@example.org",
            "organization_name": None,
        }
        self.scripts: dict[str, list[tuple[Any, ...]]] = {}
        self.start_errors: list[dict[str, Any]] = []  # replies to the next start_conversion
        self.upload_error: tuple[int, str, str] | None = None
        self.drop_after_frames: int | None = None
        self.password_for_next_upload = ""
        self.expire_next = False
        self.started: list[str] = []
        self.storage_drops: list[int] = []  # per storage request: hang up after n bytes
        self.storage_override: dict[str, bytes] = {}  # format -> the bytes storage serves
        self.storage_requests: list[dict[str, str]] = []
        self.ws_port = 0
        self.http_port = 0
        self._token_counter = 1
        self._lock = threading.Lock()

    # -------------------------------------------------------------- urls

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    def connector(self, url: str) -> Any:
        """Connect to the fake channel, whatever host the URL names."""
        from websockets.sync.client import connect

        parts = urlsplit(url)
        from quill.core.scribe.channel import _quiet_logger

        return connect(
            f"ws://127.0.0.1:{self.ws_port}{parts.path}?{parts.query}",
            open_timeout=5,
            logger=_quiet_logger(),
        )

    def add_document(self, title: str, **kwargs: Any) -> FakeDocument:
        doc = FakeDocument(id=uuid.uuid4().hex, title=title, **kwargs)
        self.documents[doc.id] = doc
        return doc

    def bytes_for(self, doc: FakeDocument, fmt: str) -> bytes:
        if fmt == "offline_html":
            import io
            import zipfile

            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr(
                    "index.html",
                    f"<html><body><h1>{doc.title}</h1><p>Converted.</p></body></html>",
                )
                archive.writestr("images/a.png", b"png")
            return buffer.getvalue()
        return f"{fmt.upper()} of {doc.title}".encode()

    def new_token(self) -> tuple[str, str]:
        with self._lock:
            self._token_counter += 1
            access, refresh = f"access-{self._token_counter}", f"refresh-{self._token_counter}"
            self.valid_tokens.add(access)
            self.refresh_tokens.add(refresh)
            return access, refresh


# ------------------------------------------------------------------ REST


def _handler(fake: FakeScribe) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a: Any) -> None:
            return

        def _send(
            self, status: int, body: Any = None, headers: dict[str, str] | None = None
        ) -> None:
            data = (
                b""
                if body is None
                else (body if isinstance(body, bytes) else json.dumps(body).encode())
            )
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            if body is not None and not isinstance(body, bytes):
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _error(self, status: int, code: str, message: str = "", **extra: Any) -> None:
            self._send(status, {"error": {"code": code, "message": message or code, **extra}})

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _authed(self) -> bool:
            token = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            if fake.expire_next and token in fake.valid_tokens:
                fake.expire_next = False
                fake.valid_tokens.discard(token)
            if token not in fake.valid_tokens:
                self._error(401, "unauthenticated", "Sign in again.")
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            self._route("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._route("POST")

        def do_PATCH(self) -> None:  # noqa: N802
            self._route("PATCH")

        def do_DELETE(self) -> None:  # noqa: N802
            self._route("DELETE")

        def _route(self, method: str) -> None:
            path = urlsplit(self.path).path
            fake.calls.append((method, path))
            body = self._body() if method in ("POST", "PATCH") else b""
            if path == "/oauth/token":
                return self._token(parse_qs(body.decode()))
            if path == "/oauth/revoke":
                fake.revoked.append(parse_qs(body.decode()).get("token", [""])[0])
                return self._send(200, {})
            if path.startswith("/storage/"):
                return self._storage(path)
            if path.startswith("/sounds/tts-samples/"):  # public: no token
                return self._send(200, b"ID3 sample of " + path.rsplit("/", 1)[1].encode())
            if not path.startswith("/api/") or not self._authed():
                return None if path.startswith("/api/") else self._error(404, "not_found")
            return self._api(method, path, body)

        def _token(self, form: dict[str, list[str]]) -> None:
            grant = form.get("grant_type", [""])[0]
            if grant == "authorization_code" and form.get("code", [""])[0] == "good-code":
                access, refresh = fake.new_token()
            elif (
                grant == "refresh_token"
                and form.get("refresh_token", [""])[0] in fake.refresh_tokens
            ):
                access, refresh = fake.new_token()
            else:
                return self._send(400, {"error": "invalid_grant", "error_description": "No."})
            self._send(
                200,
                {
                    "access_token": access,
                    "refresh_token": refresh,
                    "expires_in": 7200,
                    "token_type": "Bearer",
                },
            )
            return None

        def _storage(self, path: str) -> None:
            """Presigned storage: ETag, Range with If-Range, 416, and scripted drops."""
            import hashlib

            _, _, doc_id, fmt = path.split("/", 3)
            data = fake.storage_override.get(fmt) or fake.bytes_for(fake.documents[doc_id], fmt)
            etag = '"' + hashlib.sha256(data).hexdigest()[:16] + '"'
            fake.storage_requests.append(dict(self.headers))
            start = 0
            match = re.match(r"bytes=(\d+)-", self.headers.get("Range") or "")
            if_range = self.headers.get("If-Range")
            if match and (not if_range or if_range == etag):
                start = int(match.group(1))
                if start >= len(data):
                    self._send(416, b"", {"Content-Range": f"bytes */{len(data)}", "ETag": etag})
                    return
            body = data[start:]
            status = 206 if start else 200
            headers = {"ETag": etag}
            if start:
                headers["Content-Range"] = f"bytes {start}-{len(data) - 1}/{len(data)}"
            cut = fake.storage_drops.pop(0) if fake.storage_drops else None
            if cut is None:
                self._send(status, body, headers)
                return
            # Announce the whole length, send only ``cut`` bytes, then hang up.
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body[:cut])
            self.wfile.flush()
            self.close_connection = True

        def _api(self, method: str, path: str, body: bytes) -> None:
            if path == "/api/account_info":
                return self._send(200, fake.account)
            if path == "/api/billing":
                return self._send(
                    200, {"pages_remaining": fake.pages_remaining, "subscription": None}
                )
            if path == "/api/documents" and method == "GET":
                docs = [_doc_json(d) for d in fake.documents.values() if not d.trashed]
                return self._send(
                    200,
                    {
                        "documents": docs,
                        "pages_remaining": fake.pages_remaining,
                        "suggest_buying_pages": False,
                    },
                )
            if path == "/api/documents" and method == "POST":
                return self._upload(body)
            if path == "/api/documents/trash":
                docs = [
                    {
                        **_doc_json(d),
                        "trashed_at": "2026-11-21",
                        "permanently_delete_at": "2026-11-28",
                    }
                    for d in fake.documents.values()
                    if d.trashed
                ]
                return self._send(200, {"documents": docs})
            if path.startswith("/api/settings/"):
                return self._send(200, _SETTINGS_LISTS[path.rsplit("/", 1)[1]])
            if path == "/api/document_settings":
                return self._send(200, {"language": "en", "tts_rate": 1.0})
            match = re.match(r"/api/documents/([^/]+)(/.*)?$", path)
            if not match or match.group(1) not in fake.documents:
                return self._error(404, "not_found", "That document is not in your Scribe account.")
            doc, rest = fake.documents[match.group(1)], match.group(2) or ""
            return self._document(method, doc, rest, body)

        def _upload(self, body: bytes) -> None:
            if fake.upload_error is not None:
                status, code, message = fake.upload_error
                fake.upload_error = None
                return self._error(status, code, message)
            text = body.decode("latin-1")
            name = re.search(r'name="document\[file\]"; filename="([^"]*)"', text)
            url = re.search(r'name="document\[url\]"\r\n\r\n([^\r]*)', text)
            password = re.search(r'name="document\[password\]"\r\n\r\n([^\r]*)', text)
            title = name.group(1) if name else (url.group(1) if url else "Untitled")
            fake.uploads.append({
                "title": title,
                "password": password.group(1) if password else None,
                "url": url.group(1) if url else None,
            })
            doc = fake.add_document(title.rsplit(".", 1)[0], password=fake.password_for_next_upload)
            doc.unlocked = not doc.password or bool(password and password.group(1) == doc.password)
            return self._send(201, {"document_id": doc.id})

        def _document(self, method: str, doc: FakeDocument, rest: str, body: bytes) -> None:
            if rest == "" and method == "PATCH":
                doc.title = json.loads(body or b"{}").get("document", {}).get("title", doc.title)
                return self._send(200, _doc_json(doc))
            if rest == "" and method == "DELETE":
                if not doc.trashed:
                    return self._error(409, "not_trashed", "Move it to the trash first.")
                del fake.documents[doc.id]
                return self._send(204)
            if rest in ("/trash", "/recover"):
                doc.trashed = rest == "/trash"
                return self._send(204)
            if rest == "/outputs":
                outputs = [_output_json(f, s) for f, s in doc.outputs.items()]
                return self._send(200, {"outputs": outputs, "is_password_needed": not doc.unlocked})
            if rest == "/settings":
                if method == "PATCH":
                    doc.settings.update(json.loads(body or b"{}").get("settings", {}))
                return self._send(200, doc.settings)
            if rest == "/feedback":
                return self._send(200, {"ok": True})
            match = re.match(r"/outputs/([a-z_]+)/download$", rest)
            if match:
                fmt = match.group(1)
                if doc.outputs.get(fmt) != "complete" or fmt in ("html", "html_stream"):
                    return self._error(404, "not_found", "Not converted yet.")
                return self._send(302, b"", {"Location": f"{fake.base_url}/storage/{doc.id}/{fmt}"})
            return self._error(404, "not_found")

    return Handler


def _doc_json(doc: FakeDocument) -> dict[str, Any]:
    return {
        "id": doc.id,
        "title": doc.title,
        "page_count": doc.page_count,
        "inserted_at": "2026-11-21T09:00:00Z",
        "is_password_needed": not doc.unlocked,
        "outputs": [_output_json(f, s) for f, s in doc.outputs.items()],
    }


def _output_json(fmt: str, stage: str) -> dict[str, Any]:
    return {
        "format": fmt,
        "stage": stage,
        "progress": 1.0 if stage == "complete" else 0.0,
        "estimated_time_remaining": None,
        "is_preview": False,
        "downloadable": fmt not in ("html", "html_stream"),
    }


_SETTINGS_LISTS: dict[str, Any] = {
    "languages": {"languages": [["English", "en"], ["Spanish", "es"]]},
    "dialects": {
        "dialects": {"en": [["English (US)", "en-US"]], "es": [["Spanish (Spain)", "es-ES"]]}
    },
    "voices": {
        "voices": {
            "en-US": [["Ava", "en-US-AvaNeural", True]],
            "es-ES": [["Elvira", "es-ES-ElviraNeural", True]],
        }
    },
    "braille_tables": {"braille_tables": [["English UEB Grade 2", "en-ueb-g2.ctb"]]},
}


# ------------------------------------------------------------------ the channel


def _channel_handler(fake: FakeScribe) -> Any:
    def handle(ws: Any) -> None:
        query = parse_qs(urlsplit(ws.request.path).query)
        if query.get("token", [""])[0] not in fake.valid_tokens:
            ws.close()
            return
        sent = 0

        def push(topic: str, event: str, payload: dict[str, Any], ref: Any = None) -> None:
            nonlocal sent
            if fake.drop_after_frames is not None and sent >= fake.drop_after_frames:
                fake.drop_after_frames = None
                raise ConnectionError("dropped")
            sent += 1
            ws.send(json.dumps(["1", ref, topic, event, payload]))

        try:
            for raw in ws:
                join_ref, ref, topic, event, payload = json.loads(raw)
                if event == "heartbeat":
                    ws.send(
                        json.dumps([
                            None,
                            ref,
                            "phoenix",
                            "phx_reply",
                            {"status": "ok", "response": {}},
                        ])
                    )
                    continue
                doc = fake.documents.get(topic.split(":", 1)[1])
                if event == "phx_join":
                    if doc is None:
                        push(
                            topic,
                            "phx_reply",
                            {"status": "error", "response": {"reason": "not_found"}},
                            ref,
                        )
                        continue
                    push(topic, "phx_reply", {"status": "ok", "response": {}}, ref)
                    if not doc.unlocked:
                        push(topic, "password_required", {})
                    for fmt, stage in doc.outputs.items():
                        if stage == "complete":
                            push(topic, "conversion_complete", {"format": fmt, "output_id": fmt})
                    continue
                if event == "start_conversion" and doc is not None:
                    _start(fake, doc, topic, ref, payload, push)
        except Exception:  # noqa: BLE001 - a drop ends the connection, as in life
            return

    return handle


def _start(
    fake: FakeScribe, doc: FakeDocument, topic: str, ref: Any, payload: dict[str, Any], push: Any
) -> None:
    fmt = str(payload.get("format"))
    fake.started.append(fmt)
    if fake.start_errors:
        push(topic, "phx_reply", {"status": "error", "response": fake.start_errors.pop(0)}, ref)
        return
    if not doc.unlocked:
        given = payload.get("password")
        if not given:
            push(
                topic,
                "phx_reply",
                {
                    "status": "error",
                    "response": {
                        "reason": "password_required",
                        "message": "This document needs its password.",
                    },
                },
                ref,
            )
            return
        if given != doc.password:
            push(topic, "phx_reply", {"status": "ok", "response": {"output_id": fmt}}, ref)
            push(
                topic, "error", {"reason": "invalid_password", "message": "That password is wrong."}
            )
            return
        doc.unlocked = True
    push(topic, "phx_reply", {"status": "ok", "response": {"output_id": fmt}}, ref)
    if doc.outputs.get(fmt) == "complete":
        return  # made before: the id, and nothing more
    for step in fake.scripts.get(fmt, DEFAULT_SCRIPT):
        if step[0] == "status":
            push(topic, "status", {"format": fmt, "stage": step[1], "progress": step[2]})
        elif step[0] == "error":
            push(topic, "error", {"reason": step[1], "message": step[2]})
            return
        elif step[0] == "error_event":  # a whole payload, e.g. with a purchase_url
            push(topic, "error", dict(step[1]))
            if step[1].get("reason") != "conversion_in_progress":
                return
        elif step[0] == "chunk":
            push(topic, "chunk", {"content": step[1]})
    doc.outputs[fmt] = "complete"
    push(topic, "conversion_complete", {"format": fmt, "output_id": fmt})


@contextmanager
def running_fake() -> Iterator[FakeScribe]:
    from websockets.sync.server import serve

    fake = FakeScribe()
    http_server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    fake.http_port = http_server.server_address[1]
    quiet = logging.getLogger("tests.scribe_fake_server")
    quiet.setLevel(logging.WARNING)
    ws_server = serve(_channel_handler(fake), "127.0.0.1", 0, compression=None, logger=quiet)
    fake.ws_port = ws_server.socket.getsockname()[1]
    threads = [
        threading.Thread(target=http_server.serve_forever, daemon=True),
        threading.Thread(target=ws_server.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        yield fake
    finally:
        http_server.shutdown()
        http_server.server_close()
        ws_server.shutdown()
