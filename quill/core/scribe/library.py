"""Which Scribe document made which local file (wx-free; PRD 7.6).

``library.json`` in the shared QUILL data folder: for each Scribe document
id, its title and the files QUILL downloaded from it. My Scribe Documents
uses it to say "downloaded to ...", and Beacon can find a result again.
Also where downloads go: ``Documents\\Scribe for Documents\\<document title>``
unless Options names another folder.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from quill.core.file_lock import guarded_write

LIBRARY_FILE = "library.json"
FOLDER_NAME = "Scribe for Documents"


def default_download_root(home: Path | None = None) -> Path:
    """``Documents\\Scribe for Documents`` (the home folder when there is no Documents)."""
    base = home or Path.home()
    documents = base / "Documents"
    return (documents if documents.is_dir() else base) / FOLDER_NAME


def title_folder_name(title: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", title).strip(" .")
    return re.sub(r"\s+", " ", cleaned)[:100] or "Untitled"


def download_folder(title: str, root: str = "", *, home: Path | None = None) -> Path:
    """Where one document's results go: ``<root>\\<document title>``."""
    base = Path(root).expanduser() if root.strip() else default_download_root(home)
    return base / title_folder_name(Path(title).stem if "." in title else title)


class Library:
    def __init__(self, folder: Path | None = None) -> None:
        self._folder = folder

    @property
    def path(self) -> Path:
        if self._folder is not None:
            return self._folder / LIBRARY_FILE
        from quill.core.scribe.queue import shared_folder

        return shared_folder() / LIBRARY_FILE

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        docs = data.get("documents") if isinstance(data, dict) else None
        return docs if isinstance(docs, dict) else {}

    def record(self, document_id: str, title: str, files: list[str]) -> None:
        from quill.core.storage import write_json_atomic

        if not document_id:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with guarded_write(self.path):
            docs = self._read()
            entry = docs.get(document_id) or {"title": title, "files": []}
            known = list(entry.get("files") or [])
            for item in files:
                if item not in known:
                    known.append(item)
            docs[document_id] = {
                "title": title or entry.get("title", ""),
                "files": known,
                "updated": time.time(),
            }
            write_json_atomic(self.path, {"schema": 1, "documents": docs})

    def files_for(self, document_id: str) -> list[Path]:
        entry = self._read().get(document_id) or {}
        return [Path(p) for p in entry.get("files") or [] if Path(p).exists()]

    def document_for(self, path: Path) -> str:
        """The Scribe document a local file came from, or ``""``."""
        wanted = str(path)
        for document_id, entry in self._read().items():
            if wanted in (entry.get("files") or []):
                return str(document_id)
        return ""


__all__ = [
    "FOLDER_NAME",
    "Library",
    "default_download_root",
    "download_folder",
    "title_folder_name",
]
