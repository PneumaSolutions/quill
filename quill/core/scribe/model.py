"""Scribe for Documents: formats, stages, settings and errors (wx-free).

The vocabulary every other Scribe module shares (Scribe PRD 3.2, 3.3, 7.4 and
section 11). Format identifiers, stage names and settings keys are the
server's own (read from Pneuma's API client and server, 2026-11-21); the
words around them are what a listener hears.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from quill.core.error_codes import CodedError

SERVICE_NAME = "Scribe for Documents"
POWERED_BY = "Powered by Scribe for Documents"
SCRIBE_BASE_URL = "https://scribeit.io"
PURCHASE_PATH = "/signup/page_pack"
LINKS: dict[str, str] = {
    "terms": "https://scribeit.io/terms",
    "privacy": "https://scribeit.io/privacy",
    "manual": "https://manual.scribeit.io",
}


# ------------------------------------------------------------------ formats


@dataclass(frozen=True, slots=True)
class ScribeFormat:
    """One output Scribe makes. ``made_from`` is the format it is built from."""

    id: str
    label: str
    extension: str
    made_from: str = ""
    downloadable: bool = True


#: In the Convert dialog's order (PRD 7.3, part 2).
FORMATS: tuple[ScribeFormat, ...] = (
    ScribeFormat("html", "Accessible HTML", ".html", downloadable=False),
    ScribeFormat("docx", "Word", ".docx", "html"),
    ScribeFormat("pdf", "Tagged PDF", ".pdf", "html"),
    ScribeFormat("epub", "EPUB", ".epub", "html"),
    ScribeFormat("mobi", "Kindle (MOBI)", ".mobi", "html"),
    ScribeFormat("daisy", "DAISY (text and audio)", ".zip", "mp3"),
    ScribeFormat("mp3", "MP3 audio", ".mp3", "html"),
    ScribeFormat("brf", "Braille (BRF)", ".brf", "html"),
    ScribeFormat("offline_html", "Web page for offline reading", ".html", "html"),
)
FORMAT_IDS: tuple[str, ...] = tuple(f.id for f in FORMATS)
_BY_ID: dict[str, ScribeFormat] = {f.id: f for f in FORMATS}

#: The order Scribe builds them in: html, the formats made from it, mp3, daisy.
BUILD_ORDER: tuple[str, ...] = (
    "html",
    "docx",
    "pdf",
    "epub",
    "mobi",
    "brf",
    "offline_html",
    "mp3",
    "daisy",
)


def format_by_id(format_id: str) -> ScribeFormat:
    try:
        return _BY_ID[format_id]
    except KeyError:
        raise ScribeError(f"{SERVICE_NAME} has no format called {format_id}.") from None


def format_label(format_id: str) -> str:
    item = _BY_ID.get(format_id)
    return item.label if item is not None else format_id


def in_build_order(format_ids: Iterable[str]) -> list[str]:
    """``format_ids`` without repeats, in Scribe's dependency order."""
    wanted = set(format_ids)
    return [f for f in BUILD_ORDER if f in wanted]


def labels_sentence(format_ids: Iterable[str]) -> str:
    """ "Word and Braille (BRF)", "Word, EPUB and MP3 audio"."""
    names = [format_label(f) for f in format_ids]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


# ------------------------------------------------------------------ stages

STAGE_WORDS: dict[str, str] = {
    "queue": "waiting in Scribe's queue",
    "start": "starting",
    "convert": "converting",
    "add_image_descriptions": "adding image descriptions",
    "complete": "complete",
}


def stage_words(stage: str) -> str:
    return STAGE_WORDS.get(stage, stage.replace("_", " ") or "working")


# ------------------------------------------------------------------ settings

GROUP_LANGUAGE = "Language and voice"
GROUP_BRAILLE = "Braille"
GROUP_PRINT = "Print"
GROUP_PICTURES = "Pictures and maths"
#: Display order (PRD 7.3, part 3).
GROUPS: tuple[str, ...] = (GROUP_LANGUAGE, GROUP_BRAILLE, GROUP_PRINT, GROUP_PICTURES)

#: Which formats need a group. Language and pictures matter to every format.
GROUP_FORMATS: dict[str, frozenset[str]] = {
    GROUP_LANGUAGE: frozenset(FORMAT_IDS),
    GROUP_BRAILLE: frozenset({"brf"}),
    GROUP_PRINT: frozenset({"pdf", "docx"}),
    GROUP_PICTURES: frozenset(FORMAT_IDS),
}

TTS_GENDERS: tuple[tuple[str, str], ...] = (
    ("neutral", "Neutral"),
    ("female", "Female"),
    ("male", "Male"),
)
#: The website's choices (the server checks no range): 0.5x to 4x.
TTS_RATES: tuple[float, ...] = (0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0)


def groups_for(format_ids: Iterable[str]) -> list[str]:
    """The settings groups the chosen formats need, in display order."""
    chosen = set(format_ids)
    return [g for g in GROUPS if chosen & GROUP_FORMATS[g]]


