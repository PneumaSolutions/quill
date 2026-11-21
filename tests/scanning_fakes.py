"""A fake scanner backend for every test that scans (nothing touches hardware)."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from quill.core.scanning.base import (
    SOURCE_FLATBED,
    CancelFn,
    ScanError,
    ScannerBackend,
    ScannerInfo,
    ScanOptions,
)


def _png_1x1_gray() -> bytes:
    """A real 1x1 gray PNG, so Pillow (when installed) can open the pages."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00\x80"))
        + chunk(b"IEND", b"")
    )


PNG_BYTES = _png_1x1_gray()


class FakeBackend(ScannerBackend):
    """Lists the scanners it was given and "scans" one PNG page per call."""

    name = "the fake scanner"

    def __init__(
        self,
        scanners: list[ScannerInfo] | None = None,
        *,
        fail_with: ScanError | None = None,
        pages_per_scan: int = 1,
    ) -> None:
        self.scanners = (
            [ScannerInfo("dev-1", "Flatbed One")] if scanners is None else list(scanners)
        )
        self.fail_with = fail_with
        self.pages_per_scan = pages_per_scan
        self.scans: list[tuple[str, ScanOptions, Path]] = []
        self.sources: list[str] = []

    def list_scanners(self) -> list[ScannerInfo]:
        if self.fail_with is not None:
            raise self.fail_with
        return list(self.scanners)

    def scan(
        self,
        device_id: str,
        options: ScanOptions,
        out_dir: Path,
        *,
        source: str = SOURCE_FLATBED,
        cancel_requested: CancelFn | None = None,
    ) -> list[Path]:
        if self.fail_with is not None:
            raise self.fail_with
        self.scans.append((device_id, options, out_dir))
        self.sources.append(source)
        out_dir.mkdir(parents=True, exist_ok=True)
        pages = []
        for number in range(1, self.pages_per_scan + 1):
            page = out_dir / f"scan-{number:03d}.png"
            page.write_bytes(PNG_BYTES)
            pages.append(page)
        return pages
