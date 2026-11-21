"""My Scribe Documents (Scribe PRD 7.5).

A modal dialog with a Refresh button -- not a live list. It lists every
document in the person's Scribe account, including ones made on the web or
the iPhone: title, pages, date and formats ready. "Show: Documents / Trash"
sits above the list. With nothing to list, a focusable line says "No
documents in your Scribe account."

Commands: Download Formats (the Convert dialog without the upload), Open in
QUILL, Rename (a small dialog with the name selected; no editing in the list,
which is unreliable with JAWS), Move to Trash, and in the Trash view Recover
and Delete Forever (confirmed, Cancel the default, the server's deletion
date named), Ask for Human Review, Copy Details. After Move to Trash,
Recover or Delete, focus moves to the next row (or the previous one if it was
the last) and QUILL says once what happened.
"""

from __future__ import annotations

import webbrowser
from typing import Any

from quill.core.scribe.model import labels_sentence, plain
from quill.ui.app_context_help import ensure_help_provider
from quill.ui.dialog_contract import apply_modal_ids
from quill.ui.dpi import dip_size

TITLE = "My Scribe Documents"
EMPTY = "No documents in your Scribe account."
EMPTY_TRASH = "Nothing in the Scribe trash."


class ScribeDocumentsDialog:
    def __init__(self, host: Any) -> None:
        import wx

        ensure_help_provider(wx)
        self.wx = wx
        self.host = host
        self.documents: list[Any] = []
        self.pages_remaining: int | None = None
        self.dialog = wx.Dialog(
            host.frame, title=TITLE, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER
        )
        self.dialog.SetHelpText(
            "Every document in your Scribe for Documents account. Powered by Scribe for Documents."
        )
        root = wx.BoxSizer(wx.VERTICAL)
        self.show = wx.RadioBox(self.dialog, label="Show", choices=["Documents", "Trash"])
        self.show.SetName("Show")
        self.show.SetHelpText("Your documents, or the ones in the Scribe trash.")
        root.Add(self.show, 0, wx.ALL, 8)
        root.Add(wx.StaticText(self.dialog, label="&Documents:"), 0, wx.LEFT, 8)
        self.list = wx.ListCtrl(self.dialog, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list.SetName("Documents")
        self.list.SetHelpText("Title, pages, date, and the formats ready to download.")
        for index, (title, width) in enumerate((
            ("Title", 240),
            ("Pages", 60),
            ("Date", 110),
            ("Formats ready", 220),
        )):
            self.list.InsertColumn(index, title, width=dip_size(self.dialog, width, 1)[0])
        root.Add(self.list, 1, wx.EXPAND | wx.ALL, 8)
        self.empty = wx.TextCtrl(self.dialog, value=EMPTY, style=wx.TE_READONLY)
        self.empty.SetName("Documents")
        self.empty.SetHelpText("There is nothing to list.")
        self.empty.Hide()
        root.Add(self.empty, 0, wx.EXPAND | wx.ALL, 8)
        self.pages = wx.TextCtrl(self.dialog, style=wx.TE_READONLY)
        self.pages.SetName("Pages left")
        self.pages.SetHelpText("How many Scribe pages you have left.")
        root.Add(self.pages, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        grid = wx.WrapSizer(wx.HORIZONTAL)
        self.buttons: dict[str, Any] = {}
        for key, label, tip in (
            (
                "download",
                "Do&wnload Formats...",
                "Get any format of this document, already made or new.",
            ),
            ("open", "&Open in QUILL", "Download the Word version if needed and open it here."),
            ("rename", "Re&name...", "Give the document a new title in your Scribe account."),
            ("trash", "Move to &Trash", "Move the document to the Scribe trash."),
            ("recover", "Re&cover", "Take the document out of the trash."),
            ("delete", "Delete &Forever...", "Delete the document from Scribe for good."),
            (
                "review",
                "Ask for &Human Review...",
                "Send it to Scribe's paid human review, with a comment.",
            ),
            ("copy", "Cop&y Details", "Copy the document's details to the clipboard."),
            ("refresh", "&Refresh", "Load the list again."),
            ("buy", "&Buy Pages", "Open Scribe's purchase page in your browser."),
        ):
            button = wx.Button(self.dialog, label=label)
            button.SetHelpText(tip)
            grid.Add(button, 0, wx.ALL, 3)
            self.buttons[key] = button
        root.Add(grid, 0, wx.EXPAND | wx.ALL, 5)
        close = wx.Button(self.dialog, wx.ID_CANCEL, label="Close")
        close.SetHelpText("Close My Scribe Documents.")
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.AddStretchSpacer()
        row.Add(close, 0)
        root.Add(row, 0, wx.EXPAND | wx.ALL, 8)
        self.dialog.SetSizer(root)
        self.dialog.SetSize(dip_size(self.dialog, 720, 520))
        apply_modal_ids(self.dialog, escape_id=wx.ID_CANCEL)

        actions = {
            "download": self.download,
            "open": self.open_in_quill,
            "rename": self.rename,
            "trash": lambda: self._move("trash"),
            "recover": lambda: self._move("recover"),
            "delete": self.delete_forever,
            "review": self.review,
            "copy": self.copy_details,
            "refresh": self.refresh,
            "buy": self.buy,
        }
        for key, action in actions.items():
            self.buttons[key].Bind(wx.EVT_BUTTON, lambda _e, run=action: run())
        self.show.Bind(wx.EVT_RADIOBOX, lambda _e: self.refresh())
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda _e: self.download())
        self.refresh()

    # ------------------------------------------------------------ loading

    @property
    def in_trash(self) -> bool:
        return self.show.GetSelection() == 1

    def refresh(self, focus_index: int | None = None) -> None:
        trash = self.in_trash
        self.pages.ChangeValue("Loading...")

        def _run(progress_callback: Any, cancellation_token: Any, **_kw: Any) -> Any:
            from quill.core.scribe.client import ScribeClient

            client = ScribeClient()
            if trash:
                return client.trash_list(), None
            listing = client.documents()
            return listing.documents, listing.pages_remaining

        self.host._task_manager.submit(
            name="scribe-documents",
            func=_run,
            on_success=lambda _op, result: self._loaded(*result, focus_index=focus_index),
            on_failure=lambda _op, error: self._failed(error),
        )

    def _loaded(self, documents: list[Any], pages: int | None, *, focus_index: int | None) -> None:
        wx = self.wx
        self.documents = documents
        if pages is not None:
            self.pages_remaining = pages
        self.pages.ChangeValue(
            f"{self.pages_remaining} pages left." if self.pages_remaining is not None else ""
        )
        self.list.DeleteAllItems()
        for doc in documents:
            index = self.list.InsertItem(self.list.GetItemCount(), doc.title)
            self.list.SetItem(index, 1, str(doc.page_count))
            when = doc.permanently_delete_at if self.in_trash else doc.inserted_at
            self.list.SetItem(index, 2, (("Deleted on " if self.in_trash else "") + when[:10]))
            self.list.SetItem(index, 3, labels_sentence(doc.ready_formats()) or "none yet")
        has = bool(documents)
        self.list.Show(has)
        self.empty.ChangeValue(EMPTY_TRASH if self.in_trash else EMPTY)
        self.empty.Show(not has)
        for key in ("download", "open", "rename", "trash", "review"):
            self.buttons[key].Show(not self.in_trash)
            self.buttons[key].Enable(has)
        for key in ("recover", "delete"):
            self.buttons[key].Show(self.in_trash)
            self.buttons[key].Enable(has)
        self.buttons["copy"].Enable(has)
        self.dialog.Layout()
        if has:
            target = 0 if focus_index is None else max(0, min(focus_index, len(documents) - 1))
            state = wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED
            self.list.SetItemState(target, state, state)
            if focus_index is not None:
                self.list.SetFocus()
        elif focus_index is not None:
            self.empty.SetFocus()

    def _failed(self, error: BaseException) -> None:
        self.pages.ChangeValue(plain(error))
        self.host._announce(plain(error))

    def selected(self) -> Any:
        index = self.list.GetFirstSelected()
        return self.documents[index] if 0 <= index < len(self.documents) else None

    # ------------------------------------------------------------ commands

    def download(self) -> None:
        doc = self.selected()
        if doc is not None:
            self.dialog.EndModal(self.wx.ID_OK)
            self.wx.CallAfter(self.host.scribe_convert_existing, doc)

    def open_in_quill(self) -> None:
        doc = self.selected()
        if doc is not None:
            self.dialog.EndModal(self.wx.ID_OK)
            self.wx.CallAfter(self.host.scribe_open_existing, doc)

    def rename(self) -> None:
        wx = self.wx
        doc = self.selected()
        if doc is None:
            return
        with wx.TextEntryDialog(self.dialog, "New title:", "Rename", doc.title) as dialog:
            dialog.SetHelpText("The document's new title in your Scribe account.")
            if self.host._show_modal_dialog(dialog, "Rename") != wx.ID_OK:
                return
            title = str(dialog.GetValue()).strip()
        if not title or title == doc.title:
            return
        index = self.list.GetFirstSelected()
        self._do(lambda client: client.rename(doc.id, title), f"Renamed to {title}.", index)

    def _move(self, action: str) -> None:
        doc = self.selected()
        if doc is None:
            return
        index = self.list.GetFirstSelected()
        if action == "trash":
            self._do(lambda client: client.trash(doc.id), f"{doc.title} moved to Trash.", index)
        else:
            self._do(lambda client: client.recover(doc.id), f"{doc.title} recovered.", index)

    def delete_forever(self) -> None:
        wx = self.wx
        doc = self.selected()
        if doc is None:
            return
        when = (
            f" Scribe would delete it on {doc.permanently_delete_at[:10]} anyway."
            if doc.permanently_delete_at
            else ""
        )
        dialog = wx.MessageDialog(
            self.dialog,
            f"Delete {doc.title} forever? This cannot be undone.{when}",
            "Delete Forever",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        try:
            answer = self.host._show_modal_dialog(dialog, "Delete Forever")
        finally:
            dialog.Destroy()
        if answer != wx.ID_YES:
            return
        index = self.list.GetFirstSelected()
        self._do(
            lambda client: client.delete_forever(doc.id), f"{doc.title} deleted forever.", index
        )

    def _do(self, call: Any, sentence: str, index: int) -> None:
        def _run(progress_callback: Any, cancellation_token: Any, **_kw: Any) -> None:
            from quill.core.scribe.client import ScribeClient

            call(ScribeClient())

        def _done(_op: Any, _result: Any) -> None:
            self.host._announce(sentence)  # the person caused this, so it is said
            self.refresh(focus_index=index)

        self.host._task_manager.submit(
            name="scribe-document-command",
            func=_run,
            on_success=_done,
            on_failure=lambda _op, error: self._failed(error),
        )

    def review(self) -> None:
        doc = self.selected()
        if doc is not None:
            self.host.scribe_human_review(doc.id, doc.title, parent=self.dialog)

    def copy_details(self) -> None:
        doc = self.selected()
        wx = self.wx
        if doc is None or not wx.TheClipboard.Open():
            return
        from quill.core.scribe.library import Library

        files = Library().files_for(doc.id)
        text = (
            f"{doc.title}\nPages: {doc.page_count}\nAdded: {doc.inserted_at}\n"
            f"Formats ready: {labels_sentence(doc.ready_formats()) or 'none'}\n"
            f"Downloaded to: {'; '.join(str(f) for f in files) or 'not downloaded'}\n"
            f"Document: {doc.id}"
        )
        try:
            wx.TheClipboard.SetData(wx.TextDataObject(text))
        finally:
            wx.TheClipboard.Close()
        self.host._set_status("Details copied.")

    def buy(self) -> None:
        from quill.core.scribe.client import ScribeClient

        webbrowser.open(ScribeClient().purchase_url())

    def close(self) -> None:
        self.dialog.Destroy()


def show(host: Any) -> None:
    dialog = ScribeDocumentsDialog(host)
    try:
        host._show_modal_dialog(dialog.dialog, TITLE)
    finally:
        dialog.close()


__all__ = ["EMPTY", "TITLE", "ScribeDocumentsDialog", "show"]
