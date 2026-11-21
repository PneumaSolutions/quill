"""Scribe for Documents, the wizard and saved workflows (wx-free).

Tools > Scribe for Documents opens a wizard (``quill/ui/scribe_wizard_dialog.py``,
kept thin) whose every decision lives here so it can be tested without wx:

1. **What to do**: convert a file, scan pages, or convert a web page.
2. **The source**: a file (the current document is offered when Scribe can
   read it), pages from the scanner, or a web address.
3. **What to make**: "Open in QUILL to read" (accessible HTML, opened as a new
   document) first, then Word, PDF, braille, audio and the rest. A saved file
   goes **next to the source** by default (:func:`default_folder`), or to the
   Documents folder for a scan, a web page, or a source folder QUILL cannot
   write to; "Change folder" overrides it.
4. **Converting**, then **done**: Open, Show in folder, Do another, and Save
   as workflow.

A **workflow** (:class:`Workflow`) is a finished run saved under a name --
"Scan to braille" is *scan, then BRF, then this folder*. Each one is its own
row in Tools > Scribe Workflows, and running it asks nothing except picking
the file or scanning. Workflows are kept in ``<QUILL data>/scribe/workflows.json``
(atomic writes through :func:`quill.core.storage.write_json_atomic`), never in
``settings.json``; nothing in them is secret.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quill.core.error_codes import CodedError
from quill.core.scribe_documents import (
    OUTPUT_FORMATS,
    SCRIBE_INPUT_EXTENSIONS,
    ScribeFormat,
    format_by_id,
)

SOURCE_FILE = "file"
SOURCE_SCAN = "scan"
SOURCE_URL = "url"

#: Page one's choices, in order.
SOURCES: tuple[tuple[str, str], ...] = (
    (SOURCE_FILE, "Convert a file"),
    (SOURCE_SCAN, "Scan pages"),
    (SOURCE_URL, "Convert a web page"),
)

#: What a listener hears for each format on the "What to make" page.
FORMAT_LABELS: dict[str, str] = {
    "html": "Open in QUILL to read",
    "docx": "Word document",
    "pdf": "Accessible PDF",
    "brf": "Braille (BRF)",
    "mp3": "Audio (MP3)",
    "daisy": "DAISY talking book",
    "epub": "EPUB e-book",
    "offline_html": "Web page to keep (offline HTML)",
    "mobi": "Kindle book (MOBI)",
}

#: The order on that page: the common ones first.
FORMAT_ORDER: tuple[str, ...] = (
    "html",
    "docx",
    "pdf",
    "brf",
    "mp3",
    "daisy",
    "epub",
    "offline_html",
    "mobi",
)

WORKFLOWS_FILE = "workflows.json"
MAX_WORKFLOWS = 50
_SCHEMA_VERSION = 1


def wizard_formats() -> list[ScribeFormat]:
    """Every format, in the wizard's order (unknown ones last)."""
    known = {item.id: item for item in OUTPUT_FORMATS}
    ordered = [known[i] for i in FORMAT_ORDER if i in known]
    ordered += [item for item in OUTPUT_FORMATS if item.id not in FORMAT_ORDER]
    return ordered


def format_label(format_id: str) -> str:
    return FORMAT_LABELS.get(format_id, format_by_id(format_id).label)


def opens_in_quill(format_id: str) -> bool:
    return format_by_id(format_id).opens_in_quill


def scribe_can_read(path: Path | None) -> bool:
    return path is not None and path.suffix.lower() in SCRIBE_INPUT_EXTENSIONS


def documents_folder(home: Path | None = None) -> Path:
    """The person's Documents folder, or their home folder when there is none."""
    base = home or Path.home()
    documents = base / "Documents"
    return documents if documents.is_dir() else base


def folder_is_writable(folder: Path) -> bool:
    return folder.is_dir() and os.access(folder, os.W_OK)


def default_folder(
    source: str,
    path: Path | None,
    *,
    home: Path | None = None,
    writable: Callable[[Path], bool] = folder_is_writable,
) -> Path:
    """Where a saved result goes unless the person changes it: next to the
    source file, or Documents for a scan, a web page, or an unwritable folder."""
    if source == SOURCE_FILE and path is not None and writable(path.parent):
        return path.parent
    return documents_folder(home)


