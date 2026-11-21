"""Behavioural tests for Convert with Scribe for Documents (UI mixin).

Driven headless with a fake host and a stub ``_wx`` (as
test_main_frame_docconvert.py does), a synchronous task manager, and the fake
``scribe_client`` from tests/scribe_fakes.py. Pins the contract a listener
relies on: consent is asked before every upload and declining uploads
nothing, every status update reaches the status bar (which speaks it), the
HTML result opens as a named, unsaved document, other formats are saved and
announced, a locked PDF asks for its password and resumes without uploading
again, and failures are said in a sentence.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from quill.core import scribe_documents as scribe
from quill.core.document import Document
from quill.ui import main_frame_scribe
from quill.ui.main_frame_scribe import (
    ScribeDocumentsMixin,
    html_bytes_to_text,
    scribe_wildcard,
    stem_for_url,
    unique_path,
)
from tests.scribe_fakes import FakeChannel, FakeModule, complete, memory_secrets, status


class _Dialog:
    """A stock dialog stand-in that answers from the host's script."""

    def __init__(self, host, kind, *args, **kwargs) -> None:
        self.host = host
        self.kind = kind
        self.args = args
        self.selection = 0
        self.help = ""
        host.dialogs.append(kind)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def SetSelection(self, index):  # noqa: N802 - wx API
        self.selection = index

    def GetSelection(self):  # noqa: N802
        return self.host.choices.pop(0)

    def GetPath(self):  # noqa: N802
        return str(self.host.paths.pop(0))

    def GetValue(self):  # noqa: N802
        return self.host.texts.pop(0)

    def SetHelpText(self, text):  # noqa: N802
        self.help = text

    def Destroy(self):  # noqa: N802
        pass


class _FakeWx(SimpleNamespace):
    ID_OK = 1
    ID_CANCEL = 2
    YES = 5103
    NO = 5104
    OK = 4
    YES_NO = 10
    ICON_ERROR = 512
    ICON_INFORMATION = 2048
    ICON_WARNING = 256
    ICON_QUESTION = 1024
    FD_OPEN = 1
    FD_FILE_MUST_EXIST = 16
    DD_DEFAULT_STYLE = 0


class _SyncTaskManager:
    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, name, func, *, on_success=None, on_failure=None, on_progress=None, **_kw):
        self.submitted.append(name)
        token = SimpleNamespace(is_cancelled=lambda: False)

        def _progress(payload):
            if on_progress is not None:
                on_progress("op", payload)

        try:
            result = func(progress_callback=_progress, cancellation_token=token)
        except BaseException as exc:  # noqa: BLE001 - mirrors QuillTaskManager
            if on_failure is not None:
                on_failure("op", exc)
            return
        if on_success is not None:
            on_success("op", result)


class _Host(ScribeDocumentsMixin):
    def __init__(self) -> None:
        wx = _FakeWx()
        for kind in (
            "SingleChoiceDialog",
            "FileDialog",
            "DirDialog",
            "TextEntryDialog",
            "PasswordEntryDialog",
        ):
            setattr(wx, kind, lambda *a, _k=kind, **kw: _Dialog(self, _k, *a, **kw))
        self._wx = wx
        self.frame = object()
        self.document = Document(text="", path=None)
        self._task_manager = _SyncTaskManager()
        self.modal_answers: list[int] = []
        self.box_answers: list[int] = []
        self.choices: list[int] = []
        self.paths: list[Path] = []
        self.texts: list[str] = []
        self.dialogs: list[str] = []
        self.boxes: list[tuple[str, str]] = []
        self.statuses: list[str] = []
        self.announcements: list[str] = []
        self.docs: list[Document] = []
        self.tab_names: list[str] = []

    def _show_modal_dialog(self, _dialog, _title):
        return self.modal_answers.pop(0) if self.modal_answers else self._wx.ID_OK

    def _show_message_box(self, message, title, _style):
        self.boxes.append((title, message))
        return self.box_answers.pop(0) if self.box_answers else self._wx.YES

    def _create_document_tab(self, document, select=True):
        self.docs.append(document)
        return len(self.docs) - 1

    def _set_tab_page_text(self, _index, title):
        self.tab_names.append(title)

    def _set_status(self, message):
        self.statuses.append(str(message))

    def _announce(self, message):
        self.announcements.append(message)


