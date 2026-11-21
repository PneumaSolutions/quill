"""Convert with Scribe for Documents: the one dialog (Scribe PRD 7.3).

The same in every app, opened with the file (or files, or link, or a
document already in the person's Scribe account) chosen. In tab order:

1. **What will be sent** -- a read-only multi-line field, focused when the
   dialog opens, so it can be read again at any time
   (:func:`quill.core.scribe.consent.consent_text`).
2. **What you want back** -- a check box per format; one already made for
   this document says "already made, no pages used".
3. A read-only summary line ("Settings below: Language and voice, Braille"),
   updated without being spoken, then the four settings groups
   (``scribe_settings_panel.py``) and the account-defaults check box.
4. **Pages** -- "This uses about <x> of your <n> pages.", with Buy Pages.
5. **Send and Convert** (default; the name says what the person agrees to),
   or **Convert** when nothing is uploaded, and Cancel. No "don't ask again".

A locked PDF asks for its password only after Send and Convert (part 4):
"Password for Notes.pdf", never pre-filled or saved, with Show password and
three tries.
"""

from __future__ import annotations

import webbrowser
from dataclasses import dataclass, field
from typing import Any

from quill.core.scribe.consent import (
    AI_OFF_NOTE,
    SourceInfo,
    consent_text,
    pages_text,
    pdf_password_ok,
)
from quill.core.scribe.model import FORMATS, DocumentSettings, format_label
from quill.ui.app_context_help import ensure_help_provider
from quill.ui.dialog_contract import apply_modal_ids
from quill.ui.dpi import dip_size

TITLE = "Convert with Scribe for Documents"
MAX_PASSWORD_TRIES = 3


@dataclass(slots=True)
class ConvertRequest:
    """What the person agreed to."""

    formats: list[str]
    settings: DocumentSettings
    save_account_defaults: bool = False
    passwords: dict[str, str] = field(default_factory=dict)  # file name -> password (memory only)


