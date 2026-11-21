"""The Scribe for Documents wizard's decisions and saved workflows (no wx)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quill.core import scribe_wizard as wiz


def test_page_one_offers_the_three_jobs_in_order() -> None:
    assert [label for _k, label in wiz.SOURCES] == [
        "Convert a file",
        "Scan pages",
        "Convert a web page",
    ]


def test_open_in_quill_is_first_and_the_default() -> None:
    formats = wiz.wizard_formats()
    assert formats[0].id == "html" and wiz.WizardRun().format_id == "html"
    assert wiz.format_label("html") == "Open in QUILL to read"
    labels = [wiz.format_label(f.id) for f in formats]
    assert labels[1:5] == ["Word document", "Accessible PDF", "Braille (BRF)", "Audio (MP3)"]
    assert len(formats) == len({f.id for f in formats})


def test_a_saved_file_goes_next_to_its_source(tmp_path) -> None:
    source = tmp_path / "letters" / "letter.pdf"
    source.parent.mkdir()
    source.write_bytes(b"%PDF")
    assert wiz.default_folder(wiz.SOURCE_FILE, source) == source.parent


def test_an_unwritable_source_folder_falls_back_to_documents(tmp_path) -> None:
    (tmp_path / "Documents").mkdir()
    folder = wiz.default_folder(
        wiz.SOURCE_FILE, Path("/cd/letter.pdf"), home=tmp_path, writable=lambda _p: False
    )
    assert folder == tmp_path / "Documents"


def test_scans_and_web_pages_go_to_documents_or_home(tmp_path) -> None:
    assert wiz.default_folder(wiz.SOURCE_SCAN, None, home=tmp_path) == tmp_path
    (tmp_path / "Documents").mkdir()
    assert wiz.default_folder(wiz.SOURCE_URL, None, home=tmp_path) == tmp_path / "Documents"


def test_a_run_says_what_is_missing() -> None:
    run = wiz.WizardRun()
    assert run.ready_to_convert() == "Choose a file first."
    run.path = Path("notes.xyz")
    assert "cannot convert .xyz" in run.ready_to_convert()
    run.path = Path("notes.docx")
    assert run.ready_to_convert() == ""
    scan = wiz.WizardRun(source=wiz.SOURCE_SCAN)
    assert scan.ready_to_convert() == "Scan at least one page first."
    scan.pages = [Path("p1.png")]
    assert scan.ready_to_convert() == ""
    web = wiz.WizardRun(source=wiz.SOURCE_URL, url="example.com")
    assert "https://" in web.ready_to_convert()
    web.url = "https://example.com/a.pdf"
    assert web.ready_to_convert() == ""


def test_the_summary_names_what_and_where(tmp_path) -> None:
    run = wiz.WizardRun(path=tmp_path / "letter.pdf")
    assert run.summary() == (
        "Scribe for Documents will make Open in QUILL to read from letter.pdf and open it in QUILL."
    )
    run.format_id = "brf"
    run.folder = tmp_path
    assert run.summary().endswith(f"Braille (BRF) from letter.pdf and save it in {tmp_path}.")
    scan = wiz.WizardRun(source=wiz.SOURCE_SCAN, pages=[Path("a"), Path("b")], format_id="html")
    assert "2 scanned pages" in scan.summary()


def test_result_names() -> None:
    assert wiz.result_stem(wiz.SOURCE_FILE, Path("x/Letter.pdf"), "") == "Letter"
    assert wiz.result_stem(wiz.SOURCE_URL, None, "https://ex.com/docs/Guide.pdf") == "Guide"
    assert wiz.result_stem(wiz.SOURCE_URL, None, "https://ex.com/") == "ex.com"
    assert wiz.result_stem(wiz.SOURCE_SCAN, None, "", "Scan 1") == "Scan 1"


def test_unique_paths_never_overwrite(tmp_path) -> None:
    (tmp_path / "Letter.brf").write_text("x")
    assert wiz.unique_path(tmp_path, "Letter", ".brf").name == "Letter (2).brf"


def test_suggested_workflow_names_read_naturally() -> None:
    assert wiz.suggested_name(wiz.WizardRun(source=wiz.SOURCE_SCAN, format_id="brf")) == (
        "Scan to braille"
    )
    assert wiz.suggested_name(wiz.WizardRun()) == "File to read in QUILL"


# ---------------------------------------------------------------- the store


@pytest.fixture
def store(tmp_path) -> wiz.WorkflowStore:
    return wiz.WorkflowStore(tmp_path / "scribe" / "workflows.json")


def test_a_finished_run_saves_and_comes_back_as_a_run(store, tmp_path) -> None:
    run = wiz.WizardRun(source=wiz.SOURCE_SCAN, format_id="brf", folder=tmp_path)
    store.add(wiz.Workflow.from_run("Scan to braille", run, keep_folder=True))
    [saved] = store.load()
    assert saved == wiz.Workflow("Scan to braille", "scan", "brf", str(tmp_path))
    again = saved.to_run()
    assert (again.source, again.format_id, again.folder, again.workflow) == (
        "scan",
        "brf",
        tmp_path,
        "Scan to braille",
    )
    assert saved.describe() == f"Scan pages, then Braille (BRF), saved in {tmp_path}"


def test_next_to_the_original_is_kept_as_no_folder(store) -> None:
    run = wiz.WizardRun(format_id="docx")
    workflow = wiz.Workflow.from_run("To Word", run, keep_folder=False)
    assert workflow.folder == ""
    assert workflow.describe().endswith("saved next to the original")


def test_names_are_unique_ignoring_case(store) -> None:
    store.add(wiz.Workflow("Scan to braille", "scan", "brf"))
    with pytest.raises(wiz.WorkflowError, match="already have"):
        store.add(wiz.Workflow("scan TO braille", "scan", "mp3"))
    store.add(wiz.Workflow("scan to braille", "scan", "mp3"), replace=True)
    assert [(w.name, w.format_id) for w in store.load()] == [("scan to braille", "mp3")]
    with pytest.raises(wiz.WorkflowError, match="name"):
        store.add(wiz.Workflow("   ", "scan", "brf"))


def test_rename_and_delete(store) -> None:
    store.add(wiz.Workflow("One", "file", "docx"))
    store.add(wiz.Workflow("Two", "url", "html"))
    store.rename("One", "  First   one ")
    assert [w.name for w in store.load()] == ["First one", "Two"]
    with pytest.raises(wiz.WorkflowError):
        store.rename("Two", "first ONE")
    store.delete("First one")
    assert [w.name for w in store.load()] == ["Two"]
    assert store.find("Two") is not None and store.find("Nope") is None


def test_a_damaged_file_loads_what_it_can(store) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps({
            "workflows": [
                {"name": "Good", "source": "file", "format": "pdf"},
                {"name": "Bad format", "source": "file", "format": "exe"},
                {"name": "", "source": "file", "format": "pdf"},
                "junk",
                {"name": "good", "source": "scan", "format": "brf"},
            ]
        }),
        encoding="utf-8",
    )
    assert [w.name for w in store.load()] == ["Good"]
    store.path.write_text("{not json", encoding="utf-8")
    assert store.load() == []