@pytest.fixture
def fake(monkeypatch):
    module = FakeModule()
    manager = memory_secrets()
    scribe.save_tokens(scribe.StoredTokens("access", "refresh", 2_000_000_000), manager)
    monkeypatch.setenv(scribe.SCRIBE_CLIENT_ID_ENV, "test-client")
    monkeypatch.delenv("QUILL_SAFE_MODE", raising=False)
    monkeypatch.setattr(scribe, "client_module", lambda: module)
    real_signed_in = scribe.signed_in
    real_sign_out = scribe.sign_out
    real_run = scribe.run_conversion
    monkeypatch.setattr(scribe, "signed_in", lambda manager_=None: real_signed_in(manager))
    monkeypatch.setattr(scribe, "sign_out", lambda manager_=None: real_sign_out(manager))
    monkeypatch.setattr(
        scribe,
        "run_conversion",
        lambda job, **kw: real_run(job, module=module, manager=manager, sleep=lambda s: None, **kw),
    )
    module.manager = manager
    return module


@pytest.fixture
def pdf(tmp_path) -> Path:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF fake")
    return path


def test_html_result_opens_as_a_named_unsaved_document(fake, pdf) -> None:
    host = _Host()
    fake.channels = [FakeChannel([status("html", "convert", 0.5), complete("html")])]
    host.choices = [0, 0]  # a file; accessible HTML
    host.paths = [pdf]
    host.convert_with_scribe()
    assert host.boxes[0][0] == "Upload Consent"
    assert "Powered by Scribe for Documents" in host.boxes[0][1]
    assert "page credits" in host.boxes[0][1]
    assert "Scribe for Documents: HTML converting, 50 percent." in host.statuses
    assert len(host.docs) == 1
    assert "Converted" in host.docs[0].text
    assert host.docs[0].modified is True
    assert host.tab_names == ["report (converted)"]
    assert host.announcements[-1].startswith("Scribe for Documents converted report.pdf")
    assert host._scribe_busy is False


def test_declining_consent_uploads_nothing(fake, pdf) -> None:
    host = _Host()
    host.choices = [0, 0]
    host.paths = [pdf]
    host.box_answers = [host._wx.NO]
    host.convert_with_scribe()
    assert fake.calls == []
    assert host._task_manager.submitted == []
    assert host.statuses[-1] == "Scribe conversion cancelled; nothing was uploaded"


def test_a_saved_format_goes_to_the_chosen_folder(fake, pdf, tmp_path) -> None:
    host = _Host()
    out = tmp_path / "out"
    out.mkdir()
    (out / "report.brf").write_bytes(b"older")
    fake.download_bytes = b"BRAILLE"
    fake.channels = [FakeChannel([complete("brf")])]
    labels = [f.id for f in scribe.OUTPUT_FORMATS]
    host.choices = [0, labels.index("brf")]
    host.paths = [pdf, out]
    host.convert_with_scribe()
    saved = out / "report (2).brf"
    assert saved.read_bytes() == b"BRAILLE"
    assert host.announcements[-1] == f"Scribe for Documents saved report (2).brf in {out}."


def test_a_locked_pdf_asks_for_the_password_and_resumes(fake, pdf) -> None:
    host = _Host()
    fake.channels = [
        FakeChannel([], starts=[fake.PasswordRequiredError("locked")]),
        FakeChannel([complete("html")]),
    ]
    host.choices = [0, 0]
    host.paths = [pdf]
    host.texts = ["hunter2"]
    host.convert_with_scribe()
    assert "PasswordEntryDialog" in host.dialogs
    assert [c for c in fake.calls if c[0] == "upload"] == [("upload", "report.pdf", 9, None)]
    assert len(host.docs) == 1


def test_cancelling_the_password_stops_and_says_so(fake, pdf) -> None:
    host = _Host()
    fake.channels = [FakeChannel([], starts=[fake.PasswordRequiredError("locked")])]
    host.choices = [0, 0]
    host.paths = [pdf]
    # Source list, format list, file box answer OK; the password box cancels.
    host.modal_answers = [1, 1, 1, 2]
    host.convert_with_scribe()
    assert host.statuses[-1] == "report.pdf stays locked; the conversion stopped."
    assert host.docs == []


