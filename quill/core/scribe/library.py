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
import zipfile
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


#: Names Windows keeps for devices, with or without an extension ("CON.txt").
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.I)
#: A folder name stays within this many UTF-8 bytes (255 is the limit on most
#: file systems, and the file inside needs room too).
_FOLDER_BYTES = 120


def cut_to_bytes(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` UTF-8 bytes, never mid-character."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore")


def title_stem(title: str) -> str:
    """A title without its file extension -- only a real input extension.

    "Notes.pdf" -> "Notes", but "1. Introduction" and "Report v2.5 draft" stay
    whole (independent review Q23: ``Path.stem`` cut them at the last dot).
    """
    from quill.core.scribe.consent import INPUT_EXTENSIONS

    suffix = Path(title).suffix
    if suffix and suffix.lower() in INPUT_EXTENSIONS and len(title) > len(suffix):
        return title[: -len(suffix)]
    return title


def title_folder_name(title: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", title).strip(" .")
    cleaned = cut_to_bytes(re.sub(r"\s+", " ", cleaned), _FOLDER_BYTES).strip(" .")
    if _RESERVED.match(cleaned):
        cleaned = f"_{cleaned}"
    return cleaned or "Untitled"


def download_folder(title: str, root: str = "", *, home: Path | None = None) -> Path:
    """Where one document's results go: ``<root>\\<document title>``."""
    base = Path(root).expanduser() if root.strip() else default_download_root(home)
    return base / title_folder_name(title_stem(title))


def unpack(archive: Path, target: Path) -> list[str]:
    """Unpack a downloaded zip into ``target``; returns the member names.

    A member that would land outside ``target`` is skipped (zip slip).
    """
    root = target.resolve()
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        for member in names:
            if not (target / member).resolve().is_relative_to(root):
                continue  # never write outside the folder
            bundle.extract(member, target)
    return names


def daisy_navigation(archive: Path) -> Path | None:
    """A Scribe DAISY result (a zip of a DAISY 3 book) unpacked beside itself,
    as its navigation file (``.ncx``), which Media Player opens straight away
    (PRD 8.3). Unpacked once: a later call finds the folder already there.
    None when the file is not a DAISY zip."""
    if archive.suffix.lower() == ".ncx":
        return archive
    if not zipfile.is_zipfile(archive):
        return None
    with zipfile.ZipFile(archive) as bundle:
        if not any(n.lower().endswith(".ncx") for n in bundle.namelist()):
            return None
    target = archive.with_name(f"{archive.stem} (DAISY)")
    found = sorted(target.rglob("*.ncx")) if target.is_dir() else []
    if not found:
        names = unpack(archive, target)
        found = [target / n for n in sorted(names, key=len) if n.lower().endswith(".ncx")]
    return found[0] if found and found[0].is_file() else None


class Library:
    def __init__(self, folder: Path | None = None) -> None:
        self._folder = folder

    @property
    def path(self) -> Path:
        if self._folder is not None:
            return self._folder / LIBRARY_FILE
        from quill.core.scribe.queue import shared_folder

        return shared_folder() / LIBRARY_FILE

    def _read(self, *, strict: bool = False) -> dict[str, Any]:
        """The records. ``strict`` (for a write) raises :class:`OSError` for a
        file that is there but unreadable, so it is never written over (Q11)."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            if strict:
                raise OSError(f"{self.path.name} could not be read; left as it is") from exc
            return {}
        docs = data.get("documents") if isinstance(data, dict) else None
        if not isinstance(docs, dict):
            if strict:
                raise OSError(f"{self.path.name} is not a record QUILL understands")
            return {}
        return docs

    def record(self, document_id: str, title: str, files: list[str]) -> None:
        from quill.core.storage import write_json_atomic

        if not document_id:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with guarded_write(self.path):
            docs = self._read(strict=True)
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
    "daisy_navigation",
    "unpack",
    "FOLDER_NAME",
    "Library",
    "default_download_root",
    "download_folder",
    "title_folder_name",
]