class ScribeConvertDialog:
    def __init__(
        self,
        host: Any,
        sources: list[SourceInfo],
        *,
        formats: list[str],
        settings: DocumentSettings,
        first_upload: bool = False,
        reuse_title: str = "",
        ready_formats: tuple[str, ...] = (),
        ai_off: bool = False,
    ) -> None:
        import wx

        ensure_help_provider(wx)
        self.wx = wx
        self.host = host
        self.sources = sources
        self.reuse_title = reuse_title
        self.ready = set(ready_formats)
        self.ai_off = ai_off
        self.pages_remaining: int | None = None
        self.request: ConvertRequest | None = None
        if ai_off:
            settings.add_image_descriptions = False

        self.dialog = wx.Dialog(
            host.frame, title=TITLE, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER
        )
        self.dialog.SetHelpText(
            "Choose what Scribe for Documents should make. Nothing is sent until you "
            "press Send and Convert. Powered by Scribe for Documents."
        )
        outer = wx.BoxSizer(wx.VERTICAL)
        self.panel = wx.ScrolledWindow(self.dialog, style=wx.VSCROLL)
        self.panel.SetScrollRate(0, 20)
        body = wx.BoxSizer(wx.VERTICAL)

        body.Add(wx.StaticText(self.panel, label="What &will be sent:"), 0, wx.LEFT | wx.TOP, 6)
        self.consent = wx.TextCtrl(self.panel, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.consent.SetName("What will be sent")
        self.consent.SetMinSize(dip_size(self.panel, 520, 110))
        self.consent.SetHelpText("Exactly what goes to Scribe for Documents, and where it stays.")
        self.first_upload = first_upload
        body.Add(self.consent, 0, wx.EXPAND | wx.ALL, 6)

        formats_box = wx.StaticBoxSizer(wx.VERTICAL, self.panel, "What you want back")
        owner = formats_box.GetStaticBox()
        owner.SetHelpText("Check each format you want. Several are fine; the pages are used once.")
        self.checks: dict[str, Any] = {}
        for item in FORMATS:
            label = item.label + (" (already made, no pages used)" if item.id in self.ready else "")
            box = wx.CheckBox(owner, label=label)
            box.SetValue(item.id in formats)
            box.SetHelpText(f"Make {item.label} from the document.")
            box.Bind(wx.EVT_CHECKBOX, lambda _e: self._formats_changed())
            formats_box.Add(box, 0, wx.ALL, 3)
            self.checks[item.id] = box
        body.Add(formats_box, 0, wx.EXPAND | wx.ALL, 6)

        self.summary = wx.TextCtrl(self.panel, style=wx.TE_READONLY)
        self.summary.SetName("Settings below")
        self.summary.SetHelpText("Which settings groups the chosen formats use.")
        body.Add(self.summary, 0, wx.EXPAND | wx.ALL, 6)

        from quill.ui.scribe_settings_panel import ScribeSettingsGroups

        self.groups = ScribeSettingsGroups(self.panel, body, host, settings)
        if ai_off:
            note = wx.StaticText(self.panel, label=AI_OFF_NOTE)
            note.Wrap(500)
            body.Add(note, 0, wx.ALL, 6)

        self.account_defaults = wx.CheckBox(
            self.panel,
            label=(
                "Also save as my Scribe &account defaults "
                "(changes your Scribe website settings too)"
            ),
        )
        self.account_defaults.SetHelpText(
            "Off: these settings are for this document only. On: they also become the "
            "defaults in your Scribe account on the website."
        )
        body.Add(self.account_defaults, 0, wx.ALL, 6)

        body.Add(wx.StaticText(self.panel, label="Pages &used:"), 0, wx.LEFT | wx.TOP, 6)
        self.pages = wx.TextCtrl(self.panel, style=wx.TE_READONLY)
        self.pages.SetName("Pages")
        self.pages.SetHelpText("How many of your Scribe pages this uses.")
        body.Add(self.pages, 0, wx.EXPAND | wx.ALL, 6)
        self.buy = wx.Button(self.panel, label="&Buy Pages")
        self.buy.SetHelpText(
            "Open Scribe's purchase page in your browser. QUILL never takes payment."
        )
        body.Add(self.buy, 0, wx.ALL, 6)
        self.panel.SetSizer(body)

        outer.Add(self.panel, 1, wx.EXPAND)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.AddStretchSpacer()
        self.send = wx.Button(
            self.dialog, wx.ID_OK, label="Convert" if reuse_title else "Send and Convert"
        )
        self.send.SetHelpText(
            "Convert with Scribe for Documents"
            if reuse_title
            else "Send the document to Scribe for Documents and convert it."
        )
        self.send.SetDefault()
        cancel = wx.Button(self.dialog, wx.ID_CANCEL, label="Cancel")
        cancel.SetHelpText("Close without sending anything.")
        buttons.Add(self.send, 0, wx.RIGHT, 6)
        buttons.Add(cancel, 0)
        outer.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
        self.dialog.SetSizer(outer)
        self.dialog.SetSize(dip_size(self.dialog, 640, 620))
        apply_modal_ids(self.dialog, affirmative_id=wx.ID_OK, escape_id=wx.ID_CANCEL)

        self.send.Bind(wx.EVT_BUTTON, lambda _e: self._on_send())
        self.buy.Bind(wx.EVT_BUTTON, lambda _e: self._buy())
        self.dialog.Bind(wx.EVT_ACTIVATE, self._on_activate)
        self.dialog.Bind(wx.EVT_CHILD_FOCUS, self._scroll_to_focus)
        self._formats_changed()
        self._refresh_pages()
        self.consent.SetFocus()

    # ------------------------------------------------------------ parts

    def chosen(self) -> list[str]:
        return [fid for fid, box in self.checks.items() if box.GetValue()]

    def _formats_changed(self) -> None:
        chosen = self.chosen()
        shown = self.groups.show_for(chosen)
        self.summary.ChangeValue(
            "Settings below: " + ", ".join(shown) if shown else "Choose at least one format."
        )
        self.consent.ChangeValue(
            consent_text(
                self.sources,
                first_upload=self.first_upload,
                reuse_title=self.reuse_title,
                describe_pictures=bool(self.groups.describe.GetValue()),
            )
        )
        self.send.Enable(bool(chosen))
        self._update_pages_text()
        self.panel.FitInside()

    def _update_pages_text(self) -> None:
        reuse = bool(self.reuse_title)
        self.pages.ChangeValue(pages_text(self.sources, self.pages_remaining, reuse=reuse))

    def _refresh_pages(self) -> None:
        def _run(progress_callback: Any, cancellation_token: Any, **_kw: Any) -> int | None:
            from quill.core.scribe.client import ScribeClient

            return ScribeClient().account().pages_remaining

        def _done(_op: Any, pages: int | None) -> None:
            self.pages_remaining = pages
            try:
                self._update_pages_text()  # silently (ChangeValue)
            except RuntimeError:
                pass  # the dialog closed meanwhile

        self.host._task_manager.submit(
            name="scribe-pages", func=_run, on_success=_done, on_failure=lambda _op, _e: None
        )

    def _on_activate(self, event: Any) -> None:
        event.Skip()
        if event.GetActive() and getattr(self, "_bought", False):
            self._bought = False
            self._refresh_pages()

    def _buy(self) -> None:
        from quill.core.scribe.client import ScribeClient

        self._bought = True
        webbrowser.open(ScribeClient().purchase_url())

    def _scroll_to_focus(self, event: Any) -> None:
        event.Skip()
        window = event.GetWindow()
        if window is None:
            return
        try:
            x, y = self.panel.CalcUnscrolledPosition(window.GetPosition())
            self.panel.Scroll(-1, max(0, y // 20 - 2))
        except Exception:  # noqa: BLE001 - scrolling is a convenience
            pass

    # ------------------------------------------------------------ send

    def _on_send(self) -> None:
        wx = self.wx
        formats = self.chosen()
        if not formats:
            return
        passwords: dict[str, str] = {}
        for source in self.sources:
            if source.encrypted and source.path is not None:
                password = ask_password(self.host, self.dialog, source)
                if password is None:
                    self.host._set_status(f"{source.name} was not sent: password not accepted")
                    return
                passwords[source.name] = password
        self.request = ConvertRequest(
            formats=formats,
            settings=self.groups.read(),
            save_account_defaults=bool(self.account_defaults.GetValue()),
            passwords=passwords,
        )
        self.dialog.EndModal(wx.ID_OK)

    def close(self) -> None:
        self.dialog.Destroy()


def ask_password(host: Any, parent: Any, source: SourceInfo) -> str | None:
    """Up to three tries; QUILL checks the password itself when it can."""
    import wx

    tries_left = MAX_PASSWORD_TRIES
    message = f"Password for {source.name}:"
    while tries_left > 0:
        dialog = PasswordDialog(parent, source.name, message)
        try:
            if host._show_modal_dialog(dialog.dialog, "Document Password") != wx.ID_OK:
                return None
            password = dialog.value()
        finally:
            dialog.close()
        assert source.path is not None
        verdict = pdf_password_ok(source.path, password)
        if verdict is not False:
            return password  # right, or QUILL cannot tell and Scribe will check
        tries_left -= 1
        if tries_left:
            host._announce(
                f"Password not accepted. {tries_left} {'tries' if tries_left > 1 else 'try'} left."
            )
            message = f"Password not accepted. {tries_left} left. Password for {source.name}:"
    host._announce(f"{source.name} was not sent: password not accepted")
    return None


class PasswordDialog:
    """ "Password for Notes.pdf", never pre-filled, with Show password."""

    def __init__(self, parent: Any, name: str, message: str) -> None:
        import wx

        self.dialog = wx.Dialog(parent, title="Document Password")
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(
            wx.StaticText(self.dialog, label=message.replace("Password for", "&Password for")),
            0,
            wx.ALL,
            8,
        )
        self.hidden = wx.TextCtrl(self.dialog, style=wx.TE_PASSWORD)
        self.hidden.SetName(f"Password for {name}")
        self.hidden.SetHelpText("Used for this one upload; QUILL never saves or logs it.")
        self.shown = wx.TextCtrl(self.dialog)
        self.shown.SetName(f"Password for {name}")
        self.shown.SetHelpText("The password as plain text, so you can check what you typed.")
        self.shown.Hide()
        root.Add(self.hidden, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        root.Add(self.shown, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self.show = wx.CheckBox(self.dialog, label="&Show password")
        self.show.SetHelpText("Show the password as text, for braille and typing echo.")
        root.Add(self.show, 0, wx.ALL, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        ok = wx.Button(self.dialog, wx.ID_OK, label="OK")
        ok.SetHelpText("Use this password.")
        ok.SetDefault()
        cancel = wx.Button(self.dialog, wx.ID_CANCEL, label="Cancel")
        cancel.SetHelpText("Do not send the document.")
        row.Add(ok, 0, wx.RIGHT, 6)
        row.Add(cancel, 0)
        root.Add(row, 0, wx.ALL, 8)
        self.dialog.SetSizerAndFit(root)
        apply_modal_ids(self.dialog, affirmative_id=wx.ID_OK, escape_id=wx.ID_CANCEL)
        self.show.Bind(wx.EVT_CHECKBOX, lambda _e: self._toggle())
        self.hidden.SetFocus()

    def _toggle(self) -> None:
        if self.show.GetValue():
            self.shown.SetValue(self.hidden.GetValue())
            self.hidden.Hide()
            self.shown.Show()
            self.shown.SetFocus()
        else:
            self.hidden.SetValue(self.shown.GetValue())
            self.shown.Hide()
            self.hidden.Show()
            self.hidden.SetFocus()
        self.dialog.Layout()

    def value(self) -> str:
        return str((self.shown if self.show.GetValue() else self.hidden).GetValue())

    def close(self) -> None:
        self.dialog.Destroy()


def label_for(format_id: str) -> str:
    return format_label(format_id)


__all__ = ["TITLE", "ConvertRequest", "PasswordDialog", "ScribeConvertDialog", "ask_password"]
