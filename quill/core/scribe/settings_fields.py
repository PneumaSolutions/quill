"""Options > Scribe for Documents: QUILL's own Scribe settings (PRD 7.7).

Local choices only; Scribe's account defaults live in the person's Scribe
account and are changed only when they tick the box that says so. Tokens
never go in settings. Parsed here rather than inline in
:meth:`quill.core.settings.Settings.from_dict` (GATE-11), as dictation's and
scanning's fields are.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from quill.core.scribe.model import FORMAT_IDS, DocumentSettings, in_build_order

OPEN_RESULTS: tuple[tuple[str, str], ...] = (
    ("yes", "Yes"),
    ("offer", "Offer"),
    ("no", "No"),
)
SPEAK_STAGES: tuple[tuple[str, str], ...] = (
    ("front", "Only while a QUILL app is in front"),
    ("always", "Always"),
    ("never", "Never"),
)

SCRIBE_FIELD_DEFAULTS: dict[str, Any] = {
    "scribe_default_formats": "docx",
    "scribe_download_folder": "",
    "scribe_open_results": "yes",
    "scribe_offer_low_text_pdf": True,
    "scribe_speak_stages": "front",
    "scribe_batch_exception": False,
    "scribe_last_formats": "",
    "scribe_local_settings": {},
    "scribe_privacy_told": False,
}


def _formats(value: object, default: str) -> str:
    chosen = [f.strip() for f in str(value or "").split(",") if f.strip() in FORMAT_IDS]
    return ",".join(in_build_order(chosen)) if chosen else default


def _choice(value: object, allowed: tuple[tuple[str, str], ...], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in {key for key, _label in allowed} else default


def load_scribe_fields(data: Mapping[str, Any]) -> dict[str, Any]:
    local = data.get("scribe_local_settings")
    return {
        "scribe_default_formats": _formats(data.get("scribe_default_formats"), "docx"),
        "scribe_download_folder": str(data.get("scribe_download_folder", "") or "").strip(),
        "scribe_open_results": _choice(data.get("scribe_open_results"), OPEN_RESULTS, "yes"),
        "scribe_offer_low_text_pdf": bool(data.get("scribe_offer_low_text_pdf", True)),
        "scribe_speak_stages": _choice(data.get("scribe_speak_stages"), SPEAK_STAGES, "front"),
        "scribe_batch_exception": bool(data.get("scribe_batch_exception", False)),
        "scribe_last_formats": _formats(data.get("scribe_last_formats"), ""),
        "scribe_local_settings": DocumentSettings.from_json(local).stored()
        if isinstance(local, dict) and local
        else {},
        "scribe_privacy_told": bool(data.get("scribe_privacy_told", False)),
    }


def chosen_formats(settings: Any, app_default: str = "") -> list[str]:
    """The formats the Convert dialog starts with: the last choice, else the
    app's default, else Options' default (PRD 7.3, part 2)."""
    for value in (
        getattr(settings, "scribe_last_formats", ""),
        app_default,
        getattr(settings, "scribe_default_formats", "docx"),
    ):
        formats = [f for f in str(value or "").split(",") if f in FORMAT_IDS]
        if formats:
            return formats
    return ["docx"]


__all__ = [
    "OPEN_RESULTS",
    "SCRIBE_FIELD_DEFAULTS",
    "SPEAK_STAGES",
    "chosen_formats",
    "load_scribe_fields",
]
