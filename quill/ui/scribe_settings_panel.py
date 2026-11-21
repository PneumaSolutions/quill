"""The settings groups of the Convert with Scribe for Documents dialog (PRD 7.3, part 3).

Four labelled static boxes -- "Language and voice", "Braille", "Print" and
"Pictures and maths" -- so a screen reader says the group's name on entering
it. All four are created when the dialog is built, in display order, right
after the format check boxes (wx's tab order follows creation order), and are
only shown or hidden to match the chosen formats; hidden groups keep their
values. Each label is created immediately before its control.

The choice lists come from Scribe and are cached for the session. While a
list loads its control says "Loading voices..."; if it fails it says "Voices
could not be loaded" and a Retry button appears. A list is never left empty.
"""

from __future__ import annotations

from typing import Any

from quill.core.scribe.model import (
    GROUP_BRAILLE,
    GROUP_LANGUAGE,
    GROUP_PICTURES,
    GROUP_PRINT,
    GROUPS,
    TTS_GENDERS,
    TTS_RATES,
    DocumentSettings,
    groups_for,
)

#: Server lists, cached for the session (PRD 7.3).
_CACHE: dict[str, Any] = {}


def _rate_label(rate: float) -> str:
    return f"{rate:g} times"


class ScribeSettingsGroups:
    """Builds the four groups into ``parent``/``sizer`` and reads them back."""

    def __init__(self, parent: Any, sizer: Any, host: Any, settings: DocumentSettings) -> None:
        import wx

        self.wx = wx
        self.host = host
        self.parent = parent
        self.sizer = sizer
        self.settings = settings
        self.boxes: dict[str, Any] = {}
        self._languages: list[tuple[str, str]] = []
        self._dialects: dict[str, list[tuple[str, str]]] = {}
        self._voices: dict[str, list[tuple[str, str, bool]]] = {}
        self._tables: list[tuple[str, str]] = []
        self._build_language()
        self._build_braille()
        self._build_print()
        self._build_pictures()
        self.load_lists()

    # ------------------------------------------------------------ building

    def _box(self, title: str) -> tuple[Any, Any]:
        wx = self.wx
        box = wx.StaticBoxSizer(wx.VERTICAL, self.parent, title)
        box.GetStaticBox().SetHelpText(f"Scribe for Documents settings: {title}.")
        self.sizer.Add(box, 0, wx.EXPAND | wx.ALL, 6)
        self.boxes[title] = box
        return box, box.GetStaticBox()

    def _labelled(
        self, box: Any, owner: Any, label: str, control_factory: Any, help_text: str
    ) -> Any:
        wx = self.wx
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(owner, label=label), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        control = control_factory(owner)
        control.SetName(label.replace("&", "").rstrip(":"))
        control.SetHelpText(help_text)
        row.Add(control, 1, wx.EXPAND)
        box.Add(row, 0, wx.EXPAND | wx.ALL, 4)
        return control

    def _build_language(self) -> None:
        wx = self.wx
        box, owner = self._box(GROUP_LANGUAGE)
        self.language = self._labelled(
            box,
            owner,
            "&Language:",
            lambda p: wx.Choice(p, choices=["Loading languages..."]),
            "The document's language. Detect it lets Scribe work it out.",
        )
        self.dialect = self._labelled(
            box,
            owner,
            "&Dialect:",
            lambda p: wx.Choice(p, choices=["Loading dialects..."]),
            "Which form of the language the voice speaks.",
        )
        self.voice = self._labelled(
            box,
            owner,
            "&Voice:",
            lambda p: wx.Choice(p, choices=["Loading voices..."]),
            "The voice for MP3 audio and DAISY.",
        )
        from quill.ui.scribe_voice_sample import VoiceSample

        # Created right after the voice, so Tab reaches it next (PRD 7.3: "beside the voice").
        self.sample = VoiceSample(owner, self.host, self.voice_sample_choice)
        self.voice.GetContainingSizer().Add(self.sample.button, 0, wx.LEFT, 6)
        self.sample.button.Disable()
        self.retry = wx.Button(owner, label="R&etry")
        self.retry.SetHelpText("Load the languages, voices and braille tables again.")
        self.retry.Hide()
        box.Add(self.retry, 0, wx.ALL, 4)
        self.gender = self._labelled(
            box,
            owner,
            "Voice &gender:",
            lambda p: wx.Choice(p, choices=[g[1] for g in TTS_GENDERS]),
            "Used when Scribe detects the language: a male, female or neutral voice.",
        )
        self.rate = self._labelled(
            box,
            owner,
            "Speech &speed:",
            lambda p: wx.Choice(p, choices=[_rate_label(r) for r in TTS_RATES]),
            "How fast the voice reads. 1 is normal speed.",
        )
        genders = [g[0] for g in TTS_GENDERS]
        self.gender.SetSelection(
            genders.index(self.settings.tts_gender) if self.settings.tts_gender in genders else 0
        )
        rates = list(TTS_RATES)
        self.rate.SetSelection(
            rates.index(self.settings.tts_rate)
            if self.settings.tts_rate in rates
            else rates.index(1.0)
        )
        self.language.Bind(wx.EVT_CHOICE, lambda _e: self._fill_dialects())
        self.dialect.Bind(wx.EVT_CHOICE, lambda _e: self._fill_voices())
        self.voice.Bind(wx.EVT_CHOICE, lambda _e: self.sample.refresh())
        self.retry.Bind(wx.EVT_BUTTON, lambda _e: self.load_lists(force=True))

    def _build_braille(self) -> None:
        wx = self.wx
        box, owner = self._box(GROUP_BRAILLE)
        self.table = self._labelled(
            box,
            owner,
            "Braille &table:",
            lambda p: wx.Choice(p, choices=["Loading braille tables..."]),
            "Grade 1, Grade 2, UEB, computer braille and other languages.",
        )
        self.cells = self._labelled(
            box,
            owner,
            "&Cells per line:",
            lambda p: wx.SpinCtrl(p, min=10, max=100, initial=self.settings.braille_cells_per_line),
            "How many braille cells fit on a line. 40 is usual.",
        )
        self.split = wx.CheckBox(owner, label="Split &into pages")
        self.split.SetValue(self.settings.braille_split_into_pages)
        self.split.SetHelpText("Break the braille into pages.")
        box.Add(self.split, 0, wx.ALL, 4)
        self.lines = self._labelled(
            box,
            owner,
            "Li&nes per page:",
            lambda p: wx.SpinCtrl(p, min=10, max=100, initial=self.settings.braille_lines_per_page),
            "How many lines make a braille page. 25 is usual.",
        )

    def _build_print(self) -> None:
        wx = self.wx
        box, owner = self._box(GROUP_PRINT)
        self.large_print = wx.CheckBox(owner, label="La&rge print")
        self.large_print.SetValue(self.settings.large_print)
        self.large_print.SetHelpText("Large print for the tagged PDF and Word versions.")
        box.Add(self.large_print, 0, wx.ALL, 4)

    def _build_pictures(self) -> None:
        wx = self.wx
        box, owner = self._box(GROUP_PICTURES)
        self.describe = wx.CheckBox(owner, label="Describe the &pictures")
        self.describe.SetValue(self.settings.add_image_descriptions)
        self.describe.SetHelpText("Scribe's AI writes a description of each picture.")
        box.Add(self.describe, 0, wx.ALL, 4)
        self.math = wx.CheckBox(owner, label="&Maths as MathML (beta)")
        self.math.SetValue(self.settings.math)
        self.math.SetHelpText("Mathematics comes out as MathML, which screen readers can read.")
        box.Add(self.math, 0, wx.ALL, 4)

    # ------------------------------------------------------------ showing

    def show_for(self, format_ids: list[str]) -> list[str]:
        """Show only the groups the formats need; returns their names."""
        wanted = groups_for(format_ids)
        focused = self.wx.Window.FindFocus()
        for title in GROUPS:
            box = self.boxes[title]
            show = title in wanted
            if not show and focused is not None and _inside(focused, box.GetStaticBox()):
                show = True  # never hide the focused control
            self.sizer.Show(box, show, recursive=True)
        if not self.retry_needed():
            self.retry.Hide()
        self.parent.Layout()
        return wanted

    def retry_needed(self) -> bool:
        return bool(_CACHE.get("failed"))

    # ------------------------------------------------------------ lists

    def load_lists(self, *, force: bool = False) -> None:
        if force:
            _CACHE.clear()
        if "languages" in _CACHE:
            self._lists_loaded(_CACHE)
            return

        def _run(progress_callback: Any, cancellation_token: Any, **_kw: Any) -> dict[str, Any]:
            from quill.core.scribe.client import ScribeClient

            client = ScribeClient()
            return {
                "languages": client.languages(),
                "dialects": client.dialects(),
                "voices": client.voices(),
                "tables": client.braille_tables(),
            }

        def _done(_op: Any, lists: dict[str, Any]) -> None:
            _CACHE.update(lists)
            _CACHE.pop("failed", None)
            self._lists_loaded(lists)

        def _failed(_op: Any, _error: BaseException) -> None:
            _CACHE["failed"] = True
            for control, words in (
                (self.language, "Languages could not be loaded"),
                (self.dialect, "Dialects could not be loaded"),
                (self.voice, "Voices could not be loaded"),
                (self.table, "Braille tables could not be loaded"),
            ):
                control.Set([words])
                control.SetSelection(0)
            self.retry.Show()
            self.parent.Layout()

        self.host._task_manager.submit(
            name="scribe-settings-lists", func=_run, on_success=_done, on_failure=_failed
        )

    def _lists_loaded(self, lists: dict[str, Any]) -> None:
        self._languages = list(lists.get("languages") or [])
        self._dialects = dict(lists.get("dialects") or {})
        self._voices = dict(lists.get("voices") or {})
        self._tables = list(lists.get("tables") or [])
        _set_choices(
            self.language,
            ["Detect it", *[n for n, _c in self._languages]],
            1 + _index([c for _n, c in self._languages], self.settings.language),
        )
        self._fill_dialects()
        names = [n for n, _i in self._tables] or ["Scribe's default"]
        _set_choices(
            self.table,
            names,
            _index([i for _n, i in self._tables], self.settings.braille_translation_table),
        )
        self.retry.Hide()

    def _language_code(self) -> str:
        index = self.language.GetSelection()
        return self._languages[index - 1][1] if 0 < index <= len(self._languages) else ""

    def _fill_dialects(self) -> None:
        rows = self._dialects.get(self._language_code(), [])
        chosen = self.settings.dialects.get(self._language_code(), "")
        _set_choices(
            self.dialect,
            [n for n, _l in rows] or ["The language's usual form"],
            _index([loc for _n, loc in rows], chosen),
        )
        self._fill_voices()

    def _locale(self) -> str:
        rows = self._dialects.get(self._language_code(), [])
        index = self.dialect.GetSelection()
        return rows[index][1] if 0 <= index < len(rows) else ""

    def _fill_voices(self) -> None:
        rows = self._voices.get(self._locale(), [])
        chosen = self.settings.voices.get(self._locale(), "")
        _set_choices(
            self.voice,
            [n for n, _s, _h in rows] or ["Scribe's default voice"],
            _index([s for _n, s, _h in rows], chosen),
        )
        self.sample.refresh()

    def voice_short_name(self) -> str:
        rows = self._voices.get(self._locale(), [])
        index = self.voice.GetSelection()
        return rows[index][1] if 0 <= index < len(rows) else ""

    def voice_sample_choice(self) -> tuple[str, bool]:
        """The chosen voice's short name and whether Scribe has a sample of it."""
        return self.voice_short_name(), self.voice_has_sample()

    def voice_has_sample(self) -> bool:
        rows = self._voices.get(self._locale(), [])
        index = self.voice.GetSelection()
        return bool(rows[index][2]) if 0 <= index < len(rows) else False

    # ------------------------------------------------------------ reading

    def read(self) -> DocumentSettings:
        settings = DocumentSettings.from_json(self.settings.stored())
        # A list that has not loaded (or failed) keeps the incoming choice:
        # pressing Send before the lists arrive must not turn the language
        # into "detect" and save that (independent review Q17).
        if self._languages:
            settings.language = self._language_code()
        if settings.language and self._locale():
            settings.dialects[settings.language] = self._locale()
        if self._locale() and self.voice_short_name():
            settings.voices[self._locale()] = self.voice_short_name()
        settings.tts_gender = TTS_GENDERS[max(0, self.gender.GetSelection())][0]
        settings.tts_rate = TTS_RATES[max(0, self.rate.GetSelection())]
        index = self.table.GetSelection()
        if 0 <= index < len(self._tables):
            settings.braille_translation_table = self._tables[index][1]
        settings.braille_cells_per_line = int(self.cells.GetValue())
        settings.braille_split_into_pages = bool(self.split.GetValue())
        settings.braille_lines_per_page = int(self.lines.GetValue())
        settings.large_print = bool(self.large_print.GetValue())
        settings.add_image_descriptions = bool(self.describe.GetValue())
        settings.math = bool(self.math.GetValue())
        return settings


def _index(values: list[str], wanted: str) -> int:
    return values.index(wanted) if wanted in values else 0


def _set_choices(control: Any, labels: list[str], selection: int) -> None:
    control.Set(labels)
    control.SetSelection(max(0, min(selection, len(labels) - 1)))


def _inside(window: Any, ancestor: Any) -> bool:
    while window is not None:
        if window is ancestor:
            return True
        window = window.GetParent()
    return False


__all__ = ["ScribeSettingsGroups"]
