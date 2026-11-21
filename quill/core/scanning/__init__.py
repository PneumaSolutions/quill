"""Scanning in QUILL: a page from a flatbed or feeder scanner, as images or a PDF.

wx-free. :mod:`.base` is the interface and its errors, :mod:`.session` the
shared logic every caller uses, and :mod:`.wia_cli` (Windows) and
:mod:`.imagecapture` (macOS) the two backends. The UI is
``quill/ui/main_frame_scanning.py``.
"""

from __future__ import annotations

from quill.core.scanning.base import (
    COLOR_COLOR,
    COLOR_GRAY,
    COLOR_MODES,
    DEFAULT_DPI,
    DPI_CHOICES,
    NoScannerFound,
    ScanCancelled,
    ScanError,
    ScannerBackend,
    ScannerInfo,
    ScannerToolMissing,
    ScanOptions,
)
from quill.core.scanning.session import (
    NO_SCANNER_SENTENCE,
    ScanSession,
    backend_for_platform,
    choose_scanner,
    images_to_pdf,
    page_count_sentence,
    scanner_labels,
)

__all__ = [
    "COLOR_COLOR",
    "COLOR_GRAY",
    "COLOR_MODES",
    "DEFAULT_DPI",
    "DPI_CHOICES",
    "NO_SCANNER_SENTENCE",
    "NoScannerFound",
    "ScanCancelled",
    "ScanError",
    "ScanOptions",
    "ScanSession",
    "ScannerBackend",
    "ScannerInfo",
    "ScannerToolMissing",
    "backend_for_platform",
    "choose_scanner",
    "images_to_pdf",
    "page_count_sentence",
    "scanner_labels",
]
