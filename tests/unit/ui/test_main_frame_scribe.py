"""Scribe for Documents in QUILL (UI mixins), headless with fakes.

A fake host carrying ScribeDocumentsMixin and ScribeQueueMixin, a stub
``_wx``, a synchronous task manager, and the Convert and sign-in dialogs
replaced by recorders. Pins what a listener relies on: the menu is exactly
the PRD's (8.1.8) and every command names Scribe for Documents; Safe Mode
turns everything off; nothing is queued without Send and Convert; the first
command signs in and then carries on; who speaks and when (7.4a); results
open only in the app that started them and only when it is in front; the
Import tier's result is a new tab named "(Scribe)".
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from quill.core.document import Document
from quill.core.scribe import queue as q
from quill.core.scribe.model import DocumentSettings
from quill.ui import main_frame_scribe, main_frame_scribe_queue, scribe_convert_dialog
from quill.ui.main_frame_scribe import TOOLS_ROWS, ScribeDocumentsMixin, palette_title
from quill.ui.main_frame_scribe_queue import ScribeQueueMixin
from quill.ui.scribe_convert_dialog import ConvertRequest
from tests.scribe_fake_server import memory_store


class _FakeWx(SimpleNamespace):
    ID_OK = 1
    ID_CANCEL = 2
    YES = 5103
    NO = 5104
    OK = 4
    YES_NO = 10
    ICON_INFORMATION = 2048
    ICON_QUESTION = 1024

    @staticmethod
    def CallAfter(func, *args):  # noqa: N802
        func(*args)

    def CallLater(self, _ms, func, *args):  # noqa: N802
        self.later.append((func, args))


class _Tasks:
    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, name, func, *, on_success=None, on_failure=None, **_kw):
        self.submitted.append(name)
        try:
            result = func(progress_callback=lambda _p: None, cancellation_token=None)
        except BaseException as exc:  # noqa: BLE001
            if on_failure:
                on_failure("op", exc)
            return None
        if on_success:
            on_success("op", result)
        return None


class _Runner:
    def __init__(self) -> None:
        self.woken = 0
        self.passwords: dict[str, str] = {}

    def wake(self) -> None:
        self.woken += 1

    def resume_after_sign_in(self) -> int:
        self.resumed = getattr(self, "resumed", 0) + 1
        return 0

    def give_password(self, job_id: str, password: str) -> None:
        self.passwords[job_id] = password


class _Host(ScribeDocumentsMixin, ScribeQueueMixin):
    def __init__(self) -> None:
        self._wx = _FakeWx(later=[])
        self.frame = SimpleNamespace(IsActive=lambda: self.front)
        self.front = True
        self.document = Document(text="", path=None)
        self.settings = SimpleNamespace(
            scribe_default_formats="docx",
            scribe_download_folder="",
            scribe_open_results="yes",
            scribe_offer_low_text_pdf=True,
            scribe_speak_stages="front",
            scribe_batch_exception=False,
            scribe_last_formats="",
            scribe_local_settings={},
            scribe_privacy_told=False,
        )
        self._task_manager = _Tasks()
        self._scribe_runner = _Runner()
        self.statuses: list[str] = []
        self.announcements: list[str] = []
        self.boxes: list[str] = []
        self.opened: list[Path] = []
        self.tabs: list[tuple[Document, str]] = []
        self.registered: list[tuple[str, str]] = []
        self.commands = SimpleNamespace(
            register=lambda cid, title, handler, binding: self.registered.append((cid, title))
        )

    def _binding_for(self, _command_id):
        return None

    def _set_status(self, message):
        self.statuses.append(message)

    def _announce(self, message):
        self.announcements.append(message)

    def _show_message_box(self, message, _title, _style, _parent=None):
        self.boxes.append(message)
        return self._wx.YES

    def _show_modal_dialog(self, dialog, _title):
        return dialog.answer

    def open_file(self, path):
        self.opened.append(path)

    def _create_document_tab(self, document, select=True):
        self.tabs.append((document, ""))
        return len(self.tabs) - 1

    def _set_tab_page_text(self, index, name):
        self.tabs[index] = (self.tabs[index][0], name)


class _ConvertDialog:
    """Stands in for the Convert dialog; answers from ``answer`` and ``request``."""

    last: _ConvertDialog | None = None
    answer = 1
    request: ConvertRequest | None = None

    def __init__(self, host, sources, **kwargs) -> None:
        self.sources = sources
        self.kwargs = kwargs
        self.dialog = SimpleNamespace(answer=type(self).answer)
        self.request = type(self).request
        type(self).last = self

    def show(self):
        return self.request if self.dialog.answer == 1 else None

    def close(self) -> None:
        pass


@pytest.fixture
def scribe(monkeypatch, quill_data_dir):
    monkeypatch.delenv("QUILL_SAFE_MODE", raising=False)
    store = memory_store()
    monkeypatch.setattr("quill.core.scribe.auth.TokenStore", lambda *a, **k: store)
    monkeypatch.setattr(scribe_convert_dialog, "ScribeConvertDialog", _ConvertDialog)
    monkeypatch.setattr("quill.core.settings.save_settings", lambda _s: None)
    _ConvertDialog.answer = 1
    _ConvertDialog.request = ConvertRequest(["brf", "docx"], DocumentSettings(language="en"))
    return store


@pytest.fixture
def pdf(tmp_path) -> Path:
    path = tmp_path / "Notes.pdf"
    path.write_bytes(b"%PDF-1.4\n/Type /Page\n/Type /Page\n")
    return path


# ------------------------------------------------------------ menus and names


def test_the_tools_menu_is_the_prds_list_in_its_order() -> None:
    labels = [label.replace("&", "") for _cid, label, _h in TOOLS_ROWS]
    assert labels == [
        "Convert a File...",
        "Convert This Document...",
        "Convert from a Link...",
        "My Scribe Documents",
        "Conversions",
        "Sign In...",
        "Sign Out",
        "Open My Scribe Account in Browser",
        "Options...",
        "About Scribe for Documents",
    ]


def test_every_command_names_scribe_for_documents(scribe) -> None:
    host = _Host()
    host._register_scribe_commands()
    assert len(host.registered) == 15
    assert all("Scribe" in title for _cid, title in host.registered)
    assert palette_title("x", "Convert a &File...") == "Convert a File (Scribe for Documents)..."


def test_safe_mode_turns_every_command_off(scribe, monkeypatch, pdf) -> None:
    monkeypatch.setenv("QUILL_SAFE_MODE", "1")
    host = _Host()
    host.scribe_convert_file()
    host.scribe_convert_document()
    assert host.statuses[0] == "Scribe for Documents is off in Safe Mode."
    assert _ConvertDialog.last is None or _ConvertDialog.last.sources != []
    assert q.JobQueue().jobs() == []


# ------------------------------------------------------------ converting


def _convert(host, *sources, **kw):
    _ConvertDialog.last = None
    host._scribe_convert(list(sources), **kw)


def test_send_and_convert_queues_one_job_with_its_formats_and_settings(scribe, pdf) -> None:
    from quill.core.scribe.consent import describe_source

    host = _Host()
    _convert(host, describe_source(pdf))
    [job] = q.JobQueue().jobs()
    assert job.formats == ["docx", "brf"]  # Scribe's build order
    assert job.title == "Notes.pdf" and job.source_path == str(pdf)
    assert job.settings["language"] == "en"
    assert job.folder.endswith(os.path.join("Scribe for Documents", "Notes"))
    assert host._scribe_runner.woken == 1
    assert host.settings.scribe_last_formats == "brf,docx"
    assert host.settings.scribe_privacy_told is True
    assert _ConvertDialog.last.kwargs["first_upload"] is True


def test_cancel_sends_nothing(scribe, pdf) -> None:
    from quill.core.scribe.consent import describe_source

    _ConvertDialog.answer = 2
    host = _Host()
    _convert(host, describe_source(pdf))
    assert q.JobQueue().jobs() == []
    assert host.statuses[-1] == "Nothing was sent to Scribe for Documents."


def test_a_pdf_password_goes_to_the_runner_never_the_queue(scribe, pdf) -> None:
    from quill.core.scribe.consent import describe_source

    _ConvertDialog.request = ConvertRequest(
        ["docx"], DocumentSettings(), passwords={"Notes.pdf": "pw"}
    )
    host = _Host()
    _convert(host, describe_source(pdf))
    [job] = q.JobQueue().jobs()
    assert host._scribe_runner.passwords == {job.id: "pw"}
    assert "pw" not in q.JobQueue().path.read_text(encoding="utf-8")


def test_several_files_wait_for_send_next_file(scribe, tmp_path) -> None:
    from quill.core.scribe.consent import describe_source

    files = []
    for name in ("a.pdf", "b.pdf"):
        (tmp_path / name).write_bytes(b"%PDF")
        files.append(describe_source(tmp_path / name))
    host = _Host()
    _convert(host, *files)
    states = [j.state for j in q.JobQueue().jobs()]
    assert states == [q.WAITING, q.WAITING_YOU]
    assert "Send Next File" in host.statuses[-1]


def test_the_first_command_signs_in_then_carries_on(scribe, monkeypatch, pdf) -> None:
    from quill.core.scribe.consent import describe_source

    scribe.wipe()
    signed: list[bool] = []

    def fake_sign_in(host, **_kw):
        signed.append(True)
        scribe.save(
            __import__("quill.core.scribe.auth", fromlist=["x"]).StoredTokens("a", "r", 4e9)
        )
        return True

    monkeypatch.setattr("quill.ui.scribe_signin_dialog.sign_in", fake_sign_in)
    host = _Host()
    _convert(host, describe_source(pdf))
    assert signed == [True]
    assert len(q.JobQueue().jobs()) == 1  # Continue carried on into the command
    assert host._scribe_runner.resumed == 1  # jobs paused for sign-in carry on (Q4)


def test_this_document_unsaved_goes_as_a_word_copy(scribe) -> None:
    host = _Host()
    host.document = Document(text="# Notes\n\nHello.\n", path=None)
    source = host._scribe_this_document()
    assert source.path.suffix in (".docx", ".txt") and source.path.stem == "Untitled"
    host.document = Document(text="", path=None)
    assert host._scribe_this_document() is None
    assert "empty" in host.statuses[-1]


def test_export_and_braille_start_with_their_formats(scribe, monkeypatch) -> None:
    host = _Host()
    host.document = Document(text="Hello.", path=None)
    host.scribe_export()
    assert _ConvertDialog.last.kwargs["formats"] == ["pdf"]
    host.scribe_braille()
    assert _ConvertDialog.last.kwargs["formats"] == ["brf"]
    assert q.JobQueue().jobs()[-1].purpose == "braille"


# ------------------------------------------------------------ who speaks


def _job(**kw):
    return q.Job(title="Notes.pdf", **kw)


def test_stages_follow_speak_each_stage(scribe) -> None:
    host = _Host()
    host._scribe_say("stage", "Notes.pdf, converting.", _job())
    host.front = False
    host._scribe_say("stage", "Notes.pdf, adding image descriptions.", _job())
    host.settings.scribe_speak_stages = "never"
    host.front = True
    host._scribe_say("percent", "Notes.pdf, 50 percent.", _job())
    host._scribe_say("error", "Scribe for Documents could not convert Notes.pdf: no.", _job())
    assert host.announcements == [
        "Notes.pdf, converting.",
        "Scribe for Documents could not convert Notes.pdf: no.",  # errors always speak
    ]
    host.scribe_repeat_last()
    assert host.announcements[-1] == "Scribe for Documents could not convert Notes.pdf: no."


def test_a_result_opens_only_here_and_in_front(scribe, tmp_path) -> None:
    docx = tmp_path / "Notes.docx"
    docx.write_text("x")
    host = _Host()
    job = _job(app="QUILL", results=[str(docx)], done_formats=["docx"], state=q.DONE)
    host._scribe_job_done(job)
    assert host.opened == [docx]
    assert host.announcements[-1].endswith("Word is open in QUILL.")
    host.front = False
    host._scribe_job_done(job)
    assert host.announcements[-1].endswith("Open it from Scribe Conversions.")
    assert host.opened == [docx]


def test_an_import_result_is_a_new_tab_named_scribe(scribe, tmp_path, monkeypatch) -> None:
    page = tmp_path / "Notes.html"
    page.write_text("<h1>Notes</h1><p>Converted.</p>", encoding="utf-8")
    monkeypatch.setattr(
        "quill.io.docconvert.convert_born_digital",
        lambda path: SimpleNamespace(text="# Notes\n\nConverted.\n", warnings=()),
    )
    host = _Host()
    assert host.scribe_open_result(page, purpose="import") is True
    document, name = host.tabs[0]
    assert name == "Notes (Scribe)" and document.modified is True


class _RichEdit:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.rtf = b""

    def set_rtf(self, data: bytes) -> None:
        if self.fail:
            raise OSError("no TOM")
        self.rtf = data

    def get_plain_text(self) -> str:
        return "Notes\nConverted."


def _rich_host(monkeypatch, *, capable: bool = True, fail: bool = False) -> _Host:
    host = _Host()
    wrapper = _RichEdit(fail=fail)
    tab = SimpleNamespace(editor_mode="markup", docx_rich=False)
    host._rich_capable = lambda: capable
    host._active_richedit = lambda: wrapper
    host._active_tab = lambda: tab
    host._refresh_statusbar = lambda: None
    host._close_tab = lambda index: host.tabs.pop(index)
    host.wrapper, host.tab = wrapper, tab
    monkeypatch.setattr("quill.core.docx_open.word_rich_available", lambda: True)
    monkeypatch.setattr("quill.core.docx_open.read_docx_as_rtf", lambda path: "{\\rtf1 Notes}")
    monkeypatch.setattr(
        "quill.io.docconvert.convert_born_digital",
        lambda path: SimpleNamespace(text="# Notes\n\nConverted.\n", warnings=()),
    )
    return host


def test_an_import_word_result_opens_as_rich_text(scribe, tmp_path, monkeypatch) -> None:
    """PRD 8.1.1: "Word by default, opened as rich text", in a new unsaved tab."""
    docx = tmp_path / "Notes.docx"
    docx.write_bytes(b"PK")
    host = _rich_host(monkeypatch)
    assert host.scribe_open_result(docx, purpose="import") is True
    [(document, name)] = host.tabs
    assert name == "Notes (Scribe)" and document.path is None and document.modified
    assert host.tab.editor_mode == "rich" and host.tab.docx_rich is True
    assert host.wrapper.rtf == b"{\\rtf1 Notes}"


def test_an_import_word_result_falls_back_to_text_without_rich(
    scribe, tmp_path, monkeypatch
) -> None:
    docx = tmp_path / "Notes.docx"
    docx.write_bytes(b"PK")
    for host in (_rich_host(monkeypatch, capable=False), _rich_host(monkeypatch, fail=True)):
        assert host.scribe_open_result(docx, purpose="import") is True
        [(document, name)] = host.tabs
        assert document.text.startswith("# Notes") and name == "Notes (Scribe)"
        assert host.tab.editor_mode == "markup"


def test_mp3_and_daisy_go_to_media_player(scribe, tmp_path, monkeypatch) -> None:
    """PRD 8.3 and 9: MP3 and DAISY open in Quill Media Player, DAISY at its .ncx."""
    import zipfile

    launched: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr("quill.core.app_launcher.is_app_released", lambda key: True)
    monkeypatch.setattr(
        "quill.core.app_launcher.launch_app",
        lambda key, extra_args=(): launched.append((key, extra_args)) or True,
    )
    mp3 = tmp_path / "Notes.mp3"
    mp3.write_bytes(b"ID3")
    daisy = tmp_path / "Notes.zip"
    with zipfile.ZipFile(daisy, "w") as bundle:
        bundle.writestr("Notes/dtb.ncx", "<ncx/>")
    host = _Host()
    assert host.scribe_open_result(mp3) is True
    assert host.scribe_open_result(daisy) is True
    ncx = tmp_path / "Notes (DAISY)" / "Notes" / "dtb.ncx"
    assert launched == [("player", (str(mp3),)), ("player", (str(ncx),))]


def test_without_media_player_audio_opens_with_the_computers_player(
    scribe, tmp_path, monkeypatch
) -> None:
    opened: list[Path] = []
    monkeypatch.setattr("quill.core.app_launcher.is_app_released", lambda key: False)
    monkeypatch.setattr(
        "quill.ui.scribe_conversions_window.open_path", lambda path: opened.append(path) or True
    )
    mp3 = tmp_path / "Notes.mp3"
    mp3.write_bytes(b"ID3")
    assert _Host().scribe_open_result(mp3) is True
    assert opened == [mp3]


def test_jobs_finished_after_their_app_closed_are_said_together(scribe, tmp_path) -> None:
    host = _Host()
    folder = tmp_path / "Scribe for Documents"
    for title in ("Notes.pdf", "Chapter 2.docx"):
        host._scribe_job_done(
            q.Job(title=title, resumed=True, owner_pid=1, results=[str(folder / title / "x.docx")])
        )
    assert host.announcements == []  # said once, after a moment, for all of them
    for func, args in host._wx.later:
        func(*args)
    assert host.announcements == [
        "Scribe for Documents finished 2 documents while QUILL was closed: "
        f"Notes and Chapter 2, in {q.short_folder(str(folder))}."
    ]


def test_scribe_status_says_the_current_job(scribe) -> None:
    host = _Host()
    host.scribe_status()
    assert host.announcements[-1] == "No Scribe for Documents conversions are waiting."
    q.JobQueue().add([
        q.Job(
            title="Notes.pdf",
            state=q.CONVERTING,
            stage="add_image_descriptions",
            percent=40,
            eta_s=120,
            formats=["docx"],
        )
    ])
    host.scribe_status()
    assert (
        host.announcements[-1]
        == "Notes.pdf: adding image descriptions, 40 percent, about 2 minutes."
    )


def test_the_modules_never_show_a_modal_directly() -> None:
    for module in (main_frame_scribe, main_frame_scribe_queue, scribe_convert_dialog):
        assert ".ShowModal(" not in Path(module.__file__).read_text(encoding="utf-8")
