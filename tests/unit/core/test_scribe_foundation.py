"""Scribe for Documents' shared foundation against the fake server (PRD 13).

QUILL's own client, channel, queue and runner, unmodified, talking to the
real HTTP and WebSocket servers in tests/scribe_fake_server.py.
"""

from __future__ import annotations

import logging
import threading

import pytest

from quill.core.scribe import auth, model
from quill.core.scribe import queue as q
from quill.core.scribe.channel import DocumentChannel
from quill.core.scribe.client import ScribeClient, check_link, safe_filename, unique_target
from quill.core.scribe.library import Library, download_folder
from quill.core.scribe.runner import QueueRunner, cancel_waiting
from tests.scribe_fake_server import memory_store, running_fake

pytest.importorskip("websockets")


@pytest.fixture
def fake(monkeypatch, quill_data_dir):
    monkeypatch.delenv("QUILL_SAFE_MODE", raising=False)
    with running_fake() as server:
        monkeypatch.setenv(auth.BASE_URL_ENV, server.base_url)
        yield server


@pytest.fixture
def store():
    return memory_store()


def _runner(fake, store, tmp_path, said=None, results=None, **kw):
    client = ScribeClient(store)
    return QueueRunner(
        app="QUILL",
        jobs=q.JobQueue(tmp_path / "shared"),
        lock=q.RunnerLock(tmp_path / "shared"),
        client=client,
        library=Library(tmp_path / "shared"),
        channel_factory=lambda doc_id: DocumentChannel(
            doc_id, store=store, connector=fake.connector
        ),
        on_say=lambda kind, text, job: (said if said is not None else []).append((kind, text)),
        on_result=lambda job: (results if results is not None else []).append(job),
        sleep=lambda _s: None,
        **kw,
    )


def _dead_pid() -> int:
    """The id of a process that has already ended (the app that closed)."""
    import subprocess
    import sys

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def _job(tmp_path, *formats, name="Notes.pdf", **kw):
    source = tmp_path / name
    source.write_bytes(b"%PDF-1.4 fake")
    return q.Job(
        title=name,
        source_path=str(source),
        formats=list(formats),
        folder=str(download_folder(name, str(tmp_path / "out"))),
        **kw,
    )


# ------------------------------------------------------------------ model


def test_formats_follow_scribes_build_order() -> None:
    assert model.in_build_order(["daisy", "brf", "mp3", "html"]) == ["html", "brf", "mp3", "daisy"]
    assert model.labels_sentence(["docx", "brf"]) == "Word and Braille (BRF)"
    assert model.groups_for(["brf"]) == ["Language and voice", "Braille", "Pictures and maths"]
    assert "Print" in model.groups_for(["docx"])


def test_settings_round_trip_with_json_string_maps() -> None:
    settings = model.DocumentSettings(voices={"en-US": "en-US-AvaNeural"}, tts_rate=1.5)
    sent = settings.to_json()
    assert sent["voices"] == '{"en-US": "en-US-AvaNeural"}'
    assert model.DocumentSettings.from_json(sent) == settings


def test_every_error_row_has_its_code() -> None:
    rows = {
        "unauthenticated": "QUILL-SCRIBE-SIGNIN",
        "invalid_grant": "QUILL-SCRIBE-SIGNIN",
        "needs_purchase": "QUILL-SCRIBE-PAGES",
        "password_required": "QUILL-SCRIBE-PASSWORD",
        "invalid_password": "QUILL-SCRIBE-PASSWORD",
        "rate_limited": "QUILL-SCRIBE-BUSY",
        "not_found": "QUILL-SCRIBE-GONE",
        "forbidden": "QUILL-SCRIBE-GONE",
        "server_error": "QUILL-SCRIBE-FAILED",
    }
    for server_code, quill_code in rows.items():
        error = model.error_for(server_code, "The server's own sentence.")
        assert error.code == quill_code
        assert model.plain(error) == "The server's own sentence."  # never the code
    assert model.ScribeOffline("x").code == "QUILL-SCRIBE-OFFLINE"
    assert model.ScribeSignInLocal("x").code == "QUILL-SCRIBE-SIGNIN-LOCAL"
    assert model.ScribeSaveFailed("x").code == "QUILL-SCRIBE-SAVE"
    assert model.ScribeOff("x").code == "QUILL-SCRIBE-OFF"


