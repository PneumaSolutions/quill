"""Scribe for Documents: consent-gated cloud document conversion (wx-free).

Scribe for Documents (https://scribeit.io, run by Pneuma Solutions) turns a
PDF, a scan or photo, an Office or OpenDocument file, an e-book, a text file or
a web address into an accessible format: tagged PDF, Word, HTML, EPUB, MOBI,
DAISY, MP3 or braille (BRF). This module is QUILL's client for it, built on
the official ``scribe-client`` package (a Rust core with Python bindings).
It sits beside the Datalab cloud tier (:mod:`quill.core.datalab_ocr`) and
follows the same trust rules:

* **Explicit, per-use consent.** Nothing here runs from an automatic path.
  Every caller is the Convert with Scribe for Documents command, which asks
  before each upload (``quill.ui.main_frame_scribe``).
* **The person's own account.** Scribe signs people in with OAuth 2.0 and
  PKCE in their own browser (:mod:`quill.core.scribe_signin`). QUILL never
  sees a password. The token set lives in the OS secret vault through
  :mod:`quill.core.secrets`, never in ``settings.json``; Sign Out wipes it.
* **One document at a time**, as Scribe's developer policy asks: a job is one
  file or one address, and the next waits for the person to start it.
* **Every status update reaches the person.** Each distinct stage or progress
  report Scribe sends is handed to ``on_status``, which the UI puts in the
  status bar and speaks.
* **Safe Mode blocked**, like every network feature.
* **Logging discipline:** document ids, formats and stages may be logged;
  file contents, results, tokens and passwords never are.

GATE-9 / network egress: ``scribe-client`` does its own HTTPS and WebSocket
work inside a compiled extension, so the AST scan cannot see it. The two
integration points are recorded in ``_NON_AST_EGRESS``:
:func:`quill.core.scribe_account._make_client` and
:func:`quill.core.scribe_signin.sign_in`.

The account side (client ID, redirect, tokens, errors, the authenticated
client) lives in :mod:`quill.core.scribe_account` and is re-exported here, so
callers import one module.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quill.core.scribe_account import (
    POWERED_BY,
    SCRIBE_BASE_URL,
    SCRIBE_CLIENT_ID,
    SCRIBE_CLIENT_ID_ENV,
    SCRIBE_CLIENT_ID_PLACEHOLDER,
    SCRIBE_LINKS,
    SCRIBE_REDIRECT_PATH,
    SCRIBE_REDIRECT_PORT,
    SCRIBE_REDIRECT_URI,
    SERVICE_NAME,
    ScribeCancelled,
    ScribeDocumentsError,
    ScribeSignInRequired,
    StoredTokens,
    _make_client,
    client_id,
    client_id_configured,
    client_module,
    friendly_error,
    load_tokens,
    safe_mode_active,
    save_tokens,
    sign_out,
    signed_in,
)
from quill.core.secrets import SecretsManager

logger = logging.getLogger(__name__)

#: Reconnect attempts after the live channel drops mid-conversion.
MAX_RECONNECTS = 5
_RECONNECT_DELAYS_S = (2.0, 4.0, 8.0, 15.0, 30.0)

StatusFn = Callable[[str], None]
CancelFn = Callable[[], bool]

#: What Scribe accepts (developer documentation, 2026-11). The file chooser
#: lists these; anything else is refused before upload with a sentence.
SCRIBE_INPUT_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
    ".bmp",
    ".gif",
    ".djvu",
    ".djv",
    ".jp2",
    ".j2k",
    ".jpf",
    ".jpx",
    ".pcx",
    ".dcx",
    ".doc",
    ".docx",
    ".rtf",
    ".odt",
    ".xls",
    ".xlsx",
    ".ods",
    ".ppt",
    ".pptx",
    ".odp",
    ".epub",
    ".mobi",
    ".txt",
})


@dataclass(frozen=True, slots=True)
class ScribeFormat:
    """One output format Scribe can produce."""

    id: str
    label: str
    extension: str
    opens_in_quill: bool = False


#: In menu order. The first opens in QUILL for editing and is the default;
#: the rest are saved to a folder the person chooses.
OUTPUT_FORMATS: tuple[ScribeFormat, ...] = (
    ScribeFormat("html", "Accessible HTML, opened in QUILL for editing", ".html", True),
    ScribeFormat("docx", "Word document (DOCX)", ".docx"),
    ScribeFormat("pdf", "Tagged PDF", ".pdf"),
    ScribeFormat("epub", "EPUB e-book", ".epub"),
    ScribeFormat("daisy", "DAISY talking book", ".zip"),
    ScribeFormat("mp3", "MP3 audio", ".mp3"),
    ScribeFormat("brf", "Braille (BRF)", ".brf"),
    ScribeFormat("offline_html", "Offline HTML", ".zip"),
    ScribeFormat("mobi", "Kindle (MOBI)", ".mobi"),
)
DEFAULT_FORMAT = OUTPUT_FORMATS[0]

_FORMAT_NAMES: dict[str, str] = {
    "html": "HTML",
    "html_stream": "Preview",
    "pdf": "Tagged PDF",
    "docx": "Word",
    "epub": "EPUB",
    "daisy": "DAISY",
    "mp3": "MP3",
    "brf": "Braille",
    "offline_html": "Offline HTML",
    "mobi": "MOBI",
}
_STAGE_WORDS: dict[str, str] = {
    "queue": "waiting in the queue",
    "start": "starting",
    "convert": "converting",
    "add_image_descriptions": "adding image descriptions",
    "complete": "complete",
}


def format_by_id(format_id: str) -> ScribeFormat:
    for item in OUTPUT_FORMATS:
        if item.id == format_id:
            return item
    return DEFAULT_FORMAT


# -------------------------------------------------------------- the job


@dataclass(slots=True)
class ScribeJob:
    """One document to convert. ``document_id`` is set once it is uploaded,
    so a retry (a password, a lost connection) never uploads it twice."""

    output_format: str = DEFAULT_FORMAT.id
    path: Path | None = None
    url: str = ""
    display_name: str = ""
    password: str | None = None
    document_id: str = ""

    @property
    def name(self) -> str:
        if self.display_name:
            return self.display_name
        if self.path is not None:
            return self.path.name
        return self.url


#: Outcome kinds.
DONE = "done"
PASSWORD_NEEDED = "password_needed"
PASSWORD_WRONG = "password_wrong"


@dataclass(frozen=True, slots=True)
class ScribeResult:
    kind: str
    document_id: str
    output_format: str
    data: bytes = b""


def status_sentence(format_id: str, stage: str, progress: float | None) -> str:
    """One Scribe status event as a sentence, e.g.
    "Scribe for Documents: HTML converting, 40 percent." """
    name = _FORMAT_NAMES.get(format_id, format_id or "Document")
    words = _STAGE_WORDS.get(stage, stage.replace("_", " ") or "working")
    if stage == "complete" or progress is None:
        return f"{SERVICE_NAME}: {name} {words}."
    percent = round(max(0.0, min(1.0, float(progress))) * 100)
    return f"{SERVICE_NAME}: {name} {words}, {percent} percent."


class _Reporter:
    """Passes on every distinct status sentence; drops exact repeats."""

    def __init__(self, on_status: StatusFn | None) -> None:
        self._on_status = on_status
        self._last = ""

    def __call__(self, message: str) -> None:
        if message == self._last or self._on_status is None:
            return
        self._last = message
        self._on_status(message)


def check_source(path: Path) -> None:
    """Refuse an unsupported file before anything is uploaded."""
    if path.suffix.lower() not in SCRIBE_INPUT_EXTENSIONS:
        raise ScribeDocumentsError(
            f"Scribe for Documents cannot convert {path.suffix or 'this kind of'} "
            "files. It takes PDF, images, Word, RTF, OpenDocument, Excel, "
            "PowerPoint, EPUB, MOBI and text files, or a web address."
        )


def check_url(url: str) -> str:
    cleaned = url.strip()
    if not cleaned.lower().startswith(("https://", "http://")):
        raise ScribeDocumentsError("Enter a full web address that starts with https:// or http://.")
    return cleaned


def run_conversion(
    job: ScribeJob,
    *,
    module: Any | None = None,
    manager: SecretsManager | None = None,
    on_status: StatusFn | None = None,
    cancel_requested: CancelFn | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> ScribeResult:
    """Upload ``job`` (unless it already was), convert it, and download it.

    Runs on a worker thread. Returns :data:`DONE` with the bytes, or asks the
    caller for a password with :data:`PASSWORD_NEEDED` / :data:`PASSWORD_WRONG`
    (the caller sets ``job.password`` and calls again; the document is not
    uploaded again). Raises :class:`ScribeDocumentsError` with a sentence on
    every failure. A dropped channel is reopened up to :data:`MAX_RECONNECTS`
    times; each time QUILL first asks Scribe whether the output finished while
    it was away, because the conversion carries on without the channel.
    """
    mod = module if module is not None else client_module()
    report = _Reporter(on_status)
    fmt = format_by_id(job.output_format)
    try:
        client = _make_client(mod, manager=manager, now=now)
        if not job.document_id:
            job.document_id = _upload(mod, client, job, report)
    except mod.ScribeApiError as exc:
        raise friendly_error(mod, exc, manager) from None

    attempt = 0
    while True:
        try:
            if attempt:
                finished = _finished_while_away(client, job, fmt.id)
                if finished is not None:
                    report(f"{SERVICE_NAME}: reconnected.")
                    return _download(client, job, fmt.id, report)
            return _drive_channel(mod, client, job, fmt.id, report, cancel_requested)
        except ScribeDocumentsError:
            raise
        except mod.ScribeApiError as exc:
            if _is_specific(mod, exc):
                raise friendly_error(mod, exc, manager) from None
            attempt += 1
            logger.info("Scribe channel lost document=%s attempt=%s", job.document_id, attempt)
            if attempt > MAX_RECONNECTS:
                raise ScribeDocumentsError(
                    "QUILL lost its connection to Scribe for Documents and could not "
                    "get it back. The conversion may still finish in your Scribe "
                    "library at scribeit.io. Check your internet connection."
                ) from None
            report(
                f"Lost the connection to {SERVICE_NAME}. The conversion carries on "
                f"there. Reconnecting, attempt {attempt} of {MAX_RECONNECTS}."
            )
            _check_cancel(cancel_requested)
            sleep(_RECONNECT_DELAYS_S[min(attempt, len(_RECONNECT_DELAYS_S)) - 1])
            _check_cancel(cancel_requested)


def _is_specific(module: Any, exc: BaseException) -> bool:
    """A server answer (credits, sign-in, not found), not a dropped connection."""
    specific = (
        module.NeedsPurchaseError,
        module.InvalidGrantError,
        module.NotFoundError,
        module.ForbiddenError,
        module.RateLimitedError,
    )
    return isinstance(exc, specific)


def _check_cancel(cancel_requested: CancelFn | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise ScribeCancelled(
            "Stopped waiting for Scribe for Documents. The conversion may still "
            "finish in your Scribe library."
        )


def _upload(module: Any, client: Any, job: ScribeJob, report: _Reporter) -> str:
    if job.path is not None:
        check_source(job.path)
        report(f"Uploading {job.name} to {SERVICE_NAME}.")
        data = job.path.read_bytes()
        document_id = client.create_document_from_file(job.path.name, data, job.password)
    else:
        url = check_url(job.url)
        report(f"Sending the address to {SERVICE_NAME}.")
        document_id = client.create_document_from_url(url, job.password)
    logger.info("Scribe document created document=%s", document_id)
    report(f"{SERVICE_NAME} received {job.name}.")
    return str(document_id)


def _target_output(client: Any, job: ScribeJob, format_id: str) -> Any:
    listing = client.list_outputs(job.document_id)
    for output in getattr(listing, "outputs", ()) or ():
        if getattr(output, "format", "") == format_id:
            return output
    return None


def _finished_while_away(client: Any, job: ScribeJob, format_id: str) -> Any:
    output = _target_output(client, job, format_id)
    if output is not None and getattr(output, "stage", "") == "complete":
        return output
    return None


def _download(client: Any, job: ScribeJob, format_id: str, report: _Reporter) -> ScribeResult:
    report(f"Downloading the {_FORMAT_NAMES.get(format_id, format_id)} result.")
    data = bytes(client.download_output(job.document_id, format_id))
    logger.info("Scribe download complete document=%s format=%s", job.document_id, format_id)
    return ScribeResult(DONE, job.document_id, format_id, data)


def _drive_channel(
    module: Any,
    client: Any,
    job: ScribeJob,
    format_id: str,
    report: _Reporter,
    cancel_requested: CancelFn | None,
) -> ScribeResult:
    channel = client.open_document_channel(job.document_id)
    try:
        while True:
            try:
                channel.start_conversion(format_id, job.password)
                break
            except module.PasswordRequiredError:
                return ScribeResult(PASSWORD_NEEDED, job.document_id, format_id)
            except module.ConversionInProgressError:
                report(f"{SERVICE_NAME}: waiting for the conversion already running.")
            # Scribe runs one conversion of a document at a time; a new
            # document is always busy with its preview at first.
            waited = _wait_for_any_completion(channel, job, format_id, report, cancel_requested)
            if waited is not None:
                return waited if waited.kind != DONE else _download(client, job, format_id, report)
        # A format that was already complete answers with its id and sends
        # nothing further, so ask before waiting for events.
        if _finished_while_away(client, job, format_id) is not None:
            return _download(client, job, format_id, report)
        while True:
            _check_cancel(cancel_requested)
            event = channel.next_event()
            outcome = _handle_event(event, job, format_id, report)
            if outcome is None:
                continue
            if outcome.kind == DONE:
                return _download(client, job, format_id, report)
            return outcome
    finally:
        try:
            channel.close()
        except Exception:  # noqa: BLE001 - closing a dead channel is not an error
            pass


def _wait_for_any_completion(
    channel: Any,
    job: ScribeJob,
    format_id: str,
    report: _Reporter,
    cancel_requested: CancelFn | None,
) -> ScribeResult | None:
    """Wait out a running conversion. Returns an outcome only when the
    target itself finished or a password is needed; ``None`` means try
    starting again."""
    while True:
        _check_cancel(cancel_requested)
        event = channel.next_event()
        if event.get("type") == "conversion_complete" and event.get("format") != format_id:
            return None
        outcome = _handle_event(event, job, format_id, report)
        if outcome is not None:
            return outcome


def _handle_event(
    event: dict[str, Any], job: ScribeJob, format_id: str, report: _Reporter
) -> ScribeResult | None:
    kind = event.get("type")
    if kind == "status":
        report(
            status_sentence(
                str(event.get("format", "")),
                str(event.get("stage", "")),
                event.get("progress"),
            )
        )
        return None
    if kind == "conversion_complete":
        if event.get("format") == format_id:
            report(status_sentence(format_id, "complete", None))
            return ScribeResult(DONE, job.document_id, format_id)
        return None
    if kind == "password_required":
        if job.password is None:
            return ScribeResult(PASSWORD_NEEDED, job.document_id, format_id)
        return None
    if kind == "error":
        reason = str(event.get("reason", "") or "")
        if reason == "invalid_password":
            return ScribeResult(PASSWORD_WRONG, job.document_id, format_id)
        raise ScribeDocumentsError(
            f"Scribe for Documents could not convert {job.name}: {reason or 'unknown error'}."
        )
    return None  # "chunk" (streamed preview HTML) and anything newer


def output_extension(fmt: ScribeFormat, data: bytes) -> str:
    """The file extension to save ``data`` under.

    The Python binding does not pass on the server's file name, so a format
    that may arrive packaged (HTML with images, MP3 chapters) is saved as a
    ZIP when the bytes are one.
    """
    if data[:4] == b"PK\x03\x04" and fmt.extension not in {".docx", ".epub", ".zip"}:
        return ".zip"
    return fmt.extension


__all__ = [
    "SCRIBE_REDIRECT_PATH",
    "DEFAULT_FORMAT",
    "DONE",
    "MAX_RECONNECTS",
    "OUTPUT_FORMATS",
    "PASSWORD_NEEDED",
    "PASSWORD_WRONG",
    "POWERED_BY",
    "SCRIBE_BASE_URL",
    "SCRIBE_CLIENT_ID",
    "SCRIBE_CLIENT_ID_ENV",
    "SCRIBE_CLIENT_ID_PLACEHOLDER",
    "SCRIBE_INPUT_EXTENSIONS",
    "SCRIBE_LINKS",
    "SCRIBE_REDIRECT_PORT",
    "SCRIBE_REDIRECT_URI",
    "SERVICE_NAME",
    "ScribeCancelled",
    "ScribeDocumentsError",
    "ScribeFormat",
    "ScribeJob",
    "ScribeResult",
    "ScribeSignInRequired",
    "StoredTokens",
    "check_source",
    "check_url",
    "client_id",
    "client_id_configured",
    "client_module",
    "format_by_id",
    "friendly_error",
    "load_tokens",
    "output_extension",
    "run_conversion",
    "safe_mode_active",
    "save_tokens",
    "sign_out",
    "signed_in",
    "status_sentence",
]
