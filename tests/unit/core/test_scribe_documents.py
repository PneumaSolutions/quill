"""Scribe for Documents conversion client (quill.core.scribe_documents).

Driven entirely by the fake ``scribe_client`` in tests/scribe_fakes.py: no
socket, no server, no real credentials. Pins the policy promises (every
status update reaches the person, one upload per document, a dropped channel
reconnects and asks whether the output finished meanwhile) and the error
sentences for the cases a person can act on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quill.core import scribe_documents as scribe
from tests.scribe_fakes import (
    FakeChannel,
    FakeModule,
    complete,
    memory_secrets,
    status,
)


@pytest.fixture(autouse=True)
def _registered(monkeypatch) -> None:
    monkeypatch.setenv(scribe.SCRIBE_CLIENT_ID_ENV, "test-client")
    monkeypatch.delenv("QUILL_SAFE_MODE", raising=False)


@pytest.fixture
def secrets():
    manager = memory_secrets()
    scribe.save_tokens(scribe.StoredTokens("access", "refresh", 2_000_000_000), manager)
    return manager


@pytest.fixture
def pdf(tmp_path) -> Path:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.7 fake")
    return path


def _run(job, module, secrets, **kwargs):
    heard: list[str] = []
    result = scribe.run_conversion(
        job,
        module=module,
        manager=secrets,
        on_status=heard.append,
        sleep=lambda _s: None,
        now=lambda: 1_000_000_000,
        **kwargs,
    )
    return result, heard


# ------------------------------------------------------------- client id


def test_the_registered_client_id_is_built_in(monkeypatch) -> None:
    monkeypatch.delenv(scribe.SCRIBE_CLIENT_ID_ENV, raising=False)
    assert scribe.SCRIBE_CLIENT_ID != scribe.SCRIBE_CLIENT_ID_PLACEHOLDER
    assert scribe.client_id() == scribe.SCRIBE_CLIENT_ID
    assert scribe.client_id_configured()


def test_the_placeholder_client_id_means_not_registered(monkeypatch) -> None:
    monkeypatch.delenv(scribe.SCRIBE_CLIENT_ID_ENV, raising=False)
    import quill.core.scribe_account as account

    monkeypatch.setattr(account, "SCRIBE_CLIENT_ID", account.SCRIBE_CLIENT_ID_PLACEHOLDER)
    assert not scribe.client_id_configured()


def test_the_environment_supplies_a_development_client_id() -> None:
    assert scribe.client_id() == "test-client"
    assert scribe.client_id_configured()


def test_the_redirect_is_a_fixed_loopback_address() -> None:
    assert scribe.SCRIBE_REDIRECT_URI.startswith("http://127.0.0.1:")
    assert scribe.SCRIBE_REDIRECT_URI.endswith("/callback")
    assert scribe.SCRIBE_BASE_URL.startswith("https://")


# ----------------------------------------------------------------- tokens


def test_tokens_round_trip_through_the_vault_and_sign_out_wipes_them() -> None:
    manager = memory_secrets()
    assert not scribe.signed_in(manager)
    scribe.save_tokens(scribe.StoredTokens("a", "r", 123), manager)
    assert scribe.load_tokens(manager) == scribe.StoredTokens("a", "r", 123)
    assert scribe.sign_out(manager) is True
    assert scribe.load_tokens(manager) is None
    assert scribe.sign_out(manager) is False


def test_an_expiring_token_is_refreshed_and_the_new_one_stored(pdf) -> None:
    module = FakeModule()
    manager = memory_secrets()
    scribe.save_tokens(scribe.StoredTokens("old", "refresh-old", 1_000_000_100), manager)
    module.channels = [FakeChannel([complete("html")])]
    result, _ = _run(scribe.ScribeJob(path=pdf), module, manager)
    assert result.kind == scribe.DONE
    assert ("refresh", "refresh-old") in module.calls
    assert scribe.load_tokens(manager).access_token == "access-fresh"


def test_a_refused_refresh_signs_out_and_asks_for_sign_in(pdf) -> None:
    module = FakeModule()
    manager = memory_secrets()
    scribe.save_tokens(scribe.StoredTokens("old", "spent", 1_000_000_100), manager)
    module.refresh_error = module.InvalidGrantError("invalid_grant")
    with pytest.raises(scribe.ScribeSignInRequired):
        _run(scribe.ScribeJob(path=pdf), module, manager)
    assert scribe.load_tokens(manager) is None


def test_no_sign_in_is_a_sign_in_required_error(pdf) -> None:
    with pytest.raises(scribe.ScribeSignInRequired):
        _run(scribe.ScribeJob(path=pdf), FakeModule(), memory_secrets())


def test_safe_mode_refuses_before_any_call(monkeypatch, pdf, secrets) -> None:
    monkeypatch.setenv("QUILL_SAFE_MODE", "1")
    module = FakeModule()
    with pytest.raises(scribe.ScribeDocumentsError, match="Safe Mode"):
        _run(scribe.ScribeJob(path=pdf), module, secrets)
    assert module.calls == []


# ------------------------------------------------------------- converting


def test_a_conversion_passes_on_every_status_update_and_downloads(pdf, secrets) -> None:
    module = FakeModule()
    channel = FakeChannel([
        status("html", "queue", 0.0),
        status("html", "convert", 0.4),
        status("html", "convert", 0.4),  # an exact repeat is not said twice
        status("html", "add_image_descriptions", 0.8),
        complete("html"),
    ])
    module.channels = [channel]
    result, heard = _run(scribe.ScribeJob(path=pdf), module, secrets)
    assert result.kind == scribe.DONE
    assert result.data == module.download_bytes
    assert ("upload", "report.pdf", len(b"%PDF-1.7 fake"), None) in module.calls
    assert "Scribe for Documents: HTML waiting in the queue, 0 percent." in heard
    assert "Scribe for Documents: HTML converting, 40 percent." in heard
    assert heard.count("Scribe for Documents: HTML converting, 40 percent.") == 1
    assert "Scribe for Documents: HTML adding image descriptions, 80 percent." in heard
    assert "Scribe for Documents: HTML complete." in heard
    assert channel.closed


def test_a_busy_new_document_waits_for_its_preview_then_starts(pdf, secrets) -> None:
    module = FakeModule()
    channel = FakeChannel(
        [status("html_stream", "convert", 0.5), complete("html_stream"), complete("docx")],
        starts=[module.ConversionInProgressError("busy"), None],
    )
    module.channels = [channel]
    result, heard = _run(scribe.ScribeJob(path=pdf, output_format="docx"), module, secrets)
    assert result.kind == scribe.DONE
    assert channel.started == [("docx", None), ("docx", None)]
    assert "Scribe for Documents: Preview converting, 50 percent." in heard
    assert ("download", "doc-1", "docx") in module.calls


def test_an_already_finished_format_downloads_without_waiting(pdf, secrets) -> None:
    module = FakeModule()
    module.outputs = [("pdf", "complete")]
    module.channels = [FakeChannel([])]  # no events would ever arrive
    result, _ = _run(scribe.ScribeJob(path=pdf, output_format="pdf"), module, secrets)
    assert result.kind == scribe.DONE
    assert ("download", "doc-1", "pdf") in module.calls


def test_a_url_is_sent_for_scribe_to_fetch(secrets) -> None:
    module = FakeModule()
    module.channels = [FakeChannel([complete("html")])]
    job = scribe.ScribeJob(url="https://example.org/paper.pdf")
    result, _ = _run(job, module, secrets)
    assert result.kind == scribe.DONE
    assert ("url", "https://example.org/paper.pdf", None) in module.calls


def test_an_unsupported_file_is_refused_before_upload(tmp_path, secrets) -> None:
    module = FakeModule()
    path = tmp_path / "song.flac"
    path.write_bytes(b"x")
    with pytest.raises(scribe.ScribeDocumentsError, match="cannot convert"):
        _run(scribe.ScribeJob(path=path), module, secrets)
    assert not any(call[0] == "upload" for call in module.calls)


def test_a_bad_url_is_refused() -> None:
    with pytest.raises(scribe.ScribeDocumentsError, match="https://"):
        scribe.check_url("example.org")


# --------------------------------------------------------------- passwords


def test_a_locked_pdf_asks_for_a_password_then_resumes_without_reuploading(pdf, secrets) -> None:
    module = FakeModule()
    module.channels = [
        FakeChannel([], starts=[module.PasswordRequiredError("locked")]),
        FakeChannel([complete("html")]),
    ]
    job = scribe.ScribeJob(path=pdf)
    first, _ = _run(job, module, secrets)
    assert first.kind == scribe.PASSWORD_NEEDED
    job.password = "hunter2"
    second, _ = _run(job, module, secrets)
    assert second.kind == scribe.DONE
    assert [call for call in module.calls if call[0] == "upload"] == [
        ("upload", "report.pdf", len(b"%PDF-1.7 fake"), None)
    ]
    assert module.channels == []


def test_a_wrong_password_is_reported_as_wrong(pdf, secrets) -> None:
    module = FakeModule()
    module.channels = [FakeChannel([{"type": "error", "reason": "invalid_password"}])]
    job = scribe.ScribeJob(path=pdf, password="wrong")
    result, _ = _run(job, module, secrets)
    assert result.kind == scribe.PASSWORD_WRONG


# ------------------------------------------------------------- reconnects


def test_a_dropped_channel_reconnects_and_finds_the_finished_output(pdf, secrets) -> None:
    module = FakeModule()

    class _Dropping(FakeChannel):
        def next_event(self):
            event = super().next_event()
            module.outputs = [("html", "complete")]  # it finished while we were away
            return event

    module.channels = [_Dropping([status("html", "convert", 0.2), module.ScribeApiError("reset")])]
    result, heard = _run(scribe.ScribeJob(path=pdf), module, secrets)
    assert result.kind == scribe.DONE
    assert any("Reconnecting, attempt 1 of 5" in line for line in heard)
    assert "Scribe for Documents: reconnected." in heard
    assert [c[0] for c in module.calls].count("upload") == 1


def test_a_dropped_channel_reopens_when_the_output_is_not_finished(pdf, secrets) -> None:
    module = FakeModule()
    module.channels = [
        FakeChannel([module.ScribeApiError("reset")]),
        FakeChannel([status("html", "convert", 0.9), complete("html")]),
    ]
    result, _ = _run(scribe.ScribeJob(path=pdf), module, secrets)
    assert result.kind == scribe.DONE
    assert [c[0] for c in module.calls].count("channel") == 2


def test_reconnecting_gives_up_after_the_limit(pdf, secrets) -> None:
    module = FakeModule()
    module.channels = [module.ScribeApiError("down")] * (scribe.MAX_RECONNECTS + 1)
    with pytest.raises(scribe.ScribeDocumentsError, match="lost its connection"):
        _run(scribe.ScribeJob(path=pdf), module, secrets)
    assert [c[0] for c in module.calls].count("upload") == 1


def test_cancel_stops_waiting(pdf, secrets) -> None:
    module = FakeModule()
    module.channels = [FakeChannel([status("html", "convert", 0.1)])]
    with pytest.raises(scribe.ScribeCancelled):
        _run(scribe.ScribeJob(path=pdf), module, secrets, cancel_requested=lambda: True)


# ----------------------------------------------------------------- errors


def test_no_credits_says_what_to_do(pdf, secrets) -> None:
    module = FakeModule()
    module.channels = [
        FakeChannel([], starts=[module.NeedsPurchaseError("Not enough pages (https://x)")])
    ]
    with pytest.raises(scribe.ScribeDocumentsError, match="page credits"):
        _run(scribe.ScribeJob(path=pdf), module, secrets)


def test_a_conversion_error_event_names_the_document(pdf, secrets) -> None:
    module = FakeModule()
    module.channels = [FakeChannel([{"type": "error", "reason": "The file is damaged."}])]
    with pytest.raises(scribe.ScribeDocumentsError, match=r"report\.pdf: The file is damaged"):
        _run(scribe.ScribeJob(path=pdf), module, secrets)


def test_an_upload_failure_is_a_sentence(pdf, secrets) -> None:
    module = FakeModule()
    module.upload_error = module.ScribeApiError("connection refused")
    with pytest.raises(scribe.ScribeDocumentsError, match="reported a problem"):
        _run(scribe.ScribeJob(path=pdf), module, secrets)


def test_a_rejected_grant_mid_job_signs_out(pdf, secrets) -> None:
    module = FakeModule()
    module.channels = [module.InvalidGrantError("revoked")]
    with pytest.raises(scribe.ScribeSignInRequired):
        _run(scribe.ScribeJob(path=pdf), module, secrets)
    assert not scribe.signed_in(secrets)


def test_a_missing_client_package_is_a_sentence(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def _no_scribe(name, *args, **kwargs):
        if name == "scribe_client":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_scribe)
    with pytest.raises(scribe.ScribeDocumentsError, match="scribe-client"):
        scribe.client_module()


# ---------------------------------------------------------------- formats


def test_html_is_the_default_and_the_only_format_opened_in_quill() -> None:
    assert scribe.DEFAULT_FORMAT.id == "html"
    assert [f.id for f in scribe.OUTPUT_FORMATS if f.opens_in_quill] == ["html"]
    assert {f.id for f in scribe.OUTPUT_FORMATS} == {
        "html",
        "docx",
        "pdf",
        "epub",
        "daisy",
        "mp3",
        "brf",
        "offline_html",
        "mobi",
    }


def test_a_zipped_result_is_saved_as_a_zip() -> None:
    mp3 = scribe.format_by_id("mp3")
    assert scribe.output_extension(mp3, b"ID3...") == ".mp3"
    assert scribe.output_extension(mp3, b"PK\x03\x04rest") == ".zip"
    docx = scribe.format_by_id("docx")
    assert scribe.output_extension(docx, b"PK\x03\x04rest") == ".docx"


def test_status_sentences_read_naturally() -> None:
    assert (
        scribe.status_sentence("daisy", "convert", 0.256)
        == "Scribe for Documents: DAISY converting, 26 percent."
    )
    assert (
        scribe.status_sentence("brf", "complete", 1.0) == "Scribe for Documents: Braille complete."
    )