def test_a_url_needs_no_file(fake) -> None:
    host = _Host()
    fake.channels = [FakeChannel([complete("html")])]
    host.choices = [2, 0]
    host.texts = ["https://example.org/guide.pdf"]
    host.convert_with_scribe()
    assert ("url", "https://example.org/guide.pdf", None) in fake.calls
    assert "will fetch https://example.org/guide.pdf" in host.boxes[0][1]
    assert host.tab_names == ["guide (converted)"]


def test_the_current_unsaved_document_is_sent_as_a_word_copy(fake) -> None:
    host = _Host()
    host.document = Document(text="# Notes\n\nHello.\n", path=None)
    fake.channels = [FakeChannel([complete("html")])]
    host.choices = [1, 0]
    host.convert_with_scribe()
    upload = next(c for c in fake.calls if c[0] == "upload")
    assert upload[1] in ("Untitled.docx", "Untitled.txt")
    assert host.tab_names == ["Untitled (converted)"]


def test_an_empty_current_document_is_refused(fake) -> None:
    host = _Host()
    host.choices = [1]
    host.convert_with_scribe()
    assert fake.calls == []
    assert "empty" in host.statuses[0]


def test_not_signed_in_offers_sign_in(fake, monkeypatch) -> None:
    scribe.sign_out(fake.manager)
    host = _Host()
    host.box_answers = [host._wx.NO]
    host.convert_with_scribe()
    assert "not signed in" in host.boxes[0][1]
    assert host._task_manager.submitted == []


def test_an_unregistered_build_says_so_and_does_nothing(fake, monkeypatch) -> None:
    monkeypatch.delenv(scribe.SCRIBE_CLIENT_ID_ENV, raising=False)
    host = _Host()
    host.convert_with_scribe()
    assert "not registered" in host.boxes[0][1]
    assert host.dialogs == []


def test_safe_mode_refuses(fake, monkeypatch) -> None:
    monkeypatch.setenv("QUILL_SAFE_MODE", "1")
    host = _Host()
    host.convert_with_scribe()
    assert host.statuses == ["Scribe for Documents is off in Safe Mode."]


def test_no_credits_is_shown_as_an_error(fake, pdf) -> None:
    host = _Host()
    fake.channels = [FakeChannel([], starts=[fake.NeedsPurchaseError("No pages left")])]
    host.choices = [0, 0]
    host.paths = [pdf]
    host.convert_with_scribe()
    title, message = host.boxes[-1]
    assert title == "Convert with Scribe for Documents"
    assert "page credits" in message
    assert host._scribe_busy is False


def test_a_second_conversion_waits_for_the_first(fake) -> None:
    host = _Host()
    host._scribe_busy = True
    host.convert_with_scribe()
    assert "one at a time" in host.statuses[0]
    assert host.dialogs == []


def test_sign_in_runs_on_the_task_pool_and_announces(fake, monkeypatch) -> None:
    import quill.core.scribe_signin as signin

    monkeypatch.setattr(signin, "sign_in", lambda **kw: scribe.StoredTokens("a", "r"))
    host = _Host()
    host.scribe_sign_in()
    assert host._task_manager.submitted == ["scribe-sign-in"]
    assert host.announcements == ["Signed in to Scribe for Documents."]


def test_sign_out_forgets_and_says_so(fake) -> None:
    host = _Host()
    host.scribe_sign_out()
    assert host.announcements == ["Signed out of Scribe for Documents."]
    host.scribe_sign_out()
    assert host.statuses == ["You were not signed in to Scribe for Documents."]


def test_helpers() -> None:
    assert "*.pdf" in scribe_wildcard()
    assert "*.djvu" in scribe_wildcard()
    assert stem_for_url("https://example.org/docs/Annual%20Report.pdf") == "Annual_20Report"
    assert stem_for_url("https://example.org/") == "example.org"
    assert unique_path(Path("/nonexistent"), "a", ".pdf") == Path("/nonexistent/a.pdf")


def test_a_zipped_html_result_reads_its_page() -> None:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("book/images/a.png", b"png")
        archive.writestr("book/index.html", "<html><body><h1>Zipped</h1></body></html>")
    text, _notes = html_bytes_to_text(buffer.getvalue(), "book")
    assert "Zipped" in text


def test_the_module_never_shows_a_modal_directly() -> None:
    source = Path(main_frame_scribe.__file__).read_text(encoding="utf-8")
    assert ".ShowModal(" not in source
