"""The shared scanning logic: choosing a scanner, collecting pages, PDFs."""

from __future__ import annotations

import pytest

from quill.core import scanning
from quill.core.scanning.base import ScanOptions
from quill.core.scanning.session import UnsupportedPlatformBackend
from tests.scanning_fakes import FakeBackend

ONE = scanning.ScannerInfo("a", "Canon LiDE")
TWO = scanning.ScannerInfo("b", "HP LaserJet MFP")


def test_no_scanner_is_a_sentence() -> None:
    with pytest.raises(scanning.NoScannerFound, match="found no scanner"):
        scanning.choose_scanner([])


def test_one_scanner_is_used_without_asking() -> None:
    assert scanning.choose_scanner([ONE]) is ONE


def test_several_scanners_ask_unless_one_was_remembered() -> None:
    assert scanning.choose_scanner([ONE, TWO]) is None
    assert scanning.choose_scanner([ONE, TWO], remembered_id="b") is TWO


def test_a_remembered_scanner_that_is_gone_asks_again() -> None:
    assert scanning.choose_scanner([ONE, TWO], remembered_id="gone") is None
    assert scanning.choose_scanner([ONE], remembered_id="gone") is ONE


def test_repeated_names_are_told_apart() -> None:
    same = [scanning.ScannerInfo("1", "HP"), scanning.ScannerInfo("2", "HP")]
    assert scanning.scanner_labels(same) == ["HP", "HP (2)"]


def test_options_from_settings_fall_back_to_safe_values() -> None:
    assert ScanOptions.coerce("600", "color") == ScanOptions(600, "color")
    assert ScanOptions.coerce("lots", "purple") == ScanOptions(300, "gray")
    assert ScanOptions.coerce(5000, "GRAY") == ScanOptions(300, "gray")


def test_a_session_collects_pages_in_order_and_cleans_up(tmp_path) -> None:
    backend = FakeBackend()
    session = scanning.ScanSession(backend, ONE, ScanOptions(), folder=tmp_path / "job")
    first = session.scan_next()
    second = session.scan_next()
    assert session.pages == first + second
    assert session.page_count == 2
    assert first[0].parent != second[0].parent  # each run its own folder: no clashes
    assert backend.scans[0][0] == "a"
    session.cleanup()
    assert not (tmp_path / "job").exists()
    session.cleanup()  # twice is fine


def test_one_page_uploads_as_itself(tmp_path) -> None:
    session = scanning.ScanSession(FakeBackend(), ONE, ScanOptions(), folder=tmp_path)
    session.scan_next()
    assert session.best_upload() == session.pages[0]


def test_pages_are_copied_with_plain_names(tmp_path) -> None:
    session = scanning.ScanSession(
        FakeBackend(pages_per_scan=2), ONE, ScanOptions(), folder=tmp_path / "s"
    )
    session.scan_next()
    written = session.copy_images_to(tmp_path / "out", "Letter")
    assert [p.name for p in written] == ["Letter 1.png", "Letter 2.png"]
    again = session.copy_images_to(tmp_path / "out", "Letter")
    assert again[0].name == "Letter 1 (2).png"  # never overwrites


def test_several_pages_become_one_pdf(tmp_path) -> None:
    pytest.importorskip("PIL")
    session = scanning.ScanSession(FakeBackend(), ONE, ScanOptions(), folder=tmp_path)
    session.scan_next()
    session.scan_next()
    pdf = session.best_upload("Letter")
    assert pdf.name == "Letter.pdf"
    assert pdf.read_bytes().startswith(b"%PDF")


def test_a_pdf_with_no_pages_is_refused(tmp_path) -> None:
    with pytest.raises(scanning.ScanError):
        scanning.images_to_pdf([], tmp_path / "x.pdf")


def test_platforms_get_their_backend() -> None:
    from quill.core.scanning.pagescan_cli import PagescanBackend

    # One backend, the pagescan tool, on both platforms.
    assert isinstance(scanning.backend_for_platform(platform="win32"), PagescanBackend)
    assert isinstance(scanning.backend_for_platform(platform="darwin"), PagescanBackend)
    other = scanning.backend_for_platform(platform="linux")
    assert isinstance(other, UnsupportedPlatformBackend)
    with pytest.raises(scanning.ScanError, match="Windows and macOS"):
        other.list_scanners()


def test_file_helpers(tmp_path) -> None:
    import datetime

    from quill.core.scanning import session

    assert session.scan_name(datetime.datetime(2026, 11, 21, 9, 30)) == "Scan 2026-11-21 0930"
    assert session.documents_folder(tmp_path) == tmp_path
    (tmp_path / "Documents").mkdir()
    assert session.documents_folder(tmp_path) == tmp_path / "Documents"
    assert session.folder_is_writable(tmp_path)
    (tmp_path / "a.pdf").write_text("x")
    assert session.unique_path(tmp_path, "a", ".pdf").name == "a (2).pdf"


def test_the_session_asks_for_the_flatbed_or_else_the_feeder(tmp_path) -> None:
    glass = scanning.ScannerInfo("a", "Flatbed and feeder", has_feeder=True)
    feeder_only = scanning.ScannerInfo("b", "Sheet feeder", has_feeder=True, has_flatbed=False)
    assert glass.preferred_source == scanning.SOURCE_FLATBED
    assert feeder_only.preferred_source == scanning.SOURCE_FEEDER
    backend = FakeBackend([glass, feeder_only])
    for scanner in (glass, feeder_only):
        session = scanning.ScanSession(
            backend, scanner, scanning.ScanOptions(), folder=tmp_path / scanner.id
        )
        session.scan_next()
    assert backend.sources == [scanning.SOURCE_FLATBED, scanning.SOURCE_FEEDER]