@dataclass(slots=True)
class DocumentSettings:
    """Scribe's per-document settings (and the account defaults' shape).

    ``dialects`` maps a language to a locale; ``voices`` a locale to a voice.
    Defaults are the server's (documents/settings.ex).
    """

    language: str = ""
    dialects: dict[str, str] = field(default_factory=dict)
    voices: dict[str, str] = field(default_factory=dict)
    tts_gender: str = "neutral"
    tts_rate: float = 1.0
    braille_translation_table: str = ""
    braille_cells_per_line: int = 40
    braille_split_into_pages: bool = True
    braille_lines_per_page: int = 25
    large_print: bool = False
    add_image_descriptions: bool = True
    math: bool = False
    notify_when_complete: bool = False

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> DocumentSettings:
        data = dict(data or {})
        defaults = cls()
        values: dict[str, Any] = {}
        for item in fields(cls):
            raw = data.get(item.name, getattr(defaults, item.name))
            current = getattr(defaults, item.name)
            if isinstance(current, dict):
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw) if raw.strip() else {}
                    except ValueError:
                        raw = {}
                raw = {str(k): str(v) for k, v in dict(raw or {}).items()}
            elif isinstance(current, bool):
                raw = bool(raw)
            elif isinstance(current, int):
                try:
                    raw = int(raw)
                except (TypeError, ValueError):
                    raw = current
            elif isinstance(current, float):
                try:
                    raw = float(raw)
                except (TypeError, ValueError):
                    raw = current
            else:
                raw = "" if raw is None else str(raw)
            values[item.name] = raw
        return cls(**values)

    def to_json(self) -> dict[str, Any]:
        """For a write: ``dialects`` and ``voices`` go as JSON strings."""
        data = asdict(self)
        data["dialects"] = json.dumps(self.dialects)
        data["voices"] = json.dumps(self.voices)
        return data

    def stored(self) -> dict[str, Any]:
        """For QUILL's own settings file (plain JSON)."""
        return asdict(self)


# ------------------------------------------------------------------ errors
#
# Section 11 of the Scribe PRD. ``str(error)`` carries the support code (for
# Copy Details); ``error.message`` is the sentence a listener hears.


class ScribeError(CodedError):
    """Anything else the server or QUILL could not do (``server_error``, other)."""

    code = "QUILL-SCRIBE-FAILED"

    def __init__(self, message: str, *, server_code: str = "", **extra: Any) -> None:
        super().__init__(message)
        self.server_code = server_code
        self.extra = extra

    @property
    def message(self) -> str:
        return str(self.args[0]) if self.args else ""


class ScribeSignInRequired(ScribeError):
    """``unauthenticated``, ``invalid_grant``, or a refresh that failed."""

    code = "QUILL-SCRIBE-SIGNIN"


class ScribeNeedsPages(ScribeError):
    """``needs_purchase``: ``extra['purchase_url']`` opens Scribe's purchase page."""

    code = "QUILL-SCRIBE-PAGES"


class ScribePasswordNeeded(ScribeError):
    """``password_required`` or ``invalid_password``; ``extra['wrong']`` says which."""

    code = "QUILL-SCRIBE-PASSWORD"


class ScribeBusy(ScribeError):
    """``rate_limited``."""

    code = "QUILL-SCRIBE-BUSY"


class ScribeGone(ScribeError):
    """``not_found`` or ``forbidden``: no longer in the person's Scribe account."""

    code = "QUILL-SCRIBE-GONE"


class ScribeOffline(ScribeError):
    """The network dropped or the server could not be reached."""

    code = "QUILL-SCRIBE-OFFLINE"


class ScribeSignInLocal(ScribeError):
    """The browser did not open, or the loopback port is blocked."""

    code = "QUILL-SCRIBE-SIGNIN-LOCAL"


class ScribeSaveFailed(ScribeError):
    """A download failed, the disk is full, or the folder is not writable."""

    code = "QUILL-SCRIBE-SAVE"


class ScribeOff(ScribeError):
    """Safe Mode (or no network) when a Scribe command is chosen."""

    code = "QUILL-SCRIBE-OFF"


class ScribeCancelled(ScribeError):
    """The person stopped QUILL waiting; Scribe may still finish the work."""

    code = "QUILL-SCRIBE-CANCELLED"


#: Server error codes and channel reasons -> the class that carries them.
SERVER_ERRORS: dict[str, type[ScribeError]] = {
    "unauthenticated": ScribeSignInRequired,
    "invalid_grant": ScribeSignInRequired,
    "unauthorized": ScribeSignInRequired,
    "needs_purchase": ScribeNeedsPages,
    "password_required": ScribePasswordNeeded,
    "invalid_password": ScribePasswordNeeded,
    "rate_limited": ScribeBusy,
    "not_found": ScribeGone,
    "forbidden": ScribeGone,
}


def error_for(server_code: str, message: str, **extra: Any) -> ScribeError:
    """The coded error for a server code, keeping the server's own sentence."""
    kind = SERVER_ERRORS.get(server_code, ScribeError)
    return kind(
        message or f"{SERVICE_NAME} could not finish that.", server_code=server_code, **extra
    )


def plain(error: BaseException) -> str:
    """What a listener hears: the sentence, never the support code."""
    if isinstance(error, ScribeError):
        return error.message
    if error.args and isinstance(error.args[0], str):
        return error.args[0]
    return str(error)


__all__ = [
    "BUILD_ORDER",
    "FORMATS",
    "FORMAT_IDS",
    "GROUPS",
    "GROUP_BRAILLE",
    "GROUP_LANGUAGE",
    "GROUP_PICTURES",
    "GROUP_PRINT",
    "LINKS",
    "POWERED_BY",
    "SCRIBE_BASE_URL",
    "SERVICE_NAME",
    "STAGE_WORDS",
    "TTS_GENDERS",
    "TTS_RATES",
    "DocumentSettings",
    "ScribeBusy",
    "ScribeCancelled",
    "ScribeError",
    "ScribeFormat",
    "ScribeGone",
    "ScribeNeedsPages",
    "ScribeOff",
    "ScribeOffline",
    "ScribePasswordNeeded",
    "ScribeSaveFailed",
    "ScribeSignInLocal",
    "ScribeSignInRequired",
    "error_for",
    "format_by_id",
    "format_label",
    "groups_for",
    "in_build_order",
    "labels_sentence",
    "plain",
    "stage_words",
]