def test_file_names_are_safe_and_never_overwrite(tmp_path) -> None:
    assert safe_filename("a<b>:c?.docx") == "a_b_c_.docx"
    (tmp_path / "Notes.docx").write_text("x")
    assert unique_target(tmp_path, "Notes.docx").name == "Notes (2).docx"


def test_downloads_go_to_documents_scribe_for_documents_title(tmp_path) -> None:
    (tmp_path / "Documents").mkdir()
    assert download_folder("Notes.pdf", home=tmp_path) == (
        tmp_path / "Documents" / "Scribe for Documents" / "Notes"
    )
    assert download_folder("Notes", str(tmp_path / "x")) == tmp_path / "x" / "Notes"


# ------------------------------------------------------------------ auth


def test_safe_mode_turns_everything_off(monkeypatch, store) -> None:
    monkeypatch.setenv("QUILL_SAFE_MODE", "1")
    with pytest.raises(model.ScribeOff):
        ScribeClient(store).account()
    with pytest.raises(model.ScribeOff):
        auth.SignInFlow(store).start()


def test_the_sign_in_url_goes_through_logout_with_pkce(fake) -> None:
    url = auth.authorization_url("http://127.0.0.1:5555/callback", "CHALLENGE")
    assert url.startswith(f"{fake.base_url}/auth/logout?next=%2Foauth%2Fauthorize")
    assert "code_challenge%3DCHALLENGE" in url and "S256" in url
    assert "redirect_uri%3Dhttp%253A%252F%252F127.0.0.1%253A5555%252Fcallback" in url


def test_sign_in_through_the_loopback_listener_stores_tokens(fake) -> None:
    import urllib.request

    store = memory_store(signed_in=False)
    flow = auth.SignInFlow(store)
    url = flow.start()
    from urllib.parse import parse_qs, urlsplit

    next_path = parse_qs(urlsplit(url).query)["next"][0]
    authorize = parse_qs(urlsplit(next_path).query)
    redirect = authorize["redirect_uri"][0]
    state = authorize["state"][0]
    assert redirect.startswith("http://127.0.0.1:") and redirect.endswith("/callback")
    assert len(state) >= 32
    page: list[bytes] = []
    threading.Timer(
        0.2,
        lambda: page.append(
            urllib.request.urlopen(f"{redirect}?code=good-code&state={state}").read()
        ),
    ).start()
    tokens = flow.finish(timeout=10)
    assert tokens.access_token.startswith("access-") and store.signed_in()
    assert b"You can close this tab and return to QUILL." in page[0]


def test_a_redirect_without_the_right_state_is_refused_and_the_wait_goes_on(fake) -> None:
    """Security review: another program calling the listener first changes nothing."""
    import urllib.error
    import urllib.request
    from urllib.parse import parse_qs, urlsplit

    store = memory_store(signed_in=False)
    flow = auth.SignInFlow(store)
    authorize = parse_qs(urlsplit(parse_qs(urlsplit(flow.start()).query)["next"][0]).query)
    redirect, state = authorize["redirect_uri"][0], authorize["state"][0]
    refused: list[int] = []

    def attacker_then_browser() -> None:
        for query in ("?code=evil", "?code=evil&state=wrong", "?error=access_denied"):
            try:
                urllib.request.urlopen(redirect + query)
            except urllib.error.HTTPError as exc:
                refused.append(exc.code)
        urllib.request.urlopen(f"{redirect}?code=good-code&state={state}").read()

    threading.Timer(0.2, attacker_then_browser).start()
    tokens = flow.finish(timeout=10)
    assert refused == [400, 400, 400]
    assert tokens.access_token.startswith("access-")


def test_a_refused_code_is_a_sign_in_error(fake) -> None:
    with pytest.raises(model.ScribeSignInRequired):
        auth.token_request({"grant_type": "authorization_code", "code": "bad"})


def test_an_expired_token_is_refreshed_once_on_401(fake, store) -> None:
    fake.expire_next = True
    account = ScribeClient(store).account()
    assert account.name == "Mike Calvo" and account.pages_remaining == 100
    assert store.load().access_token != "access-1"


