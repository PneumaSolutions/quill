"""Scribe for Documents in QUILL: the queue, who speaks, and results (PRD 7.4, 7.4a, 9).

This app's :class:`~quill.core.scribe.runner.QueueRunner` runs the family
queue only while it holds the runner lock, so exactly one app uploads and
speaks. Sentences arrive here on the UI thread:

* start, result and error sentences are always said;
* stages and the 25/50/75 percent milestones follow Options > Speak each
  stage ("Only while a QUILL app is in front" by default), and stay silent
  when the focused row of Scribe Conversions is the job that changed,
  because the screen reader already reads that row;
* a job that needs the person says so once and never opens a dialog.

A result opens on its own only in the app that started the job, only if that
app is in front, and only when Options > Open each result says Yes (PRD
7.4a and 9): Word and HTML open in QUILL (an Import result as a new unsaved
tab named "<stem> (Scribe)"), BRF in the braille view, MP3 and DAISY with
the computer's player; PDF, EPUB, MOBI and the offline web page are saved.
Otherwise the result sentence ends "Open it from Scribe Conversions."
"""

from __future__ import annotations

import os
import webbrowser
from pathlib import Path
from typing import Any

from quill.core.scribe import queue as q
from quill.core.scribe.model import LINKS, POWERED_BY, format_label, plain

APP_NAME = "QUILL"

#: Which formats open where when they are ready (PRD section 9).
OPENS_IN_QUILL = frozenset({"docx", "html"})
OPENS_IN_BRAILLE = frozenset({"brf"})
OPENS_IN_PLAYER = frozenset({"mp3", "daisy"})


