"""Scribe for Documents' default keys, merged into QUILL's keymap (PRD 8.1.8).

Keys are deliberately few: Convert a File, Scribe Conversions (its own key,
PRD 7.4), Scribe Status and Repeat Last Scribe Message (both keyed, PRD
7.4a), on the last four free QUILL-key Shift letters. Every other Scribe
command is a row in Tools > Scribe for Documents (or File, Import, Export,
Braille, Window); each one's written reason is in the family gate's
``UNBOUND_WITH_REASON`` (tests/unit/core/test_family_rules_and_gates.py).
"""

from __future__ import annotations

SCRIBE_KEYS: dict[str, str] = {
    "scribe.convert_file": "Ctrl+Shift+Grave, Shift+B",
    "scribe.conversions": "Ctrl+Shift+Grave, Shift+W",
    "scribe.status": "Ctrl+Shift+Grave, Shift+U",
    "scribe.repeat": "Ctrl+Shift+Grave, Shift+Y",
    "scribe.convert_document": "",
    "scribe.convert_link": "",
    "scribe.my_documents": "",
    "scribe.sign_in": "",
    "scribe.sign_out": "",
    "scribe.account": "",
    "scribe.options": "",
    "scribe.about": "",
    "scribe.import": "",
    "scribe.export": "",
    "scribe.braille": "",
}


def default_bindings() -> dict[str, str]:
    return dict(SCRIBE_KEYS)


__all__ = ["SCRIBE_KEYS", "default_bindings"]
