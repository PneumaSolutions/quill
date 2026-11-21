"""Sign in to Scribe for Documents: the small waiting dialog (Scribe PRD 7.2).

Opens Scribe's own sign-in page in the default browser (never embedded) and
waits for the redirect on the loopback listener, on a worker thread. The
dialog shows one sentence in a read-only field that has focus, so the screen
reader reads it: "Your browser is open at Scribe for Documents. Sign in
there, then come back to QUILL." Copy Sign-in Link puts the address on the
clipboard for another browser. When the sign-in arrives the same field says
"Signed in to Scribe for Documents as <name>. <n> pages left." and focus
moves to Continue; that is the one announcement. After the time limit it
says "Sign-in timed out. Try again?" with Try Again.
"""

from __future__ import annotations

import webbrowser
from collections.abc import Callable
from typing import Any

from quill.core.scribe.model import ScribeCancelled, ScribeError, plain
from quill.ui.app_context_help import ensure_help_provider
from quill.ui.dialog_contract import apply_modal_ids
from quill.ui.dpi import dip_size

TITLE = "Sign In to Scribe for Documents"


def waiting_sentence(app: str) -> str:
    return f"Your browser is open at Scribe for Documents. Sign in there, then come back to {app}."


def signed_in_sentence(name: str, pages: int | None) -> str:
    who = f" as {name}" if name else ""
    left = f" {pages} pages left." if pages is not None else ""
    return f"Signed in to Scribe for Documents{who}.{left}"


