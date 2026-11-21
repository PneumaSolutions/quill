"""QUILL's own client for Scribe for Documents' REST API (wx-free; PRD 7.1).

Written from Pneuma's open-source client (``scribe-api-client``) and the
server's routes, so GATE-9 can see every call: each method goes through
:func:`quill.core.scribe.http.request`. Errors are the coded classes in
:mod:`quill.core.scribe.model`, carrying the server's own sentence.

Every ``/api`` call sends the bearer token from :class:`~quill.core.scribe.auth.TokenStore`,
refreshed five minutes before expiry and once more on a 401.

Methods block: call them from a worker thread.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlsplit

from quill.core.scribe import http
from quill.core.scribe.auth import TokenStore, base_url, browser_url, check_allowed
from quill.core.scribe.model import (
    DocumentSettings,
    ScribeCancelled,
    ScribeError,
    ScribeSaveFailed,
    ScribeSignInRequired,
    format_by_id,
)

logger = logging.getLogger(__name__)

_CHUNK = 256 * 1024
ProgressFn = Callable[[int, int], None]  # (bytes done, bytes total or 0)
CancelFn = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class Output:
    format: str
    stage: str = ""
    progress: float = 0.0
    estimated_time_remaining: float | None = None
    is_preview: bool = False
    downloadable: bool = False

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Output:
        eta = data.get("estimated_time_remaining")
        return cls(
            format=str(data.get("format") or ""),
            stage=str(data.get("stage") or ""),
            progress=float(data.get("progress") or 0.0),
            estimated_time_remaining=float(eta) if isinstance(eta, (int, float)) else None,
            is_preview=bool(data.get("is_preview")),
            downloadable=bool(data.get("downloadable")),
        )

    @property
    def complete(self) -> bool:
        return self.stage == "complete" and not self.is_preview


@dataclass(frozen=True, slots=True)
class RemoteDocument:
    id: str
    title: str
    page_count: int = 0
    inserted_at: str = ""
    is_password_needed: bool = False
    outputs: tuple[Output, ...] = ()
    trashed_at: str = ""
    permanently_delete_at: str = ""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RemoteDocument:
        return cls(
            id=str(data.get("id") or ""),
            title=str(data.get("title") or "Untitled"),
            page_count=int(data.get("page_count") or 0),
            inserted_at=str(data.get("inserted_at") or ""),
            is_password_needed=bool(data.get("is_password_needed")),
            outputs=tuple(
                Output.from_json(o) for o in data.get("outputs") or () if isinstance(o, dict)
            ),
            trashed_at=str(data.get("trashed_at") or ""),
            permanently_delete_at=str(data.get("permanently_delete_at") or ""),
        )

    def ready_formats(self) -> list[str]:
        return [o.format for o in self.outputs if o.complete and o.format != "html_stream"]


@dataclass(frozen=True, slots=True)
class Account:
    name: str = ""
    email: str = ""
    organization: str = ""
    pages_remaining: int | None = None


@dataclass(slots=True)
class DocumentList:
    documents: list[RemoteDocument] = field(default_factory=list)
    pages_remaining: int | None = None


def safe_filename(name: str, fallback: str = "Scribe result") -> str:
    """A server file name made safe for every file system QUILL runs on."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:150] or fallback


def unique_target(folder: Path, name: str) -> Path:
    """``folder/name``, or ``name (2)`` and so on: never overwrite (PRD 7.6)."""
    candidate = folder / name
    stem, suffix = Path(name).stem, Path(name).suffix
    number = 2
    while candidate.exists():
        candidate = folder / f"{stem} ({number}){suffix}"
        number += 1
    return candidate


def _filename_from(disposition: str, url: str) -> str:
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I)
    if match:
        return unquote(match.group(1)).strip('"')
    match = re.search(r'filename="?([^";]+)"?', disposition, re.I)
    if match:
        return match.group(1)
    query = dict(p.split("=", 1) for p in urlsplit(url).query.split("&") if "=" in p)
    embedded = unquote(query.get("response-content-disposition", ""))
    if embedded:
        return _filename_from(embedded, "")
    return unquote(Path(urlsplit(url).path).name) if url else ""


