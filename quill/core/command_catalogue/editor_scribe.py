"""Catalogue rows for Scribe for Documents and Scan Pages (command-key.md 15).

Opening a Scribe command from a sentence is safe: nothing leaves the
computer until the person presses Send and Convert in the Convert dialog, and
the scanner only scans when asked. The account page (it signs the browser in)
and Sign Out are never reached from a sentence.
"""

from __future__ import annotations

from quill.core.command_catalogue.spec import CommandSpec, Kind, Risk

__all__ = ["SCRIBE_ROWS"]

_S = Risk.SAFE
_N = Risk.NEVER
_D = Kind.OPENS_DIALOG
_TOOLS = "Tools, Scribe for Documents"

SCRIBE_ROWS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "scribe.convert_file",
        "Convert a File with Scribe for Documents",
        "Make a document accessible with Scribe: Word, PDF, braille, audio and more",
        aliases=("convert with scribe", "scribe a file", "make it accessible with scribe"),
        examples=(
            "convert a file with scribe",
            "open scribe convert",
            "make this pdf accessible with scribe",
        ),
        risk=_S,
        kind=_D,
        group="Tools",
        where=f"{_TOOLS}, Convert a File",
    ),
    CommandSpec(
        "scribe.convert_document",
        "Convert This Document with Scribe for Documents",
        "Send the document I have open to Scribe for another format",
        aliases=(
            "convert this document with scribe",
            "scribe this document",
            "send this document to scribe",
        ),
        examples=(
            "convert this document with scribe",
            "scribe this document",
            "send this document to scribe",
        ),
        risk=_S,
        kind=_D,
        group="Tools",
        where=f"{_TOOLS}, Convert This Document",
    ),
    CommandSpec(
        "scribe.convert_link",
        "Convert from a Link with Scribe for Documents",
        "Convert a document from a link to a file",
        aliases=("convert a link with scribe", "scribe a link"),
        examples=(
            "convert a link with scribe",
            "scribe a web link",
            "convert a pdf link with scribe",
        ),
        risk=_S,
        kind=_D,
        group="Tools",
        where=f"{_TOOLS}, Convert from a Link",
    ),
    CommandSpec(
        "scribe.my_documents",
        "My Scribe Documents",
        "See the documents in my Scribe account",
        aliases=("my scribe documents", "scribe library", "scribe documents"),
        examples=(
            "show my scribe documents",
            "open my scribe library",
            "what is in my scribe account",
        ),
        risk=_S,
        kind=_D,
        group="File",
        where="File, My Scribe Documents",
    ),
    CommandSpec(
        "scribe.conversions",
        "Scribe Conversions",
        "See how my Scribe conversions are going",
        aliases=("scribe conversions", "scribe progress", "scribe queue"),
        examples=(
            "show scribe conversions",
            "how are my scribe conversions doing",
            "open the scribe queue",
        ),
        risk=_S,
        kind=_D,
        group="Window",
        where="Window, Scribe Conversions",
    ),
    CommandSpec(
        "scribe.status",
        "Scribe Status",
        "Hear where the current Scribe conversion is",
        aliases=("scribe status", "where is scribe"),
        examples=("what is scribe doing", "scribe status", "how far along is scribe"),
        risk=_S,
        kind=Kind.READOUT,
        group="Tools",
        announces_itself=True,
    ),
    CommandSpec(
        "scribe.repeat",
        "Repeat Last Scribe Message",
        "Hear the last Scribe message again",
        aliases=("repeat scribe message", "say the scribe message again"),
        examples=(
            "repeat the last scribe message",
            "what did scribe say",
            "say that scribe message again",
        ),
        risk=_S,
        kind=Kind.READOUT,
        group="Tools",
        announces_itself=True,
    ),
    CommandSpec(
        "scribe.sign_in",
        "Sign In to Scribe for Documents",
        "Sign in to my Scribe account",
        aliases=("sign in to scribe", "scribe sign in"),
        examples=("sign in to scribe", "log in to scribe", "connect my scribe account"),
        risk=_S,
        kind=_D,
        group="Tools",
        where=f"{_TOOLS}, Sign In",
    ),
    CommandSpec(
        "scribe.sign_out",
        "Sign Out of Scribe for Documents",
        risk=_N,
        group="Tools",
        where=f"{_TOOLS}, Sign Out",
    ),
    CommandSpec(
        "scribe.account",
        "Open My Scribe Account in Browser",
        risk=_N,
        group="Tools",
        where=f"{_TOOLS}, Open My Scribe Account in Browser",
    ),
    CommandSpec(
        "scribe.options",
        "Scribe for Documents Options",
        "Change how Scribe conversions behave",
        aliases=("scribe options", "scribe settings"),
        examples=("open scribe options", "change scribe settings", "scribe preferences"),
        risk=_S,
        kind=_D,
        group="Tools",
        where=f"{_TOOLS}, Options",
    ),
    CommandSpec(
        "scribe.about",
        "About Scribe for Documents",
        "Learn what Scribe for Documents is",
        aliases=("about scribe", "what is scribe"),
        examples=(
            "tell me about scribe",
            "what is scribe for documents",
            "about scribe for documents",
        ),
        risk=_S,
        kind=_D,
        group="Tools",
        where=f"{_TOOLS}, About Scribe for Documents",
    ),
    CommandSpec(
        "scribe.import",
        "Import with Scribe for Documents",
        "Turn a scanned or badly made document into one I can edit",
        aliases=("import with scribe", "scribe import"),
        examples=(
            "import a scanned document with scribe",
            "import with scribe",
            "rebuild this scan with scribe",
        ),
        risk=_S,
        kind=_D,
        group="File",
        where="File, Import, Convert with Scribe for Documents",
    ),
    CommandSpec(
        "scribe.export",
        "Export with Scribe for Documents",
        "Save this document as tagged PDF, Kindle, DAISY with audio or large print",
        aliases=("export with scribe", "scribe export"),
        examples=(
            "export this with scribe",
            "make a tagged pdf with scribe",
            "make a daisy book with scribe",
        ),
        risk=_S,
        kind=_D,
        group="File",
        where="File, Export, With Scribe for Documents",
    ),
    CommandSpec(
        "scribe.braille",
        "Translate to Braille with Scribe for Documents",
        "Turn this document into braille with Scribe's tables",
        aliases=("braille with scribe", "scribe braille"),
        examples=(
            "translate this to braille with scribe",
            "make braille with scribe",
            "scribe this into braille",
        ),
        risk=_S,
        kind=_D,
        group="Braille",
        where="Braille, Translate with Scribe for Documents",
    ),
    CommandSpec(
        "file.scan",
        "Scan Pages",
        "Scan pages from my scanner",
        aliases=("scan pages", "scan a page", "use the scanner"),
        examples=("scan a page", "scan some pages", "scan a letter"),
        risk=_S,
        kind=_D,
        group="File",
        where="File, Import, Scan Pages",
    ),
)