class ScribeQueueMixin:
    _scribe_runner: Any = None
    _scribe_last_message: str = ""
    _scribe_conversions_window: Any = None
    _scribe_resumed_done: list[Any]

    # ------------------------------------------------------------ the runner

    def scribe_app_name(self) -> str:
        return APP_NAME

    def _scribe_ensure_runner(self) -> Any:
        if self._scribe_runner is None:
            from quill.core.scribe.runner import QueueRunner

            call_after = self._wx.CallAfter
            self._scribe_runner = QueueRunner(
                app=APP_NAME,
                on_say=lambda kind, text, job: call_after(self._scribe_say, kind, text, job),
                on_result=lambda job: call_after(self._scribe_job_done, job),
            )
            self._scribe_runner.start()
            import atexit

            atexit.register(self._scribe_runner.stop)  # hand the lock on at exit
        return self._scribe_runner

    def scribe_queue_wake(self) -> None:
        self._scribe_ensure_runner().wake()

    def scribe_give_password(self, job_id: str, password: str) -> None:
        self._scribe_ensure_runner().give_password(job_id, password)

    def _scribe_resume_on_startup(self) -> None:
        """Pick up jobs an app left unfinished, so closing an app loses nothing."""
        try:
            unfinished = [j for j in q.JobQueue().jobs() if j.state not in q.FINISHED]
        except OSError:
            return
        if unfinished and not os.environ.get("QUILL_SAFE_MODE") == "1":
            self._scribe_ensure_runner()

    def _scribe_stop_runner(self) -> None:
        runner, self._scribe_runner = self._scribe_runner, None
        if runner is not None:
            runner.stop()

    # ------------------------------------------------------------ speaking

    def _scribe_stage_speech_on(self) -> bool:
        mode = str(getattr(getattr(self, "settings", None), "scribe_speak_stages", "front"))
        if mode == "never":
            return False
        if mode == "always":
            return True
        try:
            return bool(self.frame.IsActive()) or self._scribe_window_active()
        except Exception:  # noqa: BLE001
            return True

    def _scribe_window_active(self) -> bool:
        window = self._scribe_conversions_window
        try:
            return window is not None and bool(window.frame.IsActive())
        except Exception:  # noqa: BLE001
            return False

    def _scribe_row_has_focus(self, job: Any) -> bool:
        window = self._scribe_conversions_window
        if window is None or not self._scribe_window_active():
            return False
        try:
            return window.selected_id() == job.id and window.list.HasFocus()
        except Exception:  # noqa: BLE001
            return False

    def _scribe_say(self, kind: str, text: str, job: Any) -> None:
        if not text:
            return  # a silent recovery
        if kind in ("stage", "percent"):
            if not self._scribe_stage_speech_on() or self._scribe_row_has_focus(job):
                return
        self._scribe_last_message = text
        self._announce(text)

    def scribe_status(self) -> None:
        """Scribe Status: the current job, once, without opening anything."""
        jobs = [j for j in q.JobQueue().jobs() if j.state not in q.FINISHED]
        if not jobs:
            self._announce("No Scribe for Documents conversions are waiting.")
            return
        active = [j for j in jobs if j.state in q.ACTIVE] or jobs
        self._announce(q.status_sentence(active[0]))

    def scribe_repeat_last(self) -> None:
        self._announce(self._scribe_last_message or "Scribe for Documents has said nothing yet.")

    # ------------------------------------------------------------ results

    def _scribe_job_done(self, job: Any) -> None:
        if job.resumed and job.owner_pid != os.getpid():
            self._scribe_finished_while_closed(job)
            return
        opened = ""
        mode = str(getattr(getattr(self, "settings", None), "scribe_open_results", "yes"))
        here = job.app == APP_NAME and job.owner_pid == os.getpid()
        front = self._scribe_front()
        if here and front and mode == "yes" and job.results:
            opened = self._scribe_open_default(job)
        offer = mode == "offer" or (bool(job.results) and not opened and mode != "no")
        sentence = q.result_sentence(job, opened=opened, app=APP_NAME, offer=offer)
        self._scribe_last_message = sentence
        self._announce(sentence)

    def _scribe_front(self) -> bool:
        try:
            return bool(self.frame.IsActive())
        except Exception:  # noqa: BLE001
            return False

    def _scribe_finished_while_closed(self, job: Any) -> None:
        """Collect jobs finished for an app that had closed; say them in one sentence."""
        if not hasattr(self, "_scribe_resumed_done"):
            self._scribe_resumed_done = []
        self._scribe_resumed_done.append(job)
        call_later = getattr(self._wx, "CallLater", None)
        if len(self._scribe_resumed_done) == 1 and callable(call_later):
            call_later(3000, self._scribe_say_finished_while_closed)
        elif not callable(call_later):
            self._scribe_say_finished_while_closed()

    def _scribe_say_finished_while_closed(self) -> None:
        jobs, self._scribe_resumed_done = list(self._scribe_resumed_done), []
        if jobs:
            sentence = q.finished_while_closed_sentence(jobs, APP_NAME)
            self._scribe_last_message = sentence
            self._announce(sentence)

    def _scribe_open_default(self, job: Any) -> str:
        """Open the first result whose default action opens it; return its label."""
        for format_id, path in zip(job.done_formats, job.results, strict=False):
            if format_id in OPENS_IN_QUILL | OPENS_IN_BRAILLE | OPENS_IN_PLAYER:
                if self.scribe_open_result(Path(path), purpose=job.purpose):
                    return format_label(format_id).replace("Accessible HTML", "HTML")
        return ""

    def scribe_open_result(self, path: Path, *, purpose: str = "") -> bool:
        """Open a result in the right tool (PRD 9). True when something opened."""
        suffix = path.suffix.lower()
        if not path.exists():
            self._announce(f"{path.name} is no longer in {path.parent}.")
            return False
        if purpose == "import" and suffix in (".docx", ".html", ".htm"):
            return self._scribe_open_as_new_tab(path)
        if suffix in (".docx", ".html", ".htm", ".brf"):
            try:
                self.open_file(path)  # type: ignore[attr-defined]
                return True
            except Exception as exc:  # noqa: BLE001 - say it, keep going
                self._announce(f"{APP_NAME} could not open {path.name}: {exc}")
                return False
        from quill.ui.scribe_conversions_window import open_path

        return open_path(path)

    def _scribe_open_as_new_tab(self, path: Path) -> bool:
        """The Import tier's result: a new unsaved tab named "<stem> (Scribe)" (8.1.1)."""
        from quill.core.document import Document
        from quill.io.docconvert import convert_born_digital

        try:
            outcome = convert_born_digital(path)
        except Exception as exc:  # noqa: BLE001
            self._announce(f"{APP_NAME} could not read {path.name}: {exc}")
            return False
        index = self._create_document_tab(  # type: ignore[attr-defined]
            Document(text=outcome.text, path=None, modified=True), select=True
        )
        stem = path.stem.removesuffix(" (web page)")
        self._set_tab_page_text(index, f"{stem} (Scribe)")  # type: ignore[attr-defined]
        return True

    # ------------------------------------------------------------ the rest

    def scribe_human_review(self, document_id: str, name: str, *, parent: Any = None) -> None:
        """Ask for Human Review...: Scribe's paid remediation, with a comment."""
        wx = self._wx
        title = "Ask for Human Review"
        with wx.TextEntryDialog(
            parent or self.frame,
            f"What should Scribe's people fix in {name}? Human review is paid; Scribe "
            "will tell you the price.",
            title,
            style=wx.TextEntryDialogStyle | wx.TE_MULTILINE,
        ) as dialog:
            dialog.SetHelpText("Your comment goes to Scribe for Documents with the request.")
            if self._show_modal_dialog(dialog, title) != wx.ID_OK:
                return
            comment = str(dialog.GetValue()).strip()
        if not comment:
            return

        def _run(progress_callback: Any, cancellation_token: Any, **_kw: Any) -> None:
            from quill.core.scribe.client import ScribeClient

            ScribeClient().human_review(document_id, comment)

        self._task_manager.submit(
            name="scribe-human-review",
            func=_run,
            on_success=lambda _op, _r: self._announce(f"Sent {name} for human review."),
            on_failure=lambda _op, error: self._announce(plain(error)),
        )

    def scribe_about(self) -> None:
        """About Scribe for Documents: the policy text and Scribe's own links."""
        wx = self._wx
        from quill.ui.dialog_contract import apply_modal_ids
        from quill.ui.dpi import dip_size

        title = "About Scribe for Documents"
        dialog = wx.Dialog(self.frame, title=title)
        root = wx.BoxSizer(wx.VERTICAL)
        text = wx.TextCtrl(dialog, style=wx.TE_READONLY | wx.TE_MULTILINE, value=ABOUT_TEXT)
        text.SetName("About Scribe for Documents")
        text.SetHelpText("What Scribe for Documents is and how QUILL uses it.")
        text.SetMinSize(dip_size(dialog, 480, 180))
        root.Add(text, 1, wx.EXPAND | wx.ALL, 8)
        for label, key in (
            ("Open Scribe &Terms in Browser", "terms"),
            ("Open Scribe &Privacy Policy in Browser", "privacy"),
            ("Open the Scribe &Manual in Browser", "manual"),
        ):
            button = wx.Button(dialog, label=label)
            button.SetHelpText(f"Opens {LINKS[key]} in your browser.")
            button.Bind(wx.EVT_BUTTON, lambda _e, url=LINKS[key]: webbrowser.open(url))
            root.Add(button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        close = wx.Button(dialog, wx.ID_CANCEL, label="Close")
        close.SetHelpText("Close this window.")
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.AddStretchSpacer()
        row.Add(close, 0)
        root.Add(row, 0, wx.EXPAND | wx.ALL, 8)
        dialog.SetSizerAndFit(root)
        apply_modal_ids(dialog, escape_id=wx.ID_CANCEL)
        text.SetFocus()
        try:
            self._show_modal_dialog(dialog, title)
        finally:
            dialog.Destroy()


ABOUT_TEXT = (
    f"{POWERED_BY}, from Pneuma Solutions (scribeit.io).\n\n"
    "Scribe turns PDFs (password-protected ones too), pictures of text, Office and "
    "OpenDocument files, RTF, e-books and plain text into accessible tagged PDF, Word, "
    "EPUB, Kindle, HTML, DAISY with audio, MP3 and braille. It rebuilds headings, "
    "reading order and tables, and can describe pictures.\n\n"
    "You sign in on Scribe's own page in your browser; QUILL never sees your password. "
    "Every conversion uses your own Scribe account: the first full conversion of a "
    "document uses its pages, and further formats of the same document are free. "
    "QUILL never takes payment.\n\n"
    "Documents are processed by Pneuma Solutions in the United States and kept in your "
    "Scribe account under your retention setting. Pictures may be described by Scribe's "
    "AI. To delete a document: My Scribe Documents, Move to Trash, then Delete Forever."
)


__all__ = ["ABOUT_TEXT", "ScribeQueueMixin"]