def test_a_refresh_that_fails_means_sign_in_again(fake) -> None:
    store = memory_store()
    store.save(auth.StoredTokens("dead", "dead-refresh", 4_000_000_000))
    with pytest.raises(model.ScribeSignInRequired):
        ScribeClient(store).account()


def test_sign_out_revokes_and_wipes(fake, store) -> None:
    assert auth.sign_out(store) is True
    assert fake.revoked == ["refresh-1"] and not store.signed_in()
    assert auth.sign_out(store) is False


def test_the_account_page_opens_signed_in(fake, store) -> None:
    url = ScribeClient(store).account_page_url()
    assert "/auth/browser_from_oauth?token=access-1" in url


# ------------------------------------------------------------------ documents


def test_my_documents_rename_trash_recover_delete(fake, store) -> None:
    doc = fake.add_document("Notes", outputs={"docx": "complete"})
    client = ScribeClient(store)
    listed = client.documents()
    assert [d.title for d in listed.documents] == ["Notes"]
    assert listed.documents[0].ready_formats() == ["docx"] and listed.pages_remaining == 100
    assert client.rename(doc.id, "Meeting notes").title == "Meeting notes"
    with pytest.raises(model.ScribeError):
        client.delete_forever(doc.id)  # only from the trash
    client.trash(doc.id)
    assert client.documents().documents == []
    assert client.trash_list()[0].permanently_delete_at == "2026-11-28"
    client.recover(doc.id)
    client.trash(doc.id)
    client.delete_forever(doc.id)
    with pytest.raises(model.ScribeGone):
        client.outputs(doc.id)


def test_settings_lists_come_from_the_server(fake, store) -> None:
    client = ScribeClient(store)
    assert ("English", "en") in client.languages()
    assert client.voices()["en-US"][0] == ("Ava", "en-US-AvaNeural", True)
    assert client.braille_tables()[0][1] == "en-ueb-g2.ctb"


# ------------------------------------------------------------------ the queue


def test_a_job_uploads_converts_each_format_in_order_and_downloads(fake, store, tmp_path) -> None:
    said, results = [], []
    runner = _runner(fake, store, tmp_path, said, results)
    runner.jobs.add([_job(tmp_path, "brf", "docx")])
    assert runner.run_once() is True
    job = results[0]
    assert job.state == q.DONE and job.percent == 100
    assert fake.started == ["docx", "brf"]  # Scribe's order, not the person's
    names = [p.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for p in job.results]
    assert names == ["docx", "brf"] or all(n.startswith("Notes") for n in names)
    assert ("start", "Scribe for Documents is converting Notes.pdf.") in said
    assert ("stage", "Notes.pdf, adding image descriptions.") in said
    assert [t for k, t in said if k == "percent"] == [
        "Notes.pdf, 25 percent.",
        "Notes.pdf, 50 percent.",
        "Notes.pdf, 75 percent.",
    ]
    assert Library(tmp_path / "shared").files_for(job.document_id)
    assert len(fake.uploads) == 1


def test_accessible_html_arrives_as_the_web_page_unpacked(fake, store, tmp_path) -> None:
    results = []
    runner = _runner(fake, store, tmp_path, results=results)
    runner.jobs.add([_job(tmp_path, "html")])
    runner.run_once()
    page = results[0].results[0]
    assert page.endswith("index.html")
    assert "Converted." in open(page, encoding="utf-8").read()
    assert fake.started == ["offline_html"]


def test_a_dropped_channel_reconnects_without_uploading_again(fake, store, tmp_path) -> None:
    said, results = [], []
    fake.drop_after_frames = 2
    runner = _runner(fake, store, tmp_path, said, results)
    runner.jobs.add([_job(tmp_path, "docx")])
    runner.run_once()
    assert results and results[0].state == q.DONE
    assert len(fake.uploads) == 1


