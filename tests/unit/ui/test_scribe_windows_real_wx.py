"""The Scribe windows built with real wx (independent review, 2026-11-21: "no test
builds any of the real wx windows"). Skipped where wxPython is not installed.

Pins what the fakes elsewhere cannot: the Convert dialog's "What will be sent"
field carries the AI-off reason and Tab reaches it (Q16); Scribe Conversions'
Tab order, and its details field written only when the text changes, keeping
the caret where the reader left it and never taking focus (Q15).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

wx = pytest.importorskip("wx")


class _Tasks:
    """A task manager that runs nothing (the lists never load in these tests)."""

    def submit(self, *_a, **_k):
        return None


def _host(parent):
    return SimpleNamespace(
        frame=parent,
        _task_manager=_Tasks(),
        _scribe_conversions_window=None,
        _announce=lambda _m: None,
        _set_status=lambda _m: None,
        scribe_sign_in=lambda: None,
        scribe_app_name=lambda: "QUILL",
        scribe_queue_wake=lambda: None,
    )


def _focusable(panel):
    return [
        child
        for child in panel.GetChildren()
        if child.AcceptsFocusFromKeyboard() and not isinstance(child, wx.StaticText)
    ]


def test_the_ai_off_reason_is_in_a_field_tab_reaches(wx_session_app, tmp_path) -> None:
    from quill.core.scribe.consent import AI_OFF_NOTE, SourceInfo
    from quill.core.scribe.model import DocumentSettings
    from quill.ui.scribe_convert_dialog import ScribeConvertDialog

    parent = wx.Frame(None)
    source = SourceInfo(path=Path(tmp_path / "Notes.pdf"), size=10, pages=2)
    dialog = ScribeConvertDialog(
        _host(parent), [source], formats=["docx"], settings=DocumentSettings(), ai_off=True
    )
    try:
        assert AI_OFF_NOTE in dialog.consent.GetValue()
        assert dialog.consent.AcceptsFocusFromKeyboard()
        assert dialog.groups.describe.GetValue() is False  # starts off, as PRD 10.1.4 says
        texts = [c.GetLabel() for c in dialog.panel.GetChildren() if isinstance(c, wx.StaticText)]
        assert AI_OFF_NOTE not in texts  # not only a note Tab never lands on
    finally:
        dialog.close()
        parent.Destroy()


def test_scribe_conversions_tab_order_and_a_steady_details_field(
    wx_session_app, quill_data_dir
) -> None:
    from quill.core.scribe import queue as q
    from quill.ui.scribe_conversions_window import ScribeConversionsWindow

    [job] = q.JobQueue().add([q.Job(title="Notes.pdf", formats=["docx"], message="Sending.")])
    parent = wx.Frame(None)
    window = ScribeConversionsWindow(_host(parent))
    window.timer.Stop()
    try:
        panel = window.list.GetParent()
        order = _focusable(panel)
        assert order[:2] == [window.list, window.details]
        assert window.commands_button in order
        assert window.details.GetValue().startswith("0 percent")
        writes = []
        real = window.details.ChangeValue
        window.details.ChangeValue = lambda text: (writes.append(text), real(text))
        for _ in range(3):
            window.refresh(force=True)  # the once-a-second refresh, nothing changed
        assert writes == []
        window.details.SetInsertionPoint(5)
        window.details.HasFocus = lambda: True  # a reader is in the field
        q.JobQueue().update(job.id, lambda j: setattr(j, "percent", 40))
        window.refresh(force=True)
        assert len(writes) == 1 and window.details.GetValue().startswith("40 percent")
        assert window.details.GetInsertionPoint() == 5
    finally:
        window.frame.Destroy()
        parent.Destroy()
