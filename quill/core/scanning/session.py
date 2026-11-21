"""Scanning, written once over :class:`~quill.core.scanning.base.ScannerBackend`.

Every place in QUILL that scans -- File > Import > Scan Pages, Insert Image, the
Scribe for Documents wizard -- goes through this module:

* :func:`backend_for_platform` picks the backend (the ``pagescan`` tool on
  Windows and macOS; anything else says scanning is not available).
* :func:`choose_scanner` applies the rule "one scanner: use it silently;
  several: ask once and remember" -- it returns the scanner to use, or
  ``None`` when the caller must ask.
* :class:`ScanSession` holds the pages of one job in a private temporary
  folder, scans the next page, and hands the pages over as images or as one
  PDF (:func:`images_to_pdf`, through Pillow).

wx-free and blocking: the UI calls these from a worker thread.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from quill.core.scanning.base import (
    SOURCE_FLATBED,
    CancelFn,
    NoScannerFound,
    ScanError,
    ScannerBackend,
    ScannerInfo,
    ScanOptions,
)

NO_SCANNER_SENTENCE = (
    "QUILL found no scanner. Check that it is turned on and connected, then try again."
)


class UnsupportedPlatformBackend(ScannerBackend):
    """Linux and anything else: scanning is not available, said plainly."""

    name = "scanning"

    def list_scanners(self) -> list[ScannerInfo]:
        raise ScanError("Scanning is available in QUILL on Windows and macOS.")

    def scan(
        self,
        device_id: str,
        options: ScanOptions,
        out_dir: Path,
        *,
        source: str = SOURCE_FLATBED,
        cancel_requested: CancelFn | None = None,
    ) -> list[Path]:
        raise ScanError("Scanning is available in QUILL on Windows and macOS.")


def backend_for_platform(*, tool_path: str = "", platform: str | None = None) -> ScannerBackend:
    """The backend for this computer: ``pagescan`` on Windows and macOS."""
    name = platform or sys.platform
    if name in ("win32", "darwin"):
        from quill.core.scanning.pagescan_cli import PagescanBackend

        return PagescanBackend(configured=tool_path)
    return UnsupportedPlatformBackend()


def choose_scanner(scanners: Sequence[ScannerInfo], remembered_id: str = "") -> ScannerInfo | None:
    """The scanner to use without asking, or ``None`` when the person must choose.

    Raises :class:`NoScannerFound` when there is none at all.
    """
    if not scanners:
        raise NoScannerFound(NO_SCANNER_SENTENCE)
    if remembered_id:
        for scanner in scanners:
            if scanner.id == remembered_id:
                return scanner
    if len(scanners) == 1:
        return scanners[0]
    return None


def scanner_labels(scanners: Sequence[ScannerInfo]) -> list[str]:
    """Names for a chooser; a repeated name gets a number so each is distinct."""
    seen: dict[str, int] = {}
    labels: list[str] = []
    for scanner in scanners:
        name = scanner.name or "Scanner"
        seen[name] = seen.get(name, 0) + 1
        labels.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
    return labels


def images_to_pdf(pages: Sequence[Path], target: Path, *, dpi: int = 300) -> Path:
    """Write *pages* as one PDF, a page each, through Pillow."""
    if not pages:
        raise ScanError("There are no pages to put in a PDF.")
    try:
        from PIL import Image
    except ImportError:
        raise ScanError(
            "QUILL needs its image library (Pillow) to make a PDF from scanned pages."
        ) from None
    images: list[Any] = []
    try:
        for page in pages:
            with Image.open(page) as opened:
                image = opened.convert("L" if opened.mode in ("1", "L", "LA") else "RGB")
                images.append(image)
        target.parent.mkdir(parents=True, exist_ok=True)
        images[0].save(
            target, "PDF", save_all=True, append_images=images[1:], resolution=float(dpi)
        )
    except OSError as exc:
        raise ScanError(f"QUILL could not make a PDF of the scanned pages: {exc}") from None
    finally:
        for image in images:
            image.close()
    return target


class ScanSession:
    """The pages of one scanning job, in a private temporary folder.

    Call :meth:`scan_next` once per page (or per feeder run), then take the
    pages with :attr:`pages`, :meth:`as_pdf` or :meth:`copy_images_to`, and
    finally :meth:`cleanup` (also safe to call twice).
    """

    def __init__(
        self,
        backend: ScannerBackend,
        scanner: ScannerInfo,
        options: ScanOptions,
        *,
        folder: Path | None = None,
    ) -> None:
        self.backend = backend
        self.scanner = scanner
        self.options = options
        self.folder = folder or Path(tempfile.mkdtemp(prefix="quill-scan-"))
        self.pages: list[Path] = []

    def scan_next(self, cancel_requested: CancelFn | None = None) -> list[Path]:
        """Scan into a fresh sub-folder (so names never collide); return the new pages."""
        run = self.folder / f"run-{len(self.pages) + 1:03d}"
        new = self.backend.scan(
            self.scanner.id,
            self.options,
            run,
            source=self.scanner.preferred_source,
            cancel_requested=cancel_requested,
        )
        self.pages.extend(new)
        return new

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def as_pdf(self, name: str = "Scanned pages") -> Path:
        return images_to_pdf(self.pages, self.folder / f"{name}.pdf", dpi=self.options.dpi)

    def best_upload(self, name: str = "Scanned pages") -> Path:
        """One file for a service that takes one: the page itself, or a PDF."""
        if len(self.pages) == 1:
            return self.pages[0]
        return self.as_pdf(name)

    def copy_images_to(self, folder: Path, stem: str) -> list[Path]:
        """Copy the pages to *folder* as ``stem 1.png``, ``stem 2.png``..."""
        folder.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for number, page in enumerate(self.pages, start=1):
            suffix = page.suffix or ".png"
            name = f"{stem}{suffix}" if len(self.pages) == 1 else f"{stem} {number}{suffix}"
            target = _unique(folder / name)
            shutil.copyfile(page, target)
            written.append(target)
        return written

    def cleanup(self) -> None:
        shutil.rmtree(self.folder, ignore_errors=True)


def _unique(path: Path) -> Path:
    candidate, number = path, 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        number += 1
    return candidate


def scan_name(now: Any = None) -> str:
    """ "Scan 2026-11-21 0930": a name that sorts and does not repeat within a minute."""
    import datetime as _dt

    moment = now or _dt.datetime.now()
    return f"Scan {moment:%Y-%m-%d %H%M}"


def documents_folder(home: Path | None = None) -> Path:
    """The person's Documents folder, or their home folder when there is none."""
    base = home or Path.home()
    documents = base / "Documents"
    return documents if documents.is_dir() else base


def folder_is_writable(folder: Path) -> bool:
    import os

    return folder.is_dir() and os.access(folder, os.W_OK)


def unique_path(folder: Path, stem: str, extension: str) -> Path:
    """``folder/stem.ext``, or ``stem (2).ext`` and so on: never an existing file."""
    return _unique(folder / f"{stem}{extension}")


def page_count_sentence(count: int) -> str:
    return "1 page" if count == 1 else f"{count} pages"


__all__ = [
    "NO_SCANNER_SENTENCE",
    "ScanSession",
    "UnsupportedPlatformBackend",
    "backend_for_platform",
    "choose_scanner",
    "documents_folder",
    "folder_is_writable",
    "images_to_pdf",
    "page_count_sentence",
    "scan_name",
    "scanner_labels",
    "unique_path",
]