def test_password_required_waits_for_the_person_without_a_dialog(fake, store, tmp_path) -> None:
    said = []
    fake.password_for_next_upload = "hunter2"
    runner = _runner(fake, store, tmp_path, said)
    [job] = runner.jobs.add([_job(tmp_path, "docx")])
    runner.run_once()
    assert runner.jobs.get(job.id).state == q.NEEDS_PASSWORD
    assert ("needs", "Notes.pdf needs its password. Open Scribe Conversions to enter it.") in said
    runner.give_password(job.id, "hunter2")
    runner.run_once()
    assert runner.jobs.get(job.id).state == q.DONE
    assert len(fake.uploads) == 1


def test_needs_purchase_keeps_the_purchase_link(fake, store, tmp_path) -> None:
    said = []
    fake.start_errors = [
        {
            "reason": "needs_purchase",
            "message": "You are out of pages.",
            "purchase_url": "https://scribeit.io/documents/x/docx/pricing_options",
        }
    ]
    runner = _runner(fake, store, tmp_path, said)
    [job] = runner.jobs.add([_job(tmp_path, "docx")])
    runner.run_once()
    job = runner.jobs.get(job.id)
    assert job.state == q.NEEDS_PAGES and job.error_code == "QUILL-SCRIBE-PAGES"
    assert job.purchase_url.endswith("/pricing_options")
    assert (
        said[-1][1] == "You are out of pages. You can still read the first two pages: "
        "Open Preview, in Scribe Conversions."
    )


def test_a_conversion_error_says_the_servers_message(fake, store, tmp_path) -> None:
    said = []
    fake.scripts["docx"] = [
        ("status", "convert", 0.2),
        ("error", "unsupported_format", "That file is not a document."),
    ]
    runner = _runner(fake, store, tmp_path, said)
    [job] = runner.jobs.add([_job(tmp_path, "docx")])
    runner.run_once()
    assert runner.jobs.get(job.id).state == q.FAILED
    assert said[-1] == (
        "error",
        "Scribe for Documents could not convert Notes.pdf: That file is not a document.",
    )


def test_rate_limited_slows_down_and_carries_on(fake, store, tmp_path) -> None:
    said, results = [], []
    fake.start_errors = [{"reason": "rate_limited", "message": "Slow down."}]
    runner = _runner(fake, store, tmp_path, said, results)
    runner.jobs.add([_job(tmp_path, "docx")])
    runner.run_once()
    assert ("error", "Scribe for Documents asked QUILL to slow down.") in said
    assert results[0].state == q.DONE


def test_a_signed_out_job_pauses(fake, tmp_path) -> None:
    store = memory_store(signed_in=False)
    runner = _runner(fake, store, tmp_path)
    [job] = runner.jobs.add([_job(tmp_path, "docx")])
    runner.run_once()
    assert runner.jobs.get(job.id).state == q.NEEDS_SIGNIN


def test_batch_without_the_exception_waits_for_send_next_file(tmp_path) -> None:
    jobs = q.JobQueue(tmp_path)
    first, second = jobs.add([
        _job(tmp_path, "docx", name="a.pdf"),
        _job(tmp_path, "docx", name="b.pdf"),
    ])
    assert jobs.get(first.id).state == q.WAITING
    assert jobs.get(second.id).state == q.WAITING_YOU
    other = q.JobQueue(tmp_path)
    assert [j.id for j in other.jobs()] == [first.id, second.id]  # the family sees one queue


def test_cancel_waiting_says_what_it_means(tmp_path) -> None:
    jobs = q.JobQueue(tmp_path)
    [job] = jobs.add([_job(tmp_path, "docx")])
    assert cancel_waiting(jobs, job.id) == (
        "QUILL stopped waiting for Notes.pdf. Scribe will still finish it and may use pages; "
        "it stays in My Scribe Documents."
    )
    assert jobs.get(job.id).state == q.CANCELLED


def test_one_runner_at_a_time_and_a_stale_lock_passes_on(tmp_path, monkeypatch) -> None:
    first = q.RunnerLock(tmp_path, app="QUILL")
    second = q.RunnerLock(tmp_path, app="QUILL Lite")
    second.pid = first.pid + 1
    assert first.acquire() and not second.acquire()
    import time as _time

    later = _time.time() + q.RUNNER_STALE_S + 1
    monkeypatch.setattr(q.time, "time", lambda: later)
    assert second.acquire()  # the first app closed or crashed


