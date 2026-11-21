"""The scanner interface every backend implements (wx-free).

A backend answers two questions: which scanners are there, and scan a page
from this one into this folder. Everything else -- choosing a scanner,
remembering it, scanning more pages, turning pages into one PDF -- is
:mod:`quill.core.scanning.session`, written once over this interface, so a
fake backend is all a test needs.

Backends: :mod:`~quill.core.scanning.wia_cli` (Windows, the open-source
``wia-scan`` command-line tool) and :mod:`~quill.core.scanning.imagecapture`
(macOS, Apple's ImageCaptureCore through PyObjC).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from quill.core.error_codes import CodedError

CancelFn = Callable[[], bool]

COLOR_GRAY = "gray"
COLOR_COLOR = "color"
COLOR_MODES: tuple[tuple[str, str], ...] = (
    (COLOR_GRAY, "Gray (best for text)"),
    (COLOR_COLOR, "Color"),
)
DPI_CHOICES: tuple[int, ...] = (150, 200, 300, 400, 600)
DEFAULT_DPI = 300


class ScanError(CodedError):
    """A scan failed, with a sentence a listener can act on."""

    code = "QUILL-SCAN-DEVICE-FAILED"


class ScannerToolMissing(ScanError):
    """The scanning tool or library is not on this computer."""

    code = "QUILL-SCAN-TOOL-MISSING"


class NoScannerFound(ScanError):
    """The backend works but sees no scanner."""

    code = "QUILL-SCAN-DEVICE-NONE"


class ScanCancelled(ScanError):
    """The person stopped the scan."""

    code = "QUILL-SCAN-DEVICE-CANCELLED"


def plain_message(error: BaseException) -> str:
    """The sentence a listener hears: the message without the support code."""
    if error.args and isinstance(error.args[0], str):
        return error.args[0]
    return str(error)


@dataclass(frozen=True, slots=True)
class ScannerInfo:
    """One scanner, as a backend lists it."""

    id: str
    name: str
    has_feeder: bool = False


@dataclass(frozen=True, slots=True)
class ScanOptions:
    """How to scan. ``color`` is :data:`COLOR_GRAY` or :data:`COLOR_COLOR`."""

    dpi: int = DEFAULT_DPI
    color: str = COLOR_GRAY

    @classmethod
    def coerce(cls, dpi: object, color: object) -> ScanOptions:
        """Clean values read from settings: an unknown one is the default."""
        try:
            value = int(str(dpi))
        except ValueError:
            value = DEFAULT_DPI
        if not 50 <= value <= 1200:
            value = DEFAULT_DPI
        mode = str(color or "").strip().lower()
        return cls(value, mode if mode in (COLOR_GRAY, COLOR_COLOR) else COLOR_GRAY)


class ScannerBackend(ABC):
    """What a platform's scanning code provides. Methods block; call them
    from a worker thread, never the UI thread."""

    #: Spoken in errors ("the scanner tool", "Image Capture").
    name: str = "scanner"

    @abstractmethod
    def list_scanners(self) -> list[ScannerInfo]:
        """Every scanner this computer can see. Raises :class:`ScanError`."""

    @abstractmethod
    def scan(
        self,
        device_id: str,
        options: ScanOptions,
        out_dir: Path,
        *,
        cancel_requested: CancelFn | None = None,
    ) -> list[Path]:
        """Scan what is on the glass (or in the feeder) into *out_dir*.

        Returns the image files written, one per page, in page order.
        Raises :class:`ScanError` (or :class:`ScanCancelled`).
        """


__all__ = [
    "COLOR_COLOR",
    "COLOR_GRAY",
    "COLOR_MODES",
    "DEFAULT_DPI",
    "DPI_CHOICES",
    "CancelFn",
    "NoScannerFound",
    "ScanCancelled",
    "ScanError",
    "ScanOptions",
    "ScannerBackend",
    "ScannerInfo",
    "ScannerToolMissing",
    "plain_message",
]