class ScribeClient:
    """REST calls on the person's own Scribe account."""

    def __init__(self, store: TokenStore | None = None, *, locale: str = "") -> None:
        self.store = store or TokenStore()
        self.locale = locale

    # ------------------------------------------------------------ plumbing

    def _call(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        body: bytes | None = None,
        content_type: str = "",
        what: str = "",
        timeout: float = http.TIMEOUT_S,
    ) -> http.Response:
        check_allowed()
        headers = {"Accept": "application/json"}
        if self.locale:
            headers["Accept-Language"] = self.locale
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        url = f"{base_url()}{path}"
        for attempt in (0, 1):
            headers["Authorization"] = (
                f"Bearer {self.store.access_token(force_refresh=attempt == 1)}"
            )
            response = http.request(method, url, headers=headers, body=body, timeout=timeout)
            if response.status != 401 or attempt == 1:
                break
        if response.status == 401:
            raise ScribeSignInRequired(
                "Your Scribe for Documents sign-in has ended. Conversions are paused "
                "until you sign in again.",
                server_code="unauthenticated",
            )
        http.raise_for(response, what=what)
        return response

    # ------------------------------------------------------------ account

    def account(self) -> Account:
        info = self._call("GET", "/api/account_info").json()
        billing = self._call("GET", "/api/billing").json()
        name = " ".join(
            part for part in (info.get("first_name"), info.get("last_name")) if part
        ).strip()
        pages = billing.get("pages_remaining")
        return Account(
            name=name or str(info.get("email") or ""),
            email=str(info.get("email") or ""),
            organization=str(info.get("organization_name") or ""),
            pages_remaining=int(pages) if isinstance(pages, (int, float)) else None,
        )

    def account_page_url(self, return_to: str = "/settings") -> str:
        """A browser link that arrives signed in (``browser_from_oauth``)."""
        return browser_url(self.store.access_token(), return_to)

    def purchase_url(self) -> str:
        return f"{base_url()}/signup/page_pack"

    # ------------------------------------------------------------ documents

    def create_from_file(self, path: Path, password: str | None = None) -> str:
        """Upload a file; returns the new document's id."""
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ScribeError(f"QUILL could not read {path.name}: {exc.strerror or exc}.") from None
        fields = {"document[password]": password} if password else {}
        body, content_type = http.multipart(fields, {"document[file]": (path.name, data)})
        reply = self._call(
            "POST",
            "/api/documents",
            body=body,
            content_type=content_type,
            what=f"uploading {path.name}",
            timeout=600.0,
        ).json()
        logger.info("Scribe document created")
        return str(reply.get("document_id") or "")

    def create_from_url(self, url: str, password: str | None = None) -> str:
        fields = {"document[url]": url}
        if password:
            fields["document[password]"] = password
        body, content_type = http.multipart(fields, {})
        reply = self._call(
            "POST", "/api/documents", body=body, content_type=content_type, what="fetching the link"
        ).json()
        return str(reply.get("document_id") or "")

    def documents(self) -> DocumentList:
        data = self._call("GET", "/api/documents").json()
        pages = data.get("pages_remaining")
        return DocumentList(
            [
                RemoteDocument.from_json(d)
                for d in data.get("documents") or ()
                if isinstance(d, dict)
            ],
            int(pages) if isinstance(pages, (int, float)) else None,
        )

    def trash_list(self) -> list[RemoteDocument]:
        data = self._call("GET", "/api/documents/trash").json()
        return [
            RemoteDocument.from_json(d) for d in data.get("documents") or () if isinstance(d, dict)
        ]

    def _doc(self, document_id: str) -> str:
        return f"/api/documents/{quote(document_id, safe='')}"

    def rename(self, document_id: str, title: str) -> RemoteDocument:
        data = self._call(
            "PATCH", self._doc(document_id), json_body={"document": {"title": title}}
        ).json()
        return RemoteDocument.from_json(
            data.get("document", data) if isinstance(data, dict) else {}
        )

    def trash(self, document_id: str) -> None:
        self._call("POST", f"{self._doc(document_id)}/trash")

    def recover(self, document_id: str) -> None:
        self._call("POST", f"{self._doc(document_id)}/recover")

    def delete_forever(self, document_id: str) -> None:
        self._call("DELETE", self._doc(document_id))

    def outputs(self, document_id: str) -> tuple[list[Output], bool]:
        data = self._call("GET", f"{self._doc(document_id)}/outputs").json()
        outputs = [Output.from_json(o) for o in data.get("outputs") or () if isinstance(o, dict)]
        return outputs, bool(data.get("is_password_needed"))

    def human_review(self, document_id: str, comment: str) -> None:
        self._call("POST", f"{self._doc(document_id)}/feedback", json_body={"comment": comment})

    # ------------------------------------------------------------ settings

    def document_settings(self, document_id: str) -> DocumentSettings:
        data = self._call("GET", f"{self._doc(document_id)}/settings").json()
        return DocumentSettings.from_json(data.get("settings", data))

    def set_document_settings(self, document_id: str, settings: DocumentSettings) -> None:
        self._call(
            "PATCH",
            f"{self._doc(document_id)}/settings",
            json_body={"settings": settings.to_json()},
        )

    def account_defaults(self) -> DocumentSettings:
        data = self._call("GET", "/api/document_settings").json()
        return DocumentSettings.from_json(data.get("settings", data))

    def set_account_defaults(self, settings: DocumentSettings) -> None:
        self._call("PATCH", "/api/document_settings", json_body={"settings": settings.to_json()})

    def languages(self) -> list[tuple[str, str]]:
        data = self._call("GET", "/api/settings/languages").json()
        return [(str(n), str(c)) for n, c in data.get("languages") or ()]

    def dialects(self) -> dict[str, list[tuple[str, str]]]:
        data = self._call("GET", "/api/settings/dialects").json()
        return {
            str(lang): [(str(n), str(loc)) for n, loc in rows]
            for lang, rows in (data.get("dialects") or {}).items()
        }

    def voices(self) -> dict[str, list[tuple[str, str, bool]]]:
        data = self._call("GET", "/api/settings/voices").json()
        return {
            str(locale): [
                (str(row[0]), str(row[1]), bool(row[2] if len(row) > 2 else False)) for row in rows
            ]
            for locale, rows in (data.get("voices") or {}).items()
        }

    def braille_tables(self) -> list[tuple[str, str]]:
        data = self._call("GET", "/api/settings/braille_tables").json()
        return [(str(n), str(i)) for n, i in data.get("braille_tables") or ()]

    def voice_sample_url(self, short_name: str) -> str:
        return f"{base_url()}/sounds/tts-samples/{quote(short_name, safe='')}.mp3"

    def voice_sample(self, short_name: str) -> bytes:
        """Scribe's sample MP3 of a voice (public: no token is sent; PRD 7.3)."""
        check_allowed()
        response = http.request("GET", self.voice_sample_url(short_name), timeout=30.0)
        http.raise_for(response, what="fetching the voice sample")
        if response.status != 200 or not response.body:
            raise ScribeError("Scribe for Documents has no sample of this voice.")
        return response.body

    # ------------------------------------------------------------ download

    def download(
        self,
        document_id: str,
        format_id: str,
        folder: Path,
        *,
        fallback_stem: str,
        on_progress: ProgressFn | None = None,
        cancel_requested: CancelFn | None = None,
    ) -> Path:
        """Download a finished output into ``folder``; returns the file written.

        The API answers with a 302 to a presigned storage URL, fetched without
        the bearer token; a dropped transfer resumes with ``Range`` (PRD 7.6).
        """
        response = self._call(
            "GET",
            f"{self._doc(document_id)}/outputs/{quote(format_id, safe='')}/download",
            what=f"downloading the {format_by_id(format_id).label} result",
        )
        location = response.header("location")
        if not location:
            # ``force_direct`` answers (and the fake server) send the bytes here.
            name = _filename_from(response.header("content-disposition"), "")
            return self._write_whole(response.body, folder, name, fallback_stem, format_id)
        name = _filename_from("", location)
        return self._fetch_resumable(
            location, folder, name, fallback_stem, format_id, on_progress, cancel_requested
        )

    def _target(self, folder: Path, name: str, fallback_stem: str, format_id: str) -> Path:
        extension = format_by_id(format_id).extension
        chosen = safe_filename(name) if name else ""
        if not chosen or "." not in chosen:
            chosen = safe_filename(f"{fallback_stem}{extension}")
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ScribeSaveFailed(
                f"QUILL could not make the folder {folder}: {exc.strerror or exc}.",
                folder=str(folder),
            ) from None
        return unique_target(folder, chosen)

    def _write_whole(
        self, data: bytes, folder: Path, name: str, fallback_stem: str, format_id: str
    ) -> Path:
        from quill.core.storage import write_bytes_atomic

        target = self._target(folder, name, fallback_stem, format_id)
        try:
            write_bytes_atomic(target, data)
        except OSError as exc:
            raise ScribeSaveFailed(
                f"QUILL could not save {target.name} in {folder}: {exc.strerror or exc}.",
                folder=str(folder),
            ) from None
        return target

    def _fetch_resumable(
        self,
        url: str,
        folder: Path,
        name: str,
        fallback_stem: str,
        format_id: str,
        on_progress: ProgressFn | None,
        cancel_requested: CancelFn | None,
    ) -> Path:
        target = self._target(folder, name, fallback_stem, format_id)
        partial = target.with_name(target.name + ".part")
        total = 0
        for attempt in range(4):
            done = partial.stat().st_size if partial.exists() else 0
            headers = {"Range": f"bytes={done}-"} if done else {}
            response = http.request("GET", url, headers=headers, timeout=300.0, stream=True)
            if response.status not in (200, 206) or response.stream is None:
                http.raise_for(response, what="downloading")
                raise ScribeSaveFailed("The download did not start.", folder=str(folder))
            if response.status == 200:
                done = 0  # the server ignored Range: start over
            length = response.header("content-length")
            total = done + int(length) if length.isdigit() else 0
            try:
                with open(partial, "ab" if done else "wb") as handle, response.stream as stream:
                    while True:
                        if cancel_requested is not None and cancel_requested():
                            raise ScribeCancelled("The download was stopped.")
                        chunk = stream.read(_CHUNK)
                        if not chunk:
                            break
                        handle.write(chunk)
                        done += len(chunk)
                        if on_progress is not None:
                            on_progress(done, total)
                if not total or done >= total:
                    break
            except OSError as exc:
                if attempt == 3 or isinstance(exc, PermissionError):
                    raise ScribeSaveFailed(
                        f"QUILL could not save {target.name} in {folder}: {exc.strerror or exc}.",
                        folder=str(folder),
                    ) from None
                logger.info("Scribe download dropped; resuming attempt=%s", attempt + 1)
        partial.replace(target)
        return target


def query(params: dict[str, str]) -> str:
    return urlencode(params)


__all__ = [
    "Account",
    "DocumentList",
    "Output",
    "RemoteDocument",
    "ScribeClient",
    "safe_filename",
    "unique_target",
]