def test_a_job_survives_a_restart_and_is_never_uploaded_twice(fake, store, tmp_path) -> None:
    results = []
    jobs = q.JobQueue(tmp_path / "shared")
    [job] = jobs.add([_job(tmp_path, "docx")])
    # The app that started it uploaded, then closed mid-conversion.
    doc = fake.add_document("Notes")
    jobs.update(
        job.id,
        lambda j: (
            setattr(j, "document_id", doc.id),
            setattr(j, "state", q.CONVERTING),
            setattr(j, "owner_pid", _dead_pid()),
        ),
    )
    runner = _runner(fake, store, tmp_path, results=results)
    runner.run_once()
    assert results[0].state == q.DONE and results[0].resumed is True
    assert fake.uploads == []


def test_result_sentences(tmp_path) -> None:
    folder = tmp_path / "Scribe for Documents" / "Notes"
    job = q.Job(title="Notes.pdf", results=[str(folder / "Notes.docx"), str(folder / "Notes.brf")])
    sentence = q.result_sentence(job, opened="Word")
    assert sentence.startswith("Scribe for Documents made Notes.docx and Notes.brf in ")
    assert sentence.endswith("Notes. Word is open in QUILL.")
    assert q.result_sentence(job, offer=True).endswith("Open it from Scribe Conversions.")
    assert q.finished_while_closed_sentence([
        job,
        q.Job(title="Chapter 2.docx", results=[str(folder / "c.docx")]),
    ]).startswith(
        "Scribe for Documents finished 2 documents while QUILL was closed: Notes and Chapter 2"
    )


