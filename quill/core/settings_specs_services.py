"""Options pages for the online services: Scribe for Documents and Scanning.

Split from :mod:`quill.core.settings_specs` under GATE-11 (extract, never
rebaseline). Scribe PRD 7.7: every field here is in the settings vocabulary
and settings documentation gates; tokens never go in settings.
"""

from __future__ import annotations

from quill.core.settings_spec_types import SettingGroup, SettingSpec

SERVICE_GROUPS: tuple[SettingGroup, ...] = (
    SettingGroup(
        "scribe",
        "Scribe for Documents",
        (
            "Powered by Scribe for Documents (Pneuma Solutions, United States): your "
            "documents are converted in your own Scribe account and kept there under "
            "your retention setting; pictures may be described by Scribe's AI; delete "
            "them from My Scribe Documents. Sign in, sign out and your account are in "
            "Tools > Scribe for Documents."
        ),
    ),
    SettingGroup(
        "scanning",
        "Scanning",
        (
            "Your scanner's resolution and color. Tools > Choose Scanner "
            "picks which scanner QUILL uses."
        ),
    ),
)

SERVICE_SPECS: tuple[SettingSpec, ...] = (
    SettingSpec(
        "scribe_default_formats",
        "Default format",
        "scribe",
        "choice",
        "What the Convert with Scribe for Documents dialog checks first, until you choose others.",
        choices=(
            ("docx", "Word"),
            ("html", "Accessible HTML"),
            ("pdf", "Tagged PDF"),
            ("epub", "EPUB"),
            ("mobi", "Kindle (MOBI)"),
            ("daisy", "DAISY (text and audio)"),
            ("mp3", "MP3 audio"),
            ("brf", "Braille (BRF)"),
            ("offline_html", "Web page for offline reading"),
        ),
        feature_id="core.ocr",
        keywords=("scribe", "format", "word", "braille", "daisy", "mp3"),
    ),
    SettingSpec(
        "scribe_download_folder",
        "Download folder",
        "scribe",
        "text",
        (
            "Where Scribe results are saved, one folder per document. Leave blank for "
            "Documents, Scribe for Documents."
        ),
        feature_id="core.ocr",
        keywords=("scribe", "download", "folder", "save"),
    ),
    SettingSpec(
        "scribe_open_results",
        "Open each result when it is ready",
        "scribe",
        "choice",
        (
            "Yes opens the result in the right tool when it is ready. Offer adds Open "
            "Result to the job and the result sentence, never a dialog. No only says it."
        ),
        choices=(("yes", "Yes"), ("offer", "Offer"), ("no", "No")),
        feature_id="core.ocr",
        keywords=("scribe", "open", "result"),
    ),
    SettingSpec(
        "scribe_offer_low_text_pdf",
        "Offer Scribe when a PDF has little or no text",
        "scribe",
        "bool",
        "When a PDF opens with little or no text, the OCR offer includes Scribe for Documents.",
        feature_id="core.ocr",
        keywords=("scribe", "pdf", "scanned", "ocr"),
    ),
    SettingSpec(
        "scribe_speak_stages",
        "Speak each stage",
        "scribe",
        "choice",
        (
            "Whether QUILL says each Scribe stage, and 25, 50 and 75 percent. Start, "
            "finish and errors are always said."
        ),
        choices=(
            ("front", "Only while a QUILL app is in front"),
            ("always", "Always"),
            ("never", "Never"),
        ),
        feature_id="core.ocr",
        keywords=("scribe", "speak", "stage", "progress", "announce"),
    ),
    SettingSpec(
        "scribe_batch_exception",
        "Send several files without asking between them",
        "scribe",
        "bool",
        (
            "Off until Pneuma Solutions allows it: with several files, each next file "
            "waits for Send Next File in Scribe Conversions. Still one at a time."
        ),
        feature_id="core.ocr",
        keywords=("scribe", "batch", "queue", "several"),
    ),
    SettingSpec(
        "scanner_dpi",
        "Scan resolution (dots per inch)",
        "scanning",
        "choice",
        (
            "How finely pages are scanned. 300 is best for reading text; higher "
            "makes bigger files and slower scans."
        ),
        choices=tuple((str(dpi), f"{dpi} dpi") for dpi in (150, 200, 300, 400, 600)),
        feature_id="core.ocr",
        keywords=("scan", "scanner", "dpi", "resolution"),
    ),
    SettingSpec(
        "scanner_color",
        "Scan in",
        "scanning",
        "choice",
        "Gray is best for text and makes smaller files. Choose color for photos.",
        choices=(("gray", "Gray (best for text)"), ("color", "Color")),
        feature_id="core.ocr",
        keywords=("scan", "scanner", "color", "gray", "grey"),
    ),
    SettingSpec(
        "scanner_device_name",
        "Default scanner",
        "scanning",
        "text",
        (
            "The scanner QUILL uses without asking. Leave blank to be asked when "
            "more than one scanner is found. Tools > Choose Scanner sets it."
        ),
        feature_id="core.ocr",
        keywords=("scan", "scanner", "default", "device"),
    ),
    SettingSpec(
        "scanner_tool_path",
        "Scanner tool location",
        "scanning",
        "text",
        (
            "Where the pagescan scanner tool is: the full path of the file or its "
            "folder. Leave blank to use the one installed with QUILL."
        ),
        feature_id="core.ocr",
        keywords=("scan", "scanner", "pagescan", "wia", "tool", "path"),
    ),
)

__all__ = ["SERVICE_GROUPS", "SERVICE_SPECS"]