def stem_for_url(url: str) -> str:
    """A file-name stem for a web address: its last path part, or its host."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    last = Path(parts.path).stem or parts.hostname or "web page"
    return re.sub(r"[^\w .-]+", "_", last).strip(" ._") or "web page"


def result_stem(source: str, path: Path | None, url: str, scan_name: str = "Scanned pages") -> str:
    if source == SOURCE_FILE and path is not None:
        return path.stem
    if source == SOURCE_URL:
        return stem_for_url(url)
    return scan_name


def unique_path(folder: Path, stem: str, extension: str) -> Path:
    candidate = folder / f"{stem}{extension}"
    number = 2
    while candidate.exists():
        candidate = folder / f"{stem} ({number}){extension}"
        number += 1
    return candidate


def scan_name(now: Any = None) -> str:
    """ "Scan 2026-11-21 0930" -- a name that sorts and never repeats in a day."""
    import datetime as _dt

    moment = now or _dt.datetime.now()
    return f"Scan {moment:%Y-%m-%d %H%M}"


# ----------------------------------------------------------- one run


@dataclass(slots=True)
class WizardRun:
    """What the person chose; the dialog fills it in page by page."""

    source: str = SOURCE_FILE
    path: Path | None = None
    url: str = ""
    pages: list[Path] = field(default_factory=list)
    format_id: str = "html"
    folder: Path | None = None  # None: the default (next to the source)
    workflow: str = ""  # the workflow this run came from, if any

    def ready_to_convert(self) -> str:
        """``""`` when the source is complete, else what is missing, as a sentence."""
        if self.source == SOURCE_FILE:
            if self.path is None:
                return "Choose a file first."
            if not scribe_can_read(self.path):
                return (
                    f"Scribe for Documents cannot convert {self.path.suffix or 'this kind of'} "
                    "files. Choose a PDF, a picture, a Word, RTF, OpenDocument, Excel, "
                    "PowerPoint, EPUB, MOBI or text file."
                )
            return ""
        if self.source == SOURCE_SCAN:
            return "" if self.pages else "Scan at least one page first."
        cleaned = self.url.strip().lower()
        if not cleaned.startswith(("https://", "http://")):
            return "Type a web address that starts with https://."
        return ""

    def target_folder(self, **kwargs: Any) -> Path:
        return self.folder or default_folder(self.source, self.path, **kwargs)

    def summary(self) -> str:
        """One sentence for the Convert page: what goes where."""
        label = format_label(self.format_id)
        if opens_in_quill(self.format_id):
            where = "and open it in QUILL"
        else:
            where = f"and save it in {self.target_folder()}"
        if self.source == SOURCE_FILE and self.path is not None:
            what = self.path.name
        elif self.source == SOURCE_SCAN:
            what = "1 scanned page" if len(self.pages) == 1 else f"{len(self.pages)} scanned pages"
        else:
            what = self.url.strip()
        return f"Scribe for Documents will make {label} from {what} {where}."


# ----------------------------------------------------------- workflows


@dataclass(frozen=True, slots=True)
class Workflow:
    """A saved run: its source kind, its format, and its folder.

    ``folder`` is ``""`` for "next to the source" (the default).
    """

    name: str
    source: str
    format_id: str
    folder: str = ""

    def describe(self) -> str:
        what = dict(SOURCES).get(self.source, self.source)
        where = ""
        if not opens_in_quill(self.format_id):
            where = f", saved in {self.folder}" if self.folder else ", saved next to the original"
        return f"{what}, then {format_label(self.format_id)}{where}"

    def to_run(self) -> WizardRun:
        return WizardRun(
            source=self.source,
            format_id=self.format_id,
            folder=Path(self.folder) if self.folder else None,
            workflow=self.name,
        )

    @classmethod
    def from_run(cls, name: str, run: WizardRun, *, keep_folder: bool) -> Workflow:
        return cls(
            name=clean_name(name),
            source=run.source,
            format_id=run.format_id,
            folder=str(run.folder) if keep_folder and run.folder is not None else "",
        )


def clean_name(name: str) -> str:
    return " ".join(str(name).split())[:60]


def suggested_name(run: WizardRun) -> str:
    """ "Scan to braille", "File to Word document" ..."""
    start = {SOURCE_FILE: "File", SOURCE_SCAN: "Scan", SOURCE_URL: "Web page"}.get(
        run.source, "Scribe"
    )
    if opens_in_quill(run.format_id):
        target = "read in QUILL"
    else:
        target = re.sub(r"\s*\(.*\)$", "", format_label(run.format_id)).lower()
    return f"{start} to {target}"


class WorkflowError(CodedError):
    """A workflow name the store refuses (empty, taken, too many)."""

    code = "QUILL-CONVERT-SCRIBE-WORKFLOW"


class WorkflowStore:
    """The saved workflows, in the order they were made."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        if self._path is None:
            from quill.core.paths import app_data_dir

            self._path = app_data_dir() / "scribe" / WORKFLOWS_FILE
        return self._path

    def load(self) -> list[Workflow]:
        from quill.core.storage import read_json

        data = read_json(self.path, {})
        rows = data.get("workflows", []) if isinstance(data, dict) else []
        known_formats = {item.id for item in OUTPUT_FORMATS}
        known_sources = {key for key, _label in SOURCES}
        workflows: list[Workflow] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            name = clean_name(str(row.get("name", "")))
            source = str(row.get("source", ""))
            format_id = str(row.get("format", ""))
            if not name or source not in known_sources or format_id not in known_formats:
                continue
            if any(w.name.lower() == name.lower() for w in workflows):
                continue
            workflows.append(Workflow(name, source, format_id, str(row.get("folder", "") or "")))
        return workflows[:MAX_WORKFLOWS]

    def _save(self, workflows: Sequence[Workflow]) -> None:
        from quill.core.storage import write_json_atomic

        write_json_atomic(
            self.path,
            {
                "schema_version": _SCHEMA_VERSION,
                "workflows": [
                    {"name": w.name, "source": w.source, "format": w.format_id, "folder": w.folder}
                    for w in workflows
                ],
            },
        )

    def add(self, workflow: Workflow, *, replace: bool = False) -> list[Workflow]:
        name = clean_name(workflow.name)
        if not name:
            raise WorkflowError("Give the workflow a name.")
        workflows = self.load()
        existing = [w for w in workflows if w.name.lower() == name.lower()]
        if existing and not replace:
            raise WorkflowError(f"You already have a workflow called {existing[0].name}.")
        workflows = [w for w in workflows if w.name.lower() != name.lower()]
        if len(workflows) >= MAX_WORKFLOWS:
            raise WorkflowError(f"You can keep up to {MAX_WORKFLOWS} workflows. Delete one first.")
        workflows.append(Workflow(name, workflow.source, workflow.format_id, workflow.folder))
        self._save(workflows)
        return workflows

    def rename(self, old: str, new: str) -> list[Workflow]:
        name = clean_name(new)
        if not name:
            raise WorkflowError("Give the workflow a name.")
        workflows = self.load()
        if any(w.name.lower() == name.lower() and w.name != old for w in workflows):
            raise WorkflowError(f"You already have a workflow called {name}.")
        renamed = [
            Workflow(name, w.source, w.format_id, w.folder) if w.name == old else w
            for w in workflows
        ]
        self._save(renamed)
        return renamed

    def delete(self, name: str) -> list[Workflow]:
        workflows = [w for w in self.load() if w.name != name]
        self._save(workflows)
        return workflows

    def find(self, name: str) -> Workflow | None:
        for workflow in self.load():
            if workflow.name == name:
                return workflow
        return None


__all__ = [
    "FORMAT_LABELS",
    "FORMAT_ORDER",
    "MAX_WORKFLOWS",
    "SOURCES",
    "SOURCE_FILE",
    "SOURCE_SCAN",
    "SOURCE_URL",
    "WizardRun",
    "Workflow",
    "WorkflowError",
    "WorkflowStore",
    "clean_name",
    "default_folder",
    "documents_folder",
    "folder_is_writable",
    "format_label",
    "opens_in_quill",
    "result_stem",
    "scan_name",
    "scribe_can_read",
    "stem_for_url",
    "suggested_name",
    "unique_path",
    "wizard_formats",
]