def test_a_password_never_reaches_the_log_or_the_queue_file(fake, store, tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    fake.password_for_next_upload = "s3cret-pass"
    runner = _runner(fake, store, tmp_path)
    [job] = runner.jobs.add([_job(tmp_path, "docx")])
    runner.run_once()
    runner.give_password(job.id, "s3cret-pass")
    runner.run_once()
    assert "s3cret-pass" not in caplog.text
    assert "s3cret-pass" not in runner.jobs.path.read_text(encoding="utf-8")


def test_the_real_websocket_connect_is_quiet(monkeypatch) -> None:
    """websockets logs the token and every frame at DEBUG; QUILL's logger for it never does."""
    import websockets.sync.client as ws_client

    from quill.core.scribe import channel

    seen = {}
    monkeypatch.setattr(ws_client, "connect", lambda url, **kw: seen.update(kw) or "ws")
    assert channel.connect("wss://scribeit.io/socket/websocket?token=t") == "ws"
    assert seen["logger"].getEffectiveLevel() >= logging.WARNING


# ------------------------------------------------------------------ live findings (2026-11-21)


def test_each_stage_is_said_once_a_job(fake, store, tmp_path) -> None:
    """Every format passes through "converting"; a nine-format job said it nine times."""
    said = []
    runner = _runner(fake, store, tmp_path, said)
    runner.jobs.add([_job(tmp_path, "docx", "pdf", "brf")])
    runner.run_once()
    stages = [text for kind, text in said if kind == "stage"]
    assert stages.count("Notes.pdf, converting.") == 1
    assert stages.count("Notes.pdf, adding image descriptions.") == 1


def test_a_wrong_password_counts_and_the_third_ends_the_job(fake, store, tmp_path) -> None:
    said = []
    fake.password_for_next_upload = "hunter2"
    runner = _runner(fake, store, tmp_path, said)
    [job] = runner.jobs.add([_job(tmp_path, "docx")])
    runner.run_once()
    runner.give_password(job.id, "wrong-1")
    runner.run_once()
    assert runner.jobs.get(job.id).state == q.NEEDS_PASSWORD
    assert said[-1] == (
        "needs",
        "Password not accepted for Notes.pdf. 2 tries left. "
        "Open Scribe Conversions to enter it again.",
    )
    runner.give_password(job.id, "wrong-2")
    runner.run_once()
    assert "1 try left" in said[-1][1]
    runner.give_password(job.id, "wrong-3")
    runner.run_once()
    job = runner.jobs.get(job.id)
    assert job.state == q.FAILED and job.error_code == "QUILL-SCRIBE-PASSWORD"
    assert said[-1] == (
        "error",
        "Scribe for Documents could not convert Notes.pdf: password not accepted.",
    )


def test_a_password_typed_in_another_app_reaches_the_app_running_the_job(
    fake, store, tmp_path
) -> None:
    """PRD 7.4a: Enter Password... in a Scribe Conversions window of an app that
    does not hold the queue lock hands the password to the one that does."""
    fake.password_for_next_upload = "hunter2"
    running = _runner(fake, store, tmp_path)
    [job] = running.jobs.add([_job(tmp_path, "docx")])
    running.run_once()
    assert running.jobs.get(job.id).state == q.NEEDS_PASSWORD
    other = _runner(fake, store, tmp_path)
    other.lock.pid = running.lock.pid + 1  # a second app: it does not hold the lock
    other.give_password(job.id, "hunter2")
    assert "hunter2" not in running.jobs.path.read_text(encoding="utf-8")
    running.run_once()
    assert running.jobs.get(job.id).state == q.DONE
    from quill.core.scribe.runner import take_handed_password

    assert take_handed_password(store, job.id) is None  # read once, then wiped


def test_accessible_html_and_the_offline_page_are_one_download(fake, store, tmp_path) -> None:
    results = []
    runner = _runner(fake, store, tmp_path, results=results)
    runner.jobs.add([_job(tmp_path, "html", "offline_html")])
    runner.run_once()
    [job] = results
    assert job.done_formats == ["html", "offline_html"]
    assert len(job.results) == 1 and job.results[0].endswith("index.html")
    downloads = [p for m, p in fake.calls if m == "GET" and p.endswith("/download")]
    assert len(downloads) == 1


def test_a_daisy_result_unpacks_once_to_its_navigation(tmp_path) -> None:
    import zipfile

    from quill.core.scribe.library import daisy_navigation

    archive = tmp_path / "Notes.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Notes/dtb.ncx", "<ncx/>")
        bundle.writestr("Notes/0.mp3", b"mp3")
        bundle.writestr("../escape.txt", "never")
    ncx = daisy_navigation(archive)
    assert ncx == tmp_path / "Notes (DAISY)" / "Notes" / "dtb.ncx" and ncx.is_file()
    assert not (tmp_path / "escape.txt").exists()
    assert daisy_navigation(archive) == ncx
    other = tmp_path / "page.zip"
    with zipfile.ZipFile(other, "w") as bundle:
        bundle.writestr("index.html", "<html/>")
    assert daisy_navigation(other) is None
    assert not (tmp_path / "page (DAISY)").exists()


def test_a_voice_sample_comes_through_the_scribe_door_without_a_token(fake, store) -> None:
    data = ScribeClient(store).voice_sample("en-US-AvaNeural")
    assert data == b"ID3 sample of en-US-AvaNeural.mp3"
    assert ("GET", "/sounds/tts-samples/en-US-AvaNeural.mp3") in fake.calls


@pytest.mark.parametrize(
    ("server_name", "expected"),
    [
        ("../../evil.docx", "_.._evil.docx"),
        ("..\\..\\Windows\\evil.dll", "_.._Windows_evil.dll"),
        ("/etc/passwd", "_etc_passwd"),
        ("C:\\Users\\x.docx", "C_Users_x.docx"),
        ("..", "Scribe result"),
        ("CON.docx", "_CON.docx"),
        ("nul", "_nul"),
    ],
)
def test_server_file_names_never_leave_the_folder(tmp_path, server_name, expected) -> None:
    """Security review: path traversal and device names in a download's name."""
    name = safe_filename(server_name)
    assert name == expected
    target = unique_target(tmp_path, name)
    assert target.parent == tmp_path


@pytest.mark.parametrize(
    "link", ["file:///C:/secret.pdf", "javascript:alert(1)", "http://", "ftp://x/y.pdf", "x.pdf"]
)
def test_convert_from_a_link_takes_only_web_addresses(link) -> None:
    with pytest.raises(model.ScribeError):
        check_link(link)
    assert check_link("  https://example.com/a.pdf ") == "https://example.com/a.pdf"