class ScribeSignInDialog:
    """Modal, through the host's ``_show_modal_dialog``. ``signed_in`` is True after."""

    def __init__(self, host: Any, *, store: Any = None, app: str = "QUILL") -> None:
        import wx

        ensure_help_provider(wx)
        self.host = host
        self.app = app
        self.store = store
        self.signed_in = False
        self._task: Any = None
        self._flow: Any = None
        self.dialog = wx.Dialog(host.frame, title=TITLE)
        self.dialog.SetHelpText(
            "Signing in to Scribe for Documents happens on Scribe's own page in your "
            "browser. QUILL never sees your password."
        )
        root = wx.BoxSizer(wx.VERTICAL)
        label = wx.StaticText(self.dialog, label="&Status:")
        root.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        self.message = wx.TextCtrl(
            self.dialog, style=wx.TE_READONLY | wx.TE_MULTILINE | wx.TE_NO_VSCROLL
        )
        self.message.SetName("Status")
        self.message.SetMinSize(dip_size(self.dialog, 420, 70))
        self.message.SetHelpText("What is happening with the sign-in, and what to do next.")
        root.Add(self.message, 1, wx.EXPAND | wx.ALL, 10)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.copy = wx.Button(self.dialog, label="Copy Sign-in &Link")
        self.copy.SetHelpText(
            "Copy the sign-in address, for when the browser did not open or you use "
            "another browser. Paste it there and sign in."
        )
        self.retry = wx.Button(self.dialog, label="&Try Again")
        self.retry.SetHelpText("Open the sign-in page again.")
        self.ok = wx.Button(self.dialog, wx.ID_OK, label="Continue")
        self.ok.SetHelpText("Carry on with what you were doing.")
        self.cancel = wx.Button(self.dialog, wx.ID_CANCEL, label="Cancel")
        self.cancel.SetHelpText("Stop waiting for the sign-in.")
        for button in (self.copy, self.retry, self.ok, self.cancel):
            row.Add(button, 0, wx.RIGHT, 6)
        root.Add(row, 0, wx.ALL, 10)
        self.dialog.SetSizerAndFit(root)
        apply_modal_ids(self.dialog, affirmative_id=wx.ID_OK, escape_id=wx.ID_CANCEL)
        self.copy.Bind(wx.EVT_BUTTON, lambda _e: self._copy_link())
        self.retry.Bind(wx.EVT_BUTTON, lambda _e: self._start())
        self.cancel.Bind(wx.EVT_BUTTON, lambda _e: self._on_cancel())
        self.dialog.Bind(wx.EVT_CLOSE, lambda _e: self._on_cancel())
        self._start()

    # ------------------------------------------------------------ flow

    def _show(self, text: str, *, waiting: bool, done: bool = False) -> None:
        self.message.SetValue(text)
        self.copy.Show(waiting)
        self.retry.Show(not waiting and not done)
        self.ok.Show(done)
        self.cancel.SetLabel("Close" if not waiting and not done else "Cancel")
        self.cancel.Show(not done)
        self.dialog.Layout()
        if done:
            self.ok.SetDefault()
            self.ok.SetFocus()
        else:
            self.message.SetFocus()

    def _start(self) -> None:
        from quill.core.scribe.auth import SignInFlow, TokenStore

        self._flow = SignInFlow(self.store or TokenStore(), app_name=self.app)
        try:
            url = self._flow.start()
        except ScribeError as exc:
            self._show(plain(exc), waiting=False)
            return
        sentence = waiting_sentence(self.app)
        if not _open_browser(url):
            sentence = (
                "Your browser did not open. Use Copy Sign-in Link, paste it into your "
                f"browser, sign in there, then come back to {self.app}."
            )
        self._show(sentence, waiting=True)
        flow = self._flow

        def _run(progress_callback: Callable[..., Any], cancellation_token: Any, **_kw: Any) -> Any:
            from quill.core.scribe.client import ScribeClient

            flow.finish(cancel_requested=cancellation_token.is_cancelled)
            try:
                return ScribeClient(flow.store).account()
            except ScribeError:
                return None  # signed in; the name and pages can wait

        self._task = self.host._task_manager.submit(
            name="scribe-sign-in",
            func=_run,
            on_success=lambda _op, account: self._signed_in(account),
            on_failure=lambda _op, error: self._failed(error),
        )

    def _signed_in(self, account: Any) -> None:
        self.signed_in = True
        name = getattr(account, "name", "") if account is not None else ""
        pages = getattr(account, "pages_remaining", None) if account is not None else None
        sentence = signed_in_sentence(name, pages)
        self._show(sentence, waiting=False, done=True)
        self.host._announce(sentence)

    def _failed(self, error: BaseException) -> None:
        if isinstance(error, ScribeCancelled):
            return
        text = plain(error)
        if getattr(error, "server_code", "") == "timeout":
            text = "Sign-in timed out. Try again?"
        self._show(text, waiting=False)

    def _copy_link(self) -> None:
        import wx

        url = getattr(self._flow, "url", "")
        if not url or not wx.TheClipboard.Open():
            return
        try:
            wx.TheClipboard.SetData(wx.TextDataObject(url))
        finally:
            wx.TheClipboard.Close()
        self.host._set_status("Sign-in link copied.")

    def _on_cancel(self) -> None:
        import wx

        if self._task is not None and not self.signed_in:
            try:
                self.host._task_manager.cancel(self._task.operation_id)
            except Exception:  # noqa: BLE001 - closing must always work
                pass
        if self._flow is not None and not self.signed_in:
            self._flow.cancel()
        self.dialog.EndModal(wx.ID_CANCEL)

    def close(self) -> None:
        self.dialog.Destroy()


def _open_browser(url: str) -> bool:
    try:
        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001 - no browser: Copy Sign-in Link is the way
        return False


def sign_in(host: Any, *, store: Any = None, app: str = "QUILL") -> bool:
    """Show the dialog; True when the person is signed in at the end."""
    dialog = ScribeSignInDialog(host, store=store, app=app)
    try:
        host._show_modal_dialog(dialog.dialog, TITLE)
        return dialog.signed_in
    finally:
        dialog.close()


__all__ = ["TITLE", "ScribeSignInDialog", "sign_in", "signed_in_sentence", "waiting_sentence"]
