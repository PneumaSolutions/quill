"""The two record types of the settings registry (split from settings_specs, GATE-11).

:mod:`quill.core.settings_specs` re-exports both, so every import of
``SettingSpec`` and ``SettingGroup`` from there keeps working; modules that
add rows of their own (``settings_specs_services``) import them from here so
there is no import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """One tunable setting, mapped to a :class:`Settings` attribute."""

    key: str
    label: str
    group: str
    kind: str  # "bool" | "choice" | "int" | "float" | "text"
    description: str = ""
    choices: tuple[tuple[str, str], ...] = ()  # (stored value, human label)
    minimum: float | None = None
    maximum: float | None = None
    feature_id: str = ""
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SettingGroup:
    """A labelled cluster of settings, surfaced as one tab."""

    id: str
    title: str
    description: str = ""


__all__ = ["SettingGroup", "SettingSpec"]
