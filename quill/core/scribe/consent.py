"""What the Convert dialog tells a person before anything is sent (wx-free).

Scribe PRD 7.3 parts 1 and 5, and 10.1: the read-only "What will be sent"
text (sizes, pages, where it goes, how long it stays, image descriptions,
copyright), the privacy lines before the first upload, the reuse wording when
nothing is uploaded, and "This uses about <x> of your <n> pages."

Pages are charged once per document, for its first full conversion; every
further format of the same document is free (pneumalith ``documents.ex``,
``required_page_credits``). A format already made is marked "already made,
no pages used".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: Scribe's input types (the manual's 18, plus what the announcement names).
INPUT_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf", ".doc", ".docx", ".rtf", ".odt", ".xls", ".xlsx", ".ods", ".ppt",
    ".pptx", ".odp", ".txt", ".htm", ".html", ".epub", ".mobi", ".tif", ".tiff",
    ".gif", ".jpg", ".jpeg", ".png", ".bmp",
})  # fmt: skip

_PNEUMA = "Pneuma Solutions, United States"
COPYRIGHT = "Convert only documents you own, have permission for, or that are out of copyright."
PRIVACY_FIRST_UPLOAD = (
    "Before your first upload: Pneuma Solutions in the United States processes the "
    "document. It is kept in your Scribe account under your retention setting. "
    "Pictures can be described by Scribe's AI. To delete it, use My Scribe Documents, "
    "Move to Trash, then Delete Forever."
)
AI_OFF_NOTE = (
    "Describe the pictures is off because you have turned AI off in QUILL. You can "
    "still turn it on: it is Scribe's processing, not QUILL's AI."
)


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """One file (or link) the person chose."""

    path: Path | None = None
    url: str = ""
    size: int = 0
    pages: int | None = None
    encrypted: bool = False

    @property
    def name(self) -> str:
        return self.path.name if self.path is not None else self.url


def can_convert(path: Path) -> bool:
    return path.suffix.lower() in INPUT_EXTENSIONS


def describe_source(path: Path) -> SourceInfo:
    """Size, a page count where QUILL can tell, and whether a PDF is locked."""
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    pages: int | None = None
    encrypted = False
    if path.suffix.lower() == ".pdf":
        pages, encrypted = _pdf_facts(path)
    elif path.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".bmp"}:
        pages = 1
    return SourceInfo(path=path, size=size, pages=pages, encrypted=encrypted)


#: Above this, a PDF is never read whole just to describe it (Q18): its start
#: and end say whether it is locked, and pypdf counts pages without reading it all.
_WHOLE_READ_BYTES = 25 * 1024 * 1024
_EDGE_BYTES = 1024 * 1024


def _pdf_facts(path: Path) -> tuple[int | None, bool]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size <= _WHOLE_READ_BYTES:
                head = handle.read()
            else:
                head = handle.read(_EDGE_BYTES)
                handle.seek(max(0, size - _EDGE_BYTES))
                head += handle.read()
    except OSError:
        return None, False
    encrypted = b"/Encrypt" in head
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found,unused-ignore]

        reader = PdfReader(str(path))
        if reader.is_encrypted:
            return None, True
        return len(reader.pages), encrypted
    except Exception:  # noqa: BLE001 - no pypdf, or a damaged file: count the pages by hand
        if size > _WHOLE_READ_BYTES:
            return None, encrypted  # only the edges were read: no count
        count = len(re.findall(rb"/Type\s*/Page[^s]", head))
        return (count or None), encrypted


def pdf_password_ok(path: Path, password: str) -> bool | None:
    """Whether ``password`` opens the PDF; ``None`` when QUILL cannot tell (Scribe will)."""
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found,unused-ignore]

        reader = PdfReader(str(path))
        return bool(reader.decrypt(password))
    except ImportError:
        return None
    except Exception:  # noqa: BLE001
        return None


def size_words(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{round(size / 1024)} KB"
    return f"{size} bytes"


def _one(source: SourceInfo) -> str:
    if source.path is None:
        return source.url
    facts = [size_words(source.size)]
    if source.pages:
        facts.append(f"{source.pages} page{'s' if source.pages != 1 else ''}")
    return f"{source.name} ({', '.join(facts)})"


def sensitive_names(sources: list[SourceInfo]) -> list[str]:
    from quill.core.datalab_ocr import looks_sensitive

    return [s.name for s in sources if s.path is not None and looks_sensitive(s.path)]


def consent_text(
    sources: list[SourceInfo],
    *,
    first_upload: bool = False,
    reuse_title: str = "",
    describe_pictures: bool = True,
    ai_off: bool = False,
) -> str:
    """The "What will be sent" text, line by line (PRD 7.3, part 1).

    With AI turned off in QUILL it also says why Describe the pictures starts
    off (PRD 10.1.4): this field is reachable by Tab, where a static note
    between controls was not (independent review Q16).
    """
    if reuse_title:
        reused = (
            f"No file is sent. Scribe makes these formats from {reuse_title}, already in "
            "your account."
        )
        return f"{reused}\n{AI_OFF_NOTE}" if ai_off else reused
    if len(sources) == 1:
        source = sources[0]
        if source.path is None:
            what = f"Scribe for Documents ({_PNEUMA}) will fetch {source.url}"
        else:
            what = f"{_one(source)} will be sent to Scribe for Documents ({_PNEUMA})"
    else:
        listed = "; ".join(_one(s) for s in sources)
        what = (
            f"{len(sources)} files will be sent to Scribe for Documents ({_PNEUMA}), "
            f"one at a time: {listed}"
        )
    lines = [
        f"{what} and converted there. It stays in your Scribe account under your "
        "retention setting until you delete it."
        + (" Pictures may be described by Scribe's AI." if describe_pictures else ""),
        COPYRIGHT,
    ]
    flagged = sensitive_names(sources)
    if flagged:
        lines.insert(
            0,
            f"Caution: the name {', '.join(flagged)} suggests it may hold private, "
            "medical, legal, financial or other confidential information.",
        )
    if first_upload:
        lines.append(PRIVACY_FIRST_UPLOAD)
    if ai_off:
        lines.append(AI_OFF_NOTE)
    return "\n".join(lines)


def pages_text(
    sources: list[SourceInfo], pages_remaining: int | None, *, reuse: bool = False
) -> str:
    """ "This uses about <x> of your <n> pages." (PRD 7.3, part 5)."""
    left = f"your {pages_remaining} pages" if pages_remaining is not None else "your pages"
    if reuse:
        return f"This uses none of {left}: the document is already in your account."
    counts = [s.pages for s in sources]
    if counts and all(c is not None for c in counts):
        used = sum(c or 0 for c in counts)
        return f"This uses about {used} of {left}."
    return f"This uses about one page for each page of the document, from {left}."


__all__ = [
    "AI_OFF_NOTE",
    "COPYRIGHT",
    "INPUT_EXTENSIONS",
    "PRIVACY_FIRST_UPLOAD",
    "SourceInfo",
    "can_convert",
    "consent_text",
    "describe_source",
    "pages_text",
    "pdf_password_ok",
    "sensitive_names",
    "size_words",
]
