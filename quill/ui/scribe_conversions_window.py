"""Scribe Conversions: the family queue, as a window (Scribe PRD 7.4, 7.4a).

A top-level modeless window with a title and its own key. Each app shows its
own instance, read from the shared queue file every second. A report-view
list (one row per job, formats as a column) and a read-only details field
below it: row text changes only when the stage changes; percent, time left
and the full latest message sit in the details field. Rows update in place
and focus stays where the person left it (FUX-06). Nothing here speaks: the
app that runs the queue does (``main_frame_scribe.py``).

Keys: Enter opens the result, Delete removes the row, the Applications key or
Shift+F10 opens every job command, Escape closes and returns focus to where
the window was opened from, F1 answers.
"""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any

from quill.core.scribe import queue as q
from quill.core.scribe.model import labels_sentence
from quill.ui.app_context_help import ensure_help_provider
from quill.ui.dialog_contract import bind_close_button
from quill.ui.dpi import dip_size

TITLE = "Scribe Conversions"
_HELP = (
    "Scribe Conversions lists every Scribe for Documents job in the QUILL family: "
    "its formats, its stage and, below the list, its percent, time left and latest "
    "message. Enter opens the result; the Applications key lists every command for "
    "the job. Powered by Scribe for Documents."
)


class ScribeConversionsWindow:
    def __init__(self, host: Any, *, return_focus: Any = None) -> None:
        import wx

        ensure_help_provider(wx)
        self.wx = wx
        self.host = host
        self.jobs_store = q.JobQueue()
        self._return_focus = return_focus
        self._rows: list[str] = []
        self._mtime = -1.0
        self.frame = wx.Frame(host.frame, title=TITLE, size=dip_size(host.frame, 720, 460))
        self.frame.SetHelpText(_HELP)
        panel = wx.Panel(self.frame)
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(wx.StaticText(panel, label="&Jobs:"), 0, wx.LEFT | wx.TOP, 8)
        self.list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list.SetName("Jobs")
        self.list.SetHelpText(
            "One row per document: its name, the formats wanted, and where it is now."
        )
        for index, (title, width) in enumerate((
            ("Document", 260),
            ("Formats", 200),
            ("Stage", 200),
        )):
            self.list.InsertColumn(index, title, width=dip_size(panel, width, 1)[0])
        root.Add(self.list, 1, wx.EXPAND | wx.ALL, 8)
        root.Add(wx.StaticText(panel, label="&Details:"), 0, wx.LEFT, 8)
        self.details = wx.TextCtrl(panel, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.details.SetName("Details")
        self.details.SetMinSize(dip_size(panel, 400, 70))
        self.details.SetHelpText(
            "Percent, time left, the latest message from Scribe for Documents, and "
            "where the results were saved."
        )
        root.Add(self.details, 0, wx.EXPAND | wx.ALL, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.commands_button = wx.Button(panel, label="&Commands...")
        self.commands_button.SetHelpText("Every command for the selected job.")
        close = wx.Button(panel, wx.ID_CANCEL, label="Close")
        close.SetHelpText("Close Scribe Conversions. Jobs carry on.")
        row.Add(self.commands_button, 0, wx.RIGHT, 6)
        row.AddStretchSpacer()
        row.Add(close, 0)
        root.Add(row, 0, wx.EXPAND | wx.ALL, 8)
        powered = wx.StaticText(panel, label="Powered by Scribe for Documents.")
        root.Add(powered, 0, wx.LEFT | wx.BOTTOM, 8)
        panel.SetSizer(root)
        bind_close_button(self.frame, close, modeless=True)

        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, lambda _e: self._show_details())
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda _e: self.open_result())
        self.list.Bind(wx.EVT_LIST_KEY_DOWN, self._on_list_key)
        self.list.Bind(wx.EVT_CONTEXT_MENU, lambda _e: self.show_commands())
        self.commands_button.Bind(wx.EVT_BUTTON, lambda _e: self.show_commands())
        self.frame.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.frame.Bind(wx.EVT_CLOSE, self._on_close)
        self.timer = wx.Timer(self.frame)
        self.frame.Bind(wx.EVT_TIMER, lambda _e: self.refresh(), self.timer)
        self.refresh(force=True)
        self.timer.Start(1000)

    # ------------------------------------------------------------ data

    def jobs(self) -> list[q.Job]:
        return sorted(self.jobs_store.jobs(), key=lambda j: j.created)

    def refresh(self, *, force: bool = False) -> None:
        mtime = self.jobs_store.mtime()
        if not force and mtime == self._mtime:
            return
        self._mtime = mtime
        jobs = self.jobs()
        selected = self.selected_id()
        ids = [j.id for j in jobs]
        if not jobs:
            self.list.DeleteAllItems()
            self._rows = []
            self.details.ChangeValue(
                "No conversions. Convert a document with Scribe for Documents and it appears here."
            )
            return
        if ids != self._rows:
            # Rows were added or removed: rebuild, keeping the selection.
            self.list.DeleteAllItems()
            for job in jobs:
                index = self.list.InsertItem(self.list.GetItemCount(), job.name)
                self._fill_row(index, job)
            self._rows = ids
            target = ids.index(selected) if selected in ids else 0
            state = self.wx.LIST_STATE_SELECTED | self.wx.LIST_STATE_FOCUSED
            self.list.SetItemState(target, state, state)
        else:
            # In place: only text that changed, so the reader is not interrupted (FUX-06).
            for index, job in enumerate(jobs):
                self._fill_row(index, job)
        self._show_details()

    def _fill_row(self, index: int, job: q.Job) -> None:
        values = (job.name, labels_sentence(job.formats), job.stage_text())
        for column, value in enumerate(values):
            if self.list.GetItemText(index, column) != value:
                self.list.SetItem(index, column, value)

    def selected_id(self) -> str:
        index = self.list.GetFirstSelected()
        return self._rows[index] if 0 <= index < len(self._rows) else ""

    def selected(self) -> q.Job | None:
        job_id = self.selected_id()
        return self.jobs_store.get(job_id) if job_id else None

    def _show_details(self) -> None:
        job = self.selected()
        if job is not None:
            self.details.ChangeValue(job.details())  # silently: the row is what is read

    # ------------------------------------------------------------ commands

    def _commands(self, job: q.Job) -> list[tuple[str, Any]]:
        items: list[tuple[str, Any]] = []
        if job.results:
            items += [("&Open Result", self.open_result), ("Open &Folder", self.open_folder)]
        if job.state == q.WAITING_YOU:
            items.append(("Send &Next File", self.send_next))
        if job.state == q.NEEDS_PASSWORD:
            items.append(("&Enter Password...", self.enter_password))
        if job.state == q.NEEDS_SIGNIN:
            items.append(("&Sign In...", self.host.scribe_sign_in))
        if job.state == q.NEEDS_PAGES:
            items.append(("&Buy Pages", self.buy_pages))
        if job.state not in q.FINISHED:
            items.append(("&Cancel Waiting", self.cancel_waiting))
        if job.state in (q.FAILED, q.CANCELLED, q.NEEDS_PAGES):
            items.append(("&Retry", self.retry))
        if job.document_id:
            items.append(("Ask for &Human Review...", self.human_review))
        items += [("Copy &Details", self.copy_details), ("&Remove from List", self.remove)]
        return items

    def show_commands(self) -> None:
        job = self.selected()
        if job is None:
            return
        menu = self.wx.Menu()
        for label, action in self._commands(job):
            item = menu.Append(self.wx.ID_ANY, label)
            self.frame.Bind(self.wx.EVT_MENU, lambda _e, run=action: run(), item)
        self.frame.PopupMenu(menu)
        menu.Destroy()

    def open_result(self) -> None:
        job = self.selected()
        if job is not None and job.results:
            self.host.scribe_open_result(Path(job.results[0]))

    def open_folder(self) -> None:
        job = self.selected()
        if job is not None and job.results:
            self.host._reveal_in_explorer(Path(job.results[0]))

    def send_next(self) -> None:
        job = self.selected()
        if job is not None:
            self.jobs_store.update(job.id, lambda j: setattr(j, "state", q.WAITING))
            self.host.scribe_queue_wake()

    def enter_password(self) -> None:
        from quill.core.scribe.consent import describe_source
        from quill.ui.scribe_convert_dialog import ask_password

        job = self.selected()
        if job is None:
            return
        source = describe_source(Path(job.source_path)) if job.source_path else None
        if source is None:
            return
        password = ask_password(self.host, self.frame, source)
        if password is not None:
            self.host.scribe_give_password(job.id, password)

    def buy_pages(self) -> None:
        job = self.selected()
        if job is not None:
            from quill.core.scribe.client import ScribeClient

            webbrowser.open(job.purchase_url or ScribeClient().purchase_url())

    def cancel_waiting(self) -> None:
        from quill.core.scribe.runner import cancel_waiting

        job = self.selected()
        if job is not None:
            self.host._announce(
                cancel_waiting(self.jobs_store, job.id, self.host.scribe_app_name())
            )

    def retry(self) -> None:
        job = self.selected()
        if job is not None:
            self.jobs_store.update(job.id, _reset_for_retry)
            self.host.scribe_queue_wake()

    def human_review(self) -> None:
        job = self.selected()
        if job is not None and job.document_id:
            self.host.scribe_human_review(job.document_id, job.name, parent=self.frame)

    def copy_details(self) -> None:
        job = self.selected()
        if job is None or not self.wx.TheClipboard.Open():
            return
        text = (
            f"{job.row_text()}\n{job.details()}\nCode: {job.error_code or 'none'}\n"
            f"Document: {job.document_id or 'not sent'}"
        )
        try:
            self.wx.TheClipboard.SetData(self.wx.TextDataObject(text))
        finally:
            self.wx.TheClipboard.Close()
        self.host._set_status("Details copied.")

    def remove(self) -> None:
        job = self.selected()
        if job is not None:
            self.jobs_store.remove(job.id)
            self.refresh(force=True)

    # ------------------------------------------------------------ keys

    def _on_list_key(self, event: Any) -> None:
        if event.GetKeyCode() == self.wx.WXK_DELETE:
            self.remove()
            return
        event.Skip()

    def _on_char_hook(self, event: Any) -> None:
        wx = self.wx
        key = event.GetKeyCode()
        if key == wx.WXK_ESCAPE:
            self.frame.Close()
            return
        if key == wx.WXK_F10 and event.ShiftDown() or key == wx.WXK_WINDOWS_MENU:
            self.show_commands()
            return
        event.Skip()

    def _on_close(self, event: Any) -> None:
        self.timer.Stop()
        target = self._return_focus
        self.host._scribe_conversions_window = None
        event.Skip()
        if target is not None:
            try:
                self.wx.CallAfter(target.SetFocus)
            except Exception:  # noqa: BLE001 - the old focus may be gone
                pass

    def show(self) -> None:
        self.frame.Show()
        self.frame.Raise()
        self.list.SetFocus()


def _reset_for_retry(job: q.Job) -> None:
    job.state = q.WAITING
    job.message = "Trying again."
    job.error_code = ""
    job.password_tries = 0


def open_path(path: Path) -> bool:
    """Open a file with its default program."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])  # noqa: S603,S607
        else:
            subprocess.Popen(["xdg-open", str(path)])  # noqa: S603,S607
        return True
    except OSError:
        return False


def open_window(host: Any) -> ScribeConversionsWindow:
    """Open (or bring forward) this app's Scribe Conversions window."""
    import wx

    existing = getattr(host, "_scribe_conversions_window", None)
    if existing is not None:
        existing.show()
        return existing
    window = ScribeConversionsWindow(host, return_focus=wx.Window.FindFocus())
    host._scribe_conversions_window = window
    window.show()
    return window


__all__ = ["TITLE", "ScribeConversionsWindow", "open_path", "open_window"]
