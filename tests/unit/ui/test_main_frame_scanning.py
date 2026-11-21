"""File > Scan Pages and Choose Scanner (UI mixin), headless with fakes.

A stub ``_wx``, a synchronous task manager and the fake scanner from
tests/scanning_fakes.py. Pins what a listener relies on: one scanner is used
without asking, several are asked about once and remembered, "Scan another
page?" after each page, every page handed over in one go, a jam keeps the
pages already scanned, and a missing tool or no scanner is a sentence.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from quill.core import scanning
from quill.core.document import Document
from quill.ui import main_frame_scanning
from quill.ui.main_frame_scanning import ScanningMixin
from tests.scanning_fakes import FakeBackend


class _Dialog:
    def __init__(self, host, kind, *args, **kwargs) -> None:
        self.host = host
        self.kind = kind
        self.args = args
        self.kwargs = kwargs
        host.dialogs.append((kind, args[2] if len(args) > 2 else kwargs.get("message", "")))
        host.questions.append((kind, args[1] if len(args) > 1 else ""))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def SetSelection(self, index):  # noqa: N802 - wx API
        self.host.preselected.append(index)

    def GetSelection(self):  # noqa: N802
        return self.host.choices.pop(0)

    def GetPath(self):  # noqa: N802
        return str(self.host.paths.pop(0))

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
    ICON_QUESTION = 1024
    FD_SAVE = 2
    FD_OVERWRITE_PROMPT = 4
    DD_DEFAULT_STYLE = 0

    @staticmethod
    def CallAfter(func, *a):  # noqa: N802
        func(*a)


class _Tasks:
    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, name, func, *, on_success=None, on_failure=None, **_kw):
        self.submitted.append(name)
        token = SimpleNamespace(is_cancelled=lambda: False)
        try:
            result = func(progress_callback=lambda _p: None, cancellation_token=token)
        except BaseException as exc:  # noqa: BLE001
            on_failure("op", exc)
            return
        on_success("op", result)


class _Host(ScanningMixin):
    def __init__(self, backend: FakeBackend | None = None) -> None:
        wx = _FakeWx()
        for kind in ("SingleChoiceDialog", "FileDialog", "DirDialog"):
            setattr(wx, kind, lambda *a, _k=kind, **kw: _Dialog(self, _k, *a, **kw))
        self._wx = wx
        self.frame = object()
        self.document = Document(text="", path=None)
        self.settings = SimpleNamespace(
            scanner_device="",
            scanner_device_name="",
            scanner_dpi="300",
            scanner_color="gray",
            scanner_tool_path="",
        )
        self.backend = backend or FakeBackend()
        self._task_manager = _Tasks()
        self.box_answers: list[int] = []
        self.modal_answers: list[int] = []
        self.choices: list[int] = []
        self.preselected: list[int] = []
        self.paths: list[Path] = []
        self.dialogs: list[tuple[str, str]] = []
        self.questions: list[tuple[str, str]] = []
        self.boxes: list[str] = []
        self.statuses: list[str] = []
        self.announcements: list[str] = []
        self.ocr: list[Path] = []
        self.inserted: list[str] = []

    def _scan_backend(self):
        return self.backend

    def _show_modal_dialog(self, _dialog, _title):
        return self.modal_answers.pop(0) if self.modal_answers else self._wx.ID_OK

    def _show_message_box(self, message, _title, _style, _parent=None):
        self.boxes.append(message)
        return self.box_answers.pop(0) if self.box_answers else self._wx.NO

    def _set_status(self, message):
        self.statuses.append(str(message))

    def _announce(self, message):
        self.announcements.append(message)

    def _docconvert_run_local_ocr(self, source):
        self.ocr.append(source)

    def insert_image(self, initial_path=""):
        self.inserted.append(initial_path)


@pytest.fixture(autouse=True)
def _no_settings_write(monkeypatch):
    monkeypatch.setattr("quill.core.settings.save_settings", lambda _s: None)


def test_one_scanner_scans_without_asking_and_hands_over_every_page() -> None:
    host = _Host()
    host.box_answers = [host._wx.YES, host._wx.NO]  # scan another? yes, then no
    got = []
    host.start_scan(got.append)
    session = got[0]
    assert session.page_count == 2
    assert host._task_manager.submitted == ["scan-list", "scan-page", "scan-page"]
    assert host.boxes[0].startswith("Page 1 scanned.") and "Scan another page?" in host.boxes[0]
    assert host.announcements[:3] == [
        "Looking for scanners...",
        "Scanning page 1. This can take up to a minute.",
        "Scanning page 2. This can take up to a minute.",
    ]
    assert not [d for d in host.dialogs if d[0] == "SingleChoiceDialog"]
    session.cleanup()


def test_several_scanners_ask_once_and_remember() -> None:
    backend = FakeBackend([scanning.ScannerInfo("a", "Canon"), scanning.ScannerInfo("b", "HP")])
    host = _Host(backend)
    host.choices = [1]
    got = []
    host.start_scan(got.append)
    assert backend.scans[0][0] == "b"
    assert (host.settings.scanner_device, host.settings.scanner_device_name) == ("b", "HP")
    got.clear()
    host.start_scan(got.append)  # remembered: no question this time
    assert len([d for d in host.dialogs if d[0] == "SingleChoiceDialog"]) == 1
    assert backend.scans[1][0] == "b"


def test_options_clearing_the_name_asks_again() -> None:
    backend = FakeBackend([scanning.ScannerInfo("a", "Canon"), scanning.ScannerInfo("b", "HP")])
    host = _Host(backend)
    host.settings.scanner_device = "b"
    host.choices = [0]
    host.start_scan(lambda s: None)
    assert backend.scans[0][0] == "a"


def test_no_scanner_and_a_missing_tool_are_sentences() -> None:
    host = _Host(FakeBackend([]))
    host.start_scan(lambda s: pytest.fail("nothing to hand over"))
    assert host.boxes == [scanning.NO_SCANNER_SENTENCE]
    host = _Host(FakeBackend(fail_with=scanning.ScannerToolMissing("The scanner tool is missing.")))
    host.start_scan(lambda s: pytest.fail("nothing to hand over"))
    assert host.boxes == ["The scanner tool is missing."]  # no error code read aloud


def test_a_jam_after_a_page_keeps_what_was_scanned() -> None:
    # Second page jams: swap in a jamming backend for the session's next call.
    host = _Host()
    host.box_answers = [host._wx.YES]
    calls = {"n": 0}
    real = host.backend.scan

    def scan_then_jam(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise scanning.ScanError("Paper jam.")
        return real(*a, **kw)

    host.backend.scan = scan_then_jam  # type: ignore[method-assign]
    got = []
    host.start_scan(got.append)
    assert got[0].page_count == 1 and "Paper jam." in host.boxes


def test_scan_pages_offers_ocr_and_reads_the_text() -> None:
    host = _Host()
    host.choices = [0]  # Read the text (OCR)
    host.scan_pages()
    assert len(host.ocr) == 1 and host.ocr[0].suffix == ".png"


def test_scan_pages_saves_a_pdf(tmp_path) -> None:
    pytest.importorskip("PIL")
    host = _Host()
    labels = [label for key, label in main_frame_scanning.SCAN_USES if key != "insert"]
    host.document = Document(text="x", path=None)
    host._document_is_read_only = lambda: True  # type: ignore[attr-defined]
    host.choices = [labels.index("Save as a PDF")]
    host.paths = [tmp_path / "letter.pdf"]
    host.scan_pages()
    assert (tmp_path / "letter.pdf").read_bytes().startswith(b"%PDF")
    assert host.announcements[-1] == f"Saved letter.pdf in {tmp_path}."


def test_one_page_can_go_into_the_document(tmp_path) -> None:
    host = _Host()
    host.document = Document(text="x", path=tmp_path / "notes.md")
    host._document_is_read_only = lambda: False  # type: ignore[attr-defined]
    labels = [label for _key, label in main_frame_scanning.SCAN_USES]
    host.choices = [labels.index("Insert the page into this document as a picture")]
    host.scan_pages()
    kept = Path(host.inserted[0])
    assert kept.parent == tmp_path and kept.suffix == ".png" and kept.is_file()


def test_choose_scanner_always_asks_and_says_the_choice() -> None:
    host = _Host()
    host.choices = [0]
    host.choose_scanner()
    assert host.announcements == ["Looking for scanners...", "QUILL will scan with Flatbed One."]
    assert host.settings.scanner_device == "dev-1"


STUCK = scanning.ScannerInfo("s", "Brother ADS", error="The scanner driver did not respond.")


def _choice_dialogs(host) -> list[str]:
    """The question each Choose Scanner dialog asked."""
    return [message for kind, message in host.questions if kind == "SingleChoiceDialog"]


def test_looking_for_scanners_is_spoken() -> None:
    host = _Host()
    host.start_scan(lambda s: s.cleanup())
    assert host.announcements[0] == "Looking for scanners..."


def test_the_only_scanner_not_responding_offers_retry() -> None:
    backend = FakeBackend([STUCK])
    host = _Host(backend)
    cancelled = []
    host.box_answers = [host._wx.NO]
    host.start_scan(
        lambda s: pytest.fail("nothing to scan with"), on_cancel=lambda: cancelled.append(1)
    )
    assert host.boxes[0].startswith(
        "The scanner Brother ADS isn't responding. Try again in a moment."
    )
    assert "Try again now?" in host.boxes[0]
    assert backend.scans == [] and cancelled == [1]
    assert host.settings.scanner_device == ""


def test_retry_lists_again_and_scans_once_it_answers() -> None:
    backend = FakeBackend([STUCK])
    host = _Host(backend)
    host.box_answers = [host._wx.YES]  # retry; "scan another?" then defaults to No
    replies = [[STUCK], [scanning.ScannerInfo("s", "Brother ADS")]]
    backend.list_scanners = lambda: replies.pop(0)  # type: ignore[method-assign]
    got = []
    host.start_scan(got.append)
    assert host._task_manager.submitted == ["scan-list", "scan-list", "scan-page"]
    assert backend.scans[0][0] == "s"
    got[0].cleanup()


def test_an_unresponsive_scanner_is_neither_picked_nor_offered() -> None:
    backend = FakeBackend([STUCK, scanning.ScannerInfo("a", "Canon")])
    host = _Host(backend)
    host.start_scan(lambda s: s.cleanup())
    assert backend.scans[0][0] == "a"
    assert _choice_dialogs(host) == []
    # Choose Scanner lists only the scanner that answered.
    host.choices = [0]
    host.choose_scanner()
    assert host.settings.scanner_device == "a"
    assert host.announcements[-1] == "QUILL will scan with Canon."


def test_a_merged_away_saved_scanner_gives_way_silently_and_is_updated() -> None:
    backend = FakeBackend([STUCK, scanning.ScannerInfo("escl-1", "HP")])
    host = _Host(backend)
    host.settings.scanner_device = "{6BDD}\\0001"
    host.settings.scanner_device_name = "HP"
    host.start_scan(lambda s: s.cleanup())
    assert backend.scans[0][0] == "escl-1"
    assert _choice_dialogs(host) == []
    assert (host.settings.scanner_device, host.settings.scanner_device_name) == ("escl-1", "HP")


def test_a_merged_away_saved_scanner_with_several_left_asks_once() -> None:
    backend = FakeBackend([
        scanning.ScannerInfo("a", "Canon"),
        STUCK,
        scanning.ScannerInfo("b", "HP"),
    ])
    host = _Host(backend)
    host.settings.scanner_device = "gone"
    host.settings.scanner_device_name = "HP"
    host.choices = [1]  # of the usable two: Canon, HP
    host.start_scan(lambda s: s.cleanup())
    host.start_scan(lambda s: s.cleanup())
    assert len(_choice_dialogs(host)) == 1
    assert [scan[0] for scan in backend.scans] == ["b", "b"]
    assert host.settings.scanner_device == "b"


def test_a_saved_scanner_not_responding_asks_and_says_why() -> None:
    backend = FakeBackend([STUCK, scanning.ScannerInfo("a", "Canon")])
    host = _Host(backend)
    host.settings.scanner_device = "s"
    host.settings.scanner_device_name = "Brother ADS"
    host.choices = [0]
    host.start_scan(lambda s: s.cleanup())
    asked = _choice_dialogs(host)
    assert len(asked) == 1 and asked[0].startswith("The scanner Brother ADS isn't responding.")
    assert backend.scans[0][0] == "a"


def test_choose_scanner_with_none_answering_offers_retry() -> None:
    host = _Host(FakeBackend([STUCK]))
    host.box_answers = [host._wx.NO]
    host.choose_scanner()
    assert host.boxes[0].startswith("The scanner Brother ADS isn't responding.")
    assert _choice_dialogs(host) == []


def test_the_module_never_shows_a_modal_directly() -> None:
    source = Path(main_frame_scanning.__file__).read_text(encoding="utf-8")
    assert ".ShowModal(" not in source
