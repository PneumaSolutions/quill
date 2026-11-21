"""Scanning's settings as QUILL's settings loader reads them (wx-free).

Options > Scanning: the default scanner (chosen with Tools > Choose
Scanner, or the first time several are found), resolution, gray or color,
and where the
pagescan scanner tool lives. Cleaned here rather than inline
in :meth:`quill.core.settings.Settings.from_dict` (GATE-11: that loader is at
its size budget), the same way dictation's fields are.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from quill.core.scanning.base import ScanOptions

#: Field name -> default, in the order they appear on ``Settings``.
SCANNER_FIELD_DEFAULTS: dict[str, Any] = {
    "scanner_device": "",
    "scanner_device_name": "",
    "scanner_dpi": "300",
    "scanner_color": "gray",
    "scanner_tool_path": "",
}


def load_scanner_fields(data: Mapping[str, Any]) -> dict[str, Any]:
    """The scanning fields from a settings document, cleaned."""
    options = ScanOptions.coerce(
        data.get("scanner_dpi", SCANNER_FIELD_DEFAULTS["scanner_dpi"]),
        data.get("scanner_color", SCANNER_FIELD_DEFAULTS["scanner_color"]),
    )
    return {
        "scanner_device": str(data.get("scanner_device", "") or "").strip(),
        "scanner_device_name": str(data.get("scanner_device_name", "") or "").strip(),
        "scanner_dpi": str(options.dpi),
        "scanner_color": options.color,
        "scanner_tool_path": str(data.get("scanner_tool_path", "") or "").strip(),
    }


def scan_options(settings: Any) -> ScanOptions:
    """The resolution and color a settings object asks for."""
    return ScanOptions.coerce(
        getattr(settings, "scanner_dpi", 300), getattr(settings, "scanner_color", "gray")
    )


__all__ = ["SCANNER_FIELD_DEFAULTS", "load_scanner_fields", "scan_options"]
