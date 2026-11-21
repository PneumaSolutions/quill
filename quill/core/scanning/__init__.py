"""Scanning in QUILL: a page from a flatbed or feeder scanner, as images or a PDF.

wx-free. :mod:`.base` is the interface and its errors, :mod:`.session` the
shared logic every caller uses, and :mod:`.pagescan_cli` the backend on
Windows and macOS (the ``pagescan`` command-line tool). The UI is
``quill/ui/main_frame_scanning.py``.
"""

from __future__ import annotations

from quill.core.scanning.base import (
    COLOR_COLOR,
    COLOR_GRAY,
    COLOR_MODES,
    DEFAULT_DPI,
    DPI_CHOICES,
    SOURCE_FEEDER,
    SOURCE_FLATBED,
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
    "SOURCE_FEEDER",
    "SOURCE_FLATBED",
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
