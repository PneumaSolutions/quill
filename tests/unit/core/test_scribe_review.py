"""Scribe for Documents: the independent review's findings (2026-11-21), as tests.

Each test is named for the finding it pins (docs/design/scribe-security-review.md,
"Independent review"). They run QUILL's own client, channel, queue and runner
against the fake server in tests/scribe_fake_server.py, as the foundation tests do.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from quill.core.scribe import auth
from quill.core.scribe import queue as q
from quill.core.scribe.channel import DocumentChannel
from quill.core.scribe.client import ScribeClient
from quill.core.scribe.library import Library, download_folder
from quill.core.scribe.runner import QueueRunner
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


def _runner(fake, store, tmp_path, said=None, results=None, *, pid=None, **kw):
    lock = q.RunnerLock(tmp_path / "shared")
    if pid is not None:
        lock.pid = pid
    return QueueRunner(
        app="QUILL",
        jobs=q.JobQueue(tmp_path / "shared"),
        lock=lock,
        client=ScribeClient(store),
        library=Library(tmp_path / "shared"),
        channel_factory=lambda doc_id: DocumentChannel(
            doc_id, store=store, connector=fake.connector
        ),
        on_say=lambda kind, text, job: (said if said is not None else []).append((kind, text)),
        on_result=lambda job: (results if results is not None else []).append(job),
        sleep=lambda _s: None,
        **kw,
    )


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


def _files(job: q.Job) -> list[str]:
    folder = Path(job.folder)
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


# ------------------------------------------------------------------ Q1: one runner


@pytest.fixture
def fast_lock(monkeypatch):
    """A lock that goes stale in 0.6 s, beaten every 0.1 s, so a 'long' job is 2 s."""
    monkeypatch.setattr(q, "RUNNER_STALE_S", 0.6)
    monkeypatch.setattr(q, "HEARTBEAT_S", 0.1)


def test_q1_two_runners_and_a_job_longer_than_the_lock(fake, store, tmp_path, fast_lock) -> None:
    """The lock is beaten for as long as the job runs: a second app never takes it."""
    a_results, b_results = [], []
    a = _runner(fake, store, tmp_path, results=a_results)
    b = _runner(fake, store, tmp_path, results=b_results, pid=a.lock.pid + 1)
    a.jobs.add([_job(tmp_path, "docx")])
    started, go = threading.Event(), threading.Event()
    real = a.client.create_from_file

    def slow(path, password=None):
        started.set()
        go.wait(10)
        return real(path, password)

    a.client.create_from_file = slow  # type: ignore[method-assign]
    worker = threading.Thread(target=a.run_once)
    worker.start()
    assert started.wait(10)
    deadline = time.monotonic() + 2.0  # more than three times the stale age
    while time.monotonic() < deadline:
        assert b.run_once() is False
        time.sleep(0.05)
    go.set()
    worker.join(30)
    assert len(fake.uploads) == 1
    assert a_results and a_results[0].state == q.DONE and not b_results
    assert len(_files(a_results[0])) == 1


def test_q1_an_app_that_lost_the_lock_stops_before_its_next_step(
    fake, store, tmp_path, fast_lock, monkeypatch
) -> None:
    """A frozen app (no heartbeat) loses the job to another, and then touches nothing."""
    a = _runner(fake, store, tmp_path)
    b_results = []
    b = _runner(fake, store, tmp_path, results=b_results, pid=a.lock.pid + 1)
    monkeypatch.setattr(a, "_heartbeat", lambda job_id, done: None)  # frozen
    [job] = a.jobs.add([_job(tmp_path, "docx")])
    doc = fake.add_document("Notes")
    a.jobs.update(job.id, lambda j: setattr(j, "document_id", doc.id))
    real_outputs = a.client.outputs
    took_over = threading.Event()

    def outputs_then_freeze(document_id):
        found = real_outputs(document_id)
        time.sleep(0.8)  # longer than the stale age: B takes over meanwhile
        assert b.run_once() is True
        took_over.set()
        return found

    monkeypatch.setattr(a.client, "outputs", outputs_then_freeze)
    assert a.run_once() is True
    assert took_over.is_set()
    done = a.jobs.get(job.id)
    assert done.state == q.DONE and not b_results  # B ran it; A, its starter, says it
    assert fake.started == ["docx"]  # A never started the conversion it lost
    assert len(_files(done)) == 1


def test_q1_a_claimed_job_is_not_offered_to_another_app(tmp_path) -> None:
    jobs = q.JobQueue(tmp_path)
    [job] = jobs.add([_job(tmp_path, "docx")])
    assert jobs.claim(100).id == job.id
    assert jobs.next_runnable(pid=200) is None and jobs.claim(200) is None
    assert jobs.next_runnable(pid=100).id == job.id  # the claimant still sees it
    assert jobs.renew(job.id, 100) and not jobs.renew(job.id, 200)
    jobs.release(job.id, 100)
    assert jobs.claim(200).id == job.id


def test_q1_a_claim_whose_app_died_passes_on(tmp_path) -> None:
    jobs = q.JobQueue(tmp_path)
    [job] = jobs.add([_job(tmp_path, "docx")])
    jobs.claim(100)
    later = time.time() + q.RUNNER_STALE_S + 1
    assert jobs.claim(200, now=later).id == job.id


# ------------------------------------------------------------------ Q5: downloads

BODY = bytes(range(256)) * 40  # 10 KB, every byte value


def _ready(fake, fmt="docx"):
    fake.storage_override[fmt] = BODY
    return fake.add_document("Notes", outputs={fmt: "complete"})


def _download(store, doc, folder, key="job1"):
    return ScribeClient(store).download(doc.id, "docx", folder, fallback_stem="Notes", part_key=key)


def test_q5_a_stream_that_ends_early_is_resumed_not_saved_short(fake, store, tmp_path) -> None:
    doc = _ready(fake)
    fake.storage_drops = [1000, 3000]
    path = _download(store, doc, tmp_path)
    assert path.read_bytes() == BODY
    resumed = [h for h in fake.storage_requests if "Range" in h]
    assert [h["Range"] for h in resumed] == ["bytes=1000-", "bytes=4000-"]
    assert all(h.get("If-Range") for h in resumed)
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]  # no partial left


def test_q5_a_download_that_never_finishes_fails_and_keeps_its_partial(
    fake, store, tmp_path
) -> None:
    from quill.core.scribe.model import ScribeSaveFailed

    doc = _ready(fake)
    fake.storage_drops = [500] + [0] * 20  # one slice, then nothing more, ever
    with pytest.raises(ScribeSaveFailed, match="stopped before the end"):
        _download(store, doc, tmp_path)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert not any(n.endswith(".docx") for n in names)  # never renamed into place
    partial = next(p for p in tmp_path.iterdir() if p.name.endswith(".part"))
    assert partial.stat().st_size == 500
    fake.storage_drops = []
    assert _download(store, doc, tmp_path).read_bytes() == BODY  # Retry carries on


def test_q5_a_stale_partial_is_never_spliced_in(fake, store, tmp_path) -> None:
    doc = _ready(fake)
    (tmp_path / "Notes.docx.job1.part").write_bytes(b"OLD-OLD-OLD")  # no record of whose
    (tmp_path / "Notes.docx.part").write_bytes(b"OLD-OLD-OLD")  # the old naming
    assert _download(store, doc, tmp_path).read_bytes() == BODY
    assert "Range" not in fake.storage_requests[0]


def test_q5_a_partial_of_a_changed_file_comes_back_whole(fake, store, tmp_path) -> None:
    from quill.core.scribe.model import ScribeSaveFailed

    doc = _ready(fake)
    fake.storage_drops = [700] + [0] * 20
    with pytest.raises(ScribeSaveFailed):
        _download(store, doc, tmp_path)
    fake.storage_drops = []
    fake.storage_override["docx"] = b"NEW VERSION " * 500  # the document was converted again
    path = _download(store, doc, tmp_path)
    assert path.read_bytes() == b"NEW VERSION " * 500  # If-Range: the server sent it whole


def test_q5_a_complete_partial_left_by_a_crash_is_finished(fake, store, tmp_path) -> None:
    """The app stopped between the last byte and the rename: a 416 finishes it."""
    import hashlib
    import json as _json

    doc = _ready(fake)

    etag = '"' + hashlib.sha256(BODY).hexdigest()[:16] + '"'
    (tmp_path / "Notes.docx.job1.part").write_bytes(BODY)
    (tmp_path / "Notes.docx.job1.part.json").write_text(
        _json.dumps({"etag": etag, "total": len(BODY)}), encoding="utf-8"
    )
    path = _download(store, doc, tmp_path)
    assert path.name == "Notes.docx" and path.read_bytes() == BODY
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Notes.docx"]


# ------------------------------------------------------------------ Q3: bounded retries


class _Clock:
    def __init__(self) -> None:
        self.t = time.time()

    def __call__(self) -> float:
        return self.t


def test_q3_an_unexpected_error_retries_a_few_times_then_ends_with_retry(
    fake, store, tmp_path, monkeypatch
) -> None:
    said = []
    now = _Clock()
    runner = _runner(fake, store, tmp_path, said, now=now)
    [job] = runner.jobs.add([_job(tmp_path, "docx")])

    def broken(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runner.library, "record", broken)
    monkeypatch.setattr(runner.client, "outputs", lambda _d: (_ for _ in ()).throw(ValueError()))
    assert runner.run_once() is True
    job = runner.jobs.get(job.id)
    assert job.attempts == 1 and job.state in q.ACTIVE
    assert runner.run_once() is False  # not again a second later: it waits
    for _ in range(3):
        now.t = runner.jobs.get(job.id).retry_at + 1
        runner.run_once()
    job = runner.jobs.get(job.id)
    assert job.state == q.FAILED and job.error_code == "QUILL-SCRIBE-FAILED"
    assert said[-1][0] == "error" and "could not convert Notes.pdf" in said[-1][1]
    assert runner.run_once() is False  # and then it stays put until Retry


def test_q3_a_failure_after_saving_never_saves_a_second_copy(
    fake, store, tmp_path, monkeypatch
) -> None:
    """The reviewer's case: library.json unwritable after the download."""
    results = []
    runner = _runner(fake, store, tmp_path, results=results)
    [job] = runner.jobs.add([_job(tmp_path, "docx")])
    monkeypatch.setattr(
        runner.library, "record", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "full"))
    )
    assert runner.run_once() is True
    job = results[0]
    assert job.state == q.DONE
    assert _files(job) == ["Notes.docx"]


def test_q3_a_retry_after_a_later_failure_reuses_the_saved_file(
    fake, store, tmp_path, monkeypatch
) -> None:
    now = _Clock()
    results = []
    runner = _runner(fake, store, tmp_path, results=results, now=now)
    [job] = runner.jobs.add([_job(tmp_path, "docx", "brf")])
    real = runner._download
    calls = {"n": 0}

    def fail_on_brf(job, format_id):
        if format_id == "brf" and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("zip went bad")
        return real(job, format_id)

    monkeypatch.setattr(runner, "_download", fail_on_brf)
    runner.run_once()
    now.t = runner.jobs.get(job.id).retry_at + 1
    runner.run_once()
    assert results and results[0].state == q.DONE
    assert _files(results[0]) == ["Notes.brf", "Notes.docx"]
    assert fake.started == ["docx", "brf"]  # nothing converted twice either


# ------------------------------------------------------------------ Q4: sign-in resumes


def test_q4_a_job_paused_for_sign_in_runs_after_signing_in(fake, tmp_path) -> None:
    signed_out = memory_store(signed_in=False)
    runner = _runner(fake, signed_out, tmp_path)
    [job] = runner.jobs.add([_job(tmp_path, "docx")])
    runner.run_once()
    assert runner.jobs.get(job.id).state == q.NEEDS_SIGNIN
    results = []
    signed_in = _runner(fake, memory_store(), tmp_path, results=results)  # same queue
    assert signed_in.resume_after_sign_in() == 1
    assert signed_in.run_once() is True
    assert results and results[0].state == q.DONE


def test_q4_a_sign_in_in_another_app_is_noticed_while_idle(fake, tmp_path) -> None:
    runner = _runner(fake, memory_store(signed_in=False), tmp_path)
    [job] = runner.jobs.add([_job(tmp_path, "docx")])
    runner.run_once()
    runner.client.store.save(memory_store().load())  # another app signed in
    runner._next_signin_check = 0.0
    assert runner.run_once() is False
    assert runner.jobs.get(job.id).state == q.WAITING
    assert runner.run_once() is True


# ------------------------------------------------------------------ Q6: reconnects


class _ScriptedChannel:
    """A channel whose events the test chooses; ``None`` is a quiet five seconds."""

    def __init__(self, clock, events, *, busy_first=False, on_complete=None):
        self.clock, self.events, self.busy_first = clock, list(events), busy_first
        self.on_complete = on_complete
        self.starts = 0

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return None

    def start_conversion(self, fmt, password=None):
        from quill.core.scribe.model import ScribeError

        self.starts += 1
        if self.busy_first and self.starts == 1:
            raise ScribeError("busy", server_code="conversion_in_progress")
        return fmt

    def next_event(self, timeout=5.0):
        self.clock.t += timeout
        if self.clock.t > 10_000:
            raise AssertionError("still waiting after hours")
        event = self.events.pop(0) if self.events else None
        if event and event.get("type") == "conversion_complete" and self.on_complete:
            self.on_complete()
        return event


def test_q6_reconnecting_after_the_format_already_finished_does_not_hang(
    fake, store, tmp_path
) -> None:
    clock = _Clock()
    clock.t = 0.0
    doc = fake.add_document("Notes")
    complete = {"type": "conversion_complete", "format": "docx"}
    channel = _ScriptedChannel(
        clock,
        [complete],
        busy_first=True,
        on_complete=lambda: doc.outputs.__setitem__("docx", "complete"),
    )
    results = []
    runner = _runner(fake, store, tmp_path, results=results, clock=clock)
    runner._channel_factory = lambda _d: channel
    [job] = runner.jobs.add([_job(tmp_path, "docx")])
    runner.jobs.update(job.id, lambda j: setattr(j, "document_id", doc.id))
    runner.run_once()
    assert results and results[0].state == q.DONE
    assert channel.starts == 1  # downloaded, not started again


def test_q6_a_quiet_channel_falls_back_to_polling_the_outputs(fake, store, tmp_path) -> None:
    clock = _Clock()
    clock.t = 0.0
    doc = fake.add_document("Notes")
    channel = _ScriptedChannel(clock, [None, None])

    def quiet_then_done(timeout=5.0):
        event = _ScriptedChannel.next_event(channel, timeout)
        if clock.t >= 20:
            doc.outputs["docx"] = "complete"  # finished; the event never came
        return event

    channel.next_event = quiet_then_done  # type: ignore[method-assign]
    results = []
    runner = _runner(fake, store, tmp_path, results=results, clock=clock)
    runner._channel_factory = lambda _d: channel
    [job] = runner.jobs.add([_job(tmp_path, "docx")])
    runner.jobs.update(job.id, lambda j: setattr(j, "document_id", doc.id))
    runner.run_once()
    assert results and results[0].state == q.DONE
    assert clock.t <= 70  # found within about one poll


def test_q6_phx_close_counts_as_a_dropped_channel() -> None:
    from quill.core.scribe.model import ScribeOffline

    channel = DocumentChannel("abc", store=memory_store(), connector=lambda _u: None)
    with pytest.raises(ScribeOffline):
        channel._as_event(["1", None, "documents:abc", "phx_close", {}])
    assert channel._as_event(["1", None, "documents:other", "phx_close", {}]) is None


# ------------------------------------------------------------------ Q7: error events


@pytest.mark.parametrize(
    ("event", "state", "code"),
    [
        (
            {
                "reason": "needs_purchase",
                "message": "You are out of pages.",
                "purchase_url": "https://scribeit.io/documents/x/docx/pricing_options",
            },
            q.NEEDS_PAGES,
            "QUILL-SCRIBE-PAGES",
        ),
        ({"reason": "not_found", "message": "Gone."}, q.GONE, "QUILL-SCRIBE-GONE"),
        ({"reason": "unauthenticated", "message": ""}, q.NEEDS_SIGNIN, "QUILL-SCRIBE-SIGNIN"),
        ({"reason": "server_error", "message": "Broke."}, q.FAILED, "QUILL-SCRIBE-FAILED"),
    ],
)
def test_q7_each_error_event_reaches_its_row_of_section_11(
    fake, store, tmp_path, event, state, code
) -> None:
    fake.scripts["docx"] = [("status", "convert", 0.2), ("error_event", event)]
    runner = _runner(fake, store, tmp_path)
    [job] = runner.jobs.add([_job(tmp_path, "docx")])
    runner.run_once()
    job = runner.jobs.get(job.id)
    assert (job.state, job.error_code) == (state, code)
    if state == q.NEEDS_PAGES:
        assert job.purchase_url.endswith("/pricing_options")


def test_q7_a_purchase_link_that_is_not_scribes_is_replaced(fake, store, tmp_path) -> None:
    fake.scripts["docx"] = [
        ("error_event", {"reason": "needs_purchase", "purchase_url": "file:///C:/x.hta"})
    ]
    runner = _runner(fake, store, tmp_path)
    [job] = runner.jobs.add([_job(tmp_path, "docx")])
    runner.run_once()
    assert runner.jobs.get(job.id).purchase_url == runner.client.purchase_url()


def test_q7_rate_limited_as_an_event_waits_longer_each_time_and_is_said_once(
    fake, store, tmp_path
) -> None:
    said, results, waits = [], [], []
    limited = ("error_event", {"reason": "rate_limited", "message": "Slow down."})
    fake.scripts["docx"] = [limited]
    runner = _runner(fake, store, tmp_path, said, results)
    runner._sleep = waits.append

    def next_script() -> None:
        if len(waits) == 1:
            fake.scripts["docx"] = [limited]
        elif len(waits) == 2:
            fake.scripts.pop("docx")

    real_sleep = runner._sleep
    runner._sleep = lambda s: (real_sleep(s), next_script())
    runner.jobs.add([_job(tmp_path, "docx")])
    runner.run_once()
    assert results and results[0].state == q.DONE
    assert waits == [60.0, 120.0]
    assert [t for k, t in said if "slow down" in t] == [
        "Scribe for Documents asked QUILL to slow down."
    ]


def test_q7_conversion_in_progress_as_an_event_is_never_an_error(fake, store, tmp_path) -> None:
    results = []
    fake.scripts["docx"] = [
        ("error_event", {"reason": "conversion_in_progress", "message": "Busy."}),
        ("status", "complete", 1.0),
    ]
    runner = _runner(fake, store, tmp_path, results=results)
    runner.jobs.add([_job(tmp_path, "docx")])
    runner.run_once()
    assert results and results[0].state == q.DONE


# ------------------------------------------------------------------ Q9: the batch gate


def test_q9_a_password_does_not_skip_send_next_file(fake, store, tmp_path) -> None:
    runner = _runner(fake, store, tmp_path)
    first, second = runner.jobs.add([
        _job(tmp_path, "docx", name="a.pdf"),
        _job(tmp_path, "docx", name="b.pdf"),
    ])
    runner.give_password(second.id, "pw")
    assert runner.jobs.get(second.id).state == q.WAITING_YOU
    runner.jobs.update(second.id, lambda j: setattr(j, "state", q.NEEDS_PASSWORD))
    runner.give_password(second.id, "pw")
    assert runner.jobs.get(second.id).state == q.WAITING


# ------------------------------------------------------------------ Q23: titles with dots


@pytest.mark.parametrize(
    ("title", "folder"),
    [
        ("1. Introduction", "1. Introduction"),
        ("Dr. Smith's notes", "Dr. Smith's notes"),
        ("Report v2.5 draft", "Report v2.5 draft"),
        ("Notes.pdf", "Notes"),
        ("CON", "_CON"),
    ],
)
def test_q23_a_title_with_a_full_stop_keeps_its_folder(title, folder) -> None:
    assert download_folder(title, "/x").name == folder


def test_q23_a_long_name_keeps_its_extension_and_fits_in_bytes() -> None:
    from quill.core.scribe.client import safe_filename

    long = safe_filename("Report v1.2 " + "A" * 300 + ".docx")
    assert long.endswith(".docx") and len(long.encode()) <= 200
    arabic = safe_filename("تقرير " * 60 + ".pdf")
    assert arabic.endswith(".pdf") and len(arabic.encode()) <= 200


# ------------------------------------------------------------------ Q11: an unreadable queue


def test_q11_an_unreadable_queue_is_never_written_over(tmp_path) -> None:
    jobs = q.JobQueue(tmp_path)
    jobs.path.write_text('{"schema": 1, "jobs": [ {"id": "a", ', encoding="utf-8")  # damaged
    before = jobs.path.read_bytes()
    with pytest.raises(q.QueueUnreadable):
        jobs.add([_job(tmp_path, "docx")])
    with pytest.raises(q.QueueUnreadable):
        jobs.update("a", lambda j: None)
    with pytest.raises(q.QueueUnreadable):
        jobs.remove("a")
    assert jobs.path.read_bytes() == before
    assert jobs.jobs() == [] and jobs.claim(1) is None  # readers see nothing, safely


def test_q11_a_newer_queue_is_read_but_not_changed(tmp_path) -> None:
    jobs = q.JobQueue(tmp_path)
    jobs.path.write_text(
        '{"schema": 2, "jobs": [{"id": "a", "title": "Notes.pdf", "formats": ["docx"]}]}',
        encoding="utf-8",
    )
    assert [j.id for j in jobs.jobs()] == ["a"]
    with pytest.raises(q.QueueUnreadable, match="newer QUILL"):
        jobs.update("a", lambda j: setattr(j, "state", q.CANCELLED))


def test_q11_unknown_fields_and_rows_survive_a_rewrite(tmp_path) -> None:
    import json as _json

    jobs = q.JobQueue(tmp_path)
    jobs.path.write_text(
        _json.dumps({
            "schema": 1,
            "jobs": [
                {"id": "a", "title": "Notes.pdf", "formats": ["docx"], "future": {"x": 1}},
                {"id": "b", "formats": 7},  # a row this version cannot read
                "not even a row",
            ],
        }),
        encoding="utf-8",
    )
    jobs.update("a", lambda j: setattr(j, "message", "hello"))
    rows = _json.loads(jobs.path.read_text(encoding="utf-8"))["jobs"]
    assert rows[0]["future"] == {"x": 1} and rows[0]["message"] == "hello"
    assert rows[1] == {"id": "b", "formats": 7} and rows[2] == "not even a row"


def test_q11_a_locked_queue_file_is_read_again_before_giving_up(tmp_path, monkeypatch) -> None:
    jobs = q.JobQueue(tmp_path)
    [job] = jobs.add([_job(tmp_path, "docx")])
    real = Path.read_text
    calls = {"n": 0}

    def flaky(self, *a, **k):
        if self == jobs.path and calls["n"] == 0:
            calls["n"] += 1
            raise PermissionError(13, "sharing violation")  # antivirus had it open
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky)
    assert jobs.update(job.id, lambda j: setattr(j, "message", "ok")).message == "ok"
    assert len(jobs.jobs()) == 1


def test_q11_an_unreadable_library_is_never_written_over(tmp_path) -> None:
    library = Library(tmp_path)
    library.path.write_text("{damaged", encoding="utf-8")
    with pytest.raises(OSError):
        library.record("doc", "Notes", ["x.docx"])
    assert library.path.read_text(encoding="utf-8") == "{damaged"


# ------------------------------------------------------------------ Q8: the sign-in listener


def test_q8_an_idle_local_connection_never_freezes_the_sign_in_wait() -> None:
    import socket
    import urllib.request

    from quill.core.scribe.model import ScribeCancelled

    catcher = auth.LoopbackCatcher(state="s1")
    catcher.start()
    port = int(catcher.redirect_uri.split(":")[2].split("/")[0])
    idle = socket.create_connection(("127.0.0.1", port))  # connects, sends nothing
    try:
        started = time.monotonic()
        with pytest.raises(ScribeCancelled):
            catcher.wait(cancel_requested=lambda: time.monotonic() - started > 0.5, timeout=60)
        assert time.monotonic() - started < 2.0  # Cancel still answers
        # ...and the real redirect is not queued behind the idle connection.
        got: dict[str, str] = {}

        def wait() -> None:
            got["code"] = catcher.wait(cancel_requested=None, timeout=10)

        waiter = threading.Thread(target=wait)
        waiter.start()
        time.sleep(0.3)
        urllib.request.urlopen(f"{catcher.redirect_uri}?code=abc&state=s1", timeout=5).read()
        waiter.join(5)
        assert got == {"code": "abc"}
    finally:
        idle.close()
        catcher.close()


# ------------------------------------------------------------------ Q16: the AI-off note


def test_q16_the_ai_off_reason_is_in_the_field_tab_reaches() -> None:
    from quill.core.scribe.consent import AI_OFF_NOTE, SourceInfo, consent_text

    source = SourceInfo(path=Path("Notes.pdf"), size=10)
    assert AI_OFF_NOTE in consent_text([source], ai_off=True)
    assert AI_OFF_NOTE not in consent_text([source])
    assert AI_OFF_NOTE in consent_text([], reuse_title="Notes", ai_off=True)


# ------------------------------------------------------------------ Q14: who says the result


def _dead_pid() -> int:
    import subprocess
    import sys

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def test_q14_a_job_started_in_an_open_app_is_said_there_not_as_closed(
    fake, store, tmp_path
) -> None:
    starter_results, runner_results = [], []
    starter = _runner(fake, store, tmp_path, results=starter_results)  # this process
    other = _runner(fake, store, tmp_path, results=runner_results, pid=starter.lock.pid + 1)
    starter.jobs.add([_job(tmp_path, "docx")])  # owner_pid: this (live) process
    assert other.run_once() is True
    assert runner_results == []  # the app that ran it does not say it...
    starter._deliver_results()
    assert len(starter_results) == 1 and starter_results[0].delivered  # ...its starter does
    starter._deliver_results()
    assert len(starter_results) == 1  # once


def test_q14_a_job_whose_app_closed_is_said_by_the_runner(fake, store, tmp_path) -> None:
    results = []
    runner = _runner(fake, store, tmp_path, results=results)
    runner.jobs.add([_job(tmp_path, "docx", owner_pid=_dead_pid())])
    runner.run_once()
    assert len(results) == 1 and results[0].resumed


def test_q14_pid_alive() -> None:
    import os

    assert q.pid_alive(os.getpid()) and not q.pid_alive(_dead_pid()) and not q.pid_alive(0)


# ------------------------------------------------------------------ Q18: large files


def test_q18_the_upload_is_streamed_from_disk_with_its_exact_length(tmp_path) -> None:
    from quill.core.scribe import http as scribe_http

    big = tmp_path / "Big.pdf"
    big.write_bytes(bytes(range(256)) * 4096)  # 1 MB
    upload = scribe_http.MultipartFile({"document[password]": "pw"}, "document[file]", big)
    stream = upload.open()
    pieces = []
    while chunk := stream.read(65536):
        pieces.append(chunk)
    stream.close()
    body = b"".join(pieces)
    assert len(body) == upload.length
    assert max(len(p) for p in pieces) <= 1024 * 1024  # never the whole file in one read
    boundary = upload.content_type.split("boundary=")[1]
    assert body.startswith(f"--{boundary}".encode()) and body.endswith(
        f"--{boundary}--\r\n".encode()
    )
    assert big.read_bytes() in body and b'name="document[password]"\r\n\r\npw' in body


def test_q18_the_fake_server_reads_a_streamed_upload(fake, store, tmp_path) -> None:
    source = tmp_path / "Notes.pdf"
    source.write_bytes(b"%PDF-1.4 " + b"x" * 300_000)
    document_id = ScribeClient(store).create_from_file(source, "pw")
    assert document_id and fake.uploads[-1] == {"title": "Notes.pdf", "password": "pw", "url": None}


def test_q18_a_large_pdf_is_described_without_reading_it_whole(tmp_path, monkeypatch) -> None:
    from quill.core.scribe import consent

    monkeypatch.setattr(consent, "_WHOLE_READ_BYTES", 1000)
    monkeypatch.setattr(consent, "_EDGE_BYTES", 100)
    pdf = tmp_path / "Scan.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"0" * 5000 + b"\ntrailer << /Encrypt 5 0 R >>\n")
    reads = []
    real_open = open

    def counting_open(path, mode="r", *a, **k):
        handle = real_open(path, mode, *a, **k)
        if str(path) == str(pdf):
            real_read = handle.read

            def handle_read(n=-1):
                data = real_read(n)
                reads.append(len(data))
                return data

            return _Wrapped(handle, handle_read)
        return handle

    monkeypatch.setattr("builtins.open", counting_open)
    info = consent.describe_source(pdf)
    assert info.encrypted is True
    assert sum(reads) <= 200  # only the two 100-byte edges, not the 5 KB file


class _Wrapped:
    def __init__(self, handle, read):
        self._h, self.read = handle, read

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._h.close()

    def seek(self, *a):
        return self._h.seek(*a)


def test_q18_many_scanned_pages_become_one_pdf_a_page_at_a_time(tmp_path) -> None:
    pytest.importorskip("PIL")
    pytest.importorskip("pypdf")
    from PIL import Image
    from pypdf import PdfReader

    from quill.core.scanning.session import images_to_pdf

    pages = []
    for number in range(3):
        path = tmp_path / f"p{number}.png"
        Image.new("L", (40, 60), color=number * 60).save(path)
        pages.append(path)
    target = images_to_pdf(pages, tmp_path / "out" / "Scan.pdf", dpi=100)
    assert len(PdfReader(str(target)).pages) == 3
    assert not (tmp_path / "out" / "Scan.pdf.part").exists()


# ------------------------------------------------------------------ Q24: passwords


def test_q24_a_handed_over_password_expires_unread(store, monkeypatch) -> None:
    from quill.core.scribe import runner_parts

    runner_parts.hand_over_password(store, "job1", "hunter2")
    assert runner_parts.take_handed_password(store, "job1") == "hunter2"
    assert runner_parts.take_handed_password(store, "job1") is None  # read once
    runner_parts.hand_over_password(store, "job2", "hunter2")
    later = time.time() + runner_parts.HANDOVER_TTL_S + 1
    monkeypatch.setattr(runner_parts.time, "time", lambda: later)
    assert runner_parts.take_handed_password(store, "job2") is None  # too old: never used
    assert store.manager.get(runner_parts._handover_ref("job2")) is None  # and wiped


def test_q24_a_closing_app_wipes_the_passwords_it_left_uncollected(
    fake, store, tmp_path, caplog
) -> None:
    import logging

    from quill.core.scribe import runner_parts

    caplog.set_level(logging.DEBUG)
    holder = q.RunnerLock(tmp_path / "shared")
    holder.pid = 999_999
    holder.acquire()  # another app runs the queue
    giver = _runner(fake, store, tmp_path)
    [job] = giver.jobs.add([_job(tmp_path, "docx", state=q.NEEDS_PASSWORD)])
    giver.give_password(job.id, "s3cret-pw")
    assert store.manager.get(runner_parts._handover_ref(job.id)) is not None
    giver.stop()
    assert store.manager.get(runner_parts._handover_ref(job.id)) is None
    assert "s3cret-pw" not in caplog.text
    assert "s3cret-pw" not in (tmp_path / "shared" / "queue.json").read_text(encoding="utf-8")


# ------------------------------------------------------------------ Q25: offline


def test_q25_an_interrupted_upload_resumes_without_a_second_copy(
    fake, store, tmp_path, monkeypatch
) -> None:
    from quill.core.scribe.model import ScribeOffline

    said, results = [], []
    runner = _runner(fake, store, tmp_path, said, results)
    runner.jobs.add([_job(tmp_path, "docx")])
    real = runner.client.create_from_file
    calls = {"n": 0}

    def reached_scribe_then_dropped(path, password=None):
        calls["n"] += 1
        if calls["n"] == 1:
            real(path, password)  # Scribe made the document...
            raise ScribeOffline("lost", server_code="offline")  # ...the answer was lost
        return real(path, password)

    monkeypatch.setattr(runner.client, "create_from_file", reached_scribe_then_dropped)
    runner.run_once()
    assert results and results[0].state == q.DONE
    assert len(fake.uploads) == 1  # adopted, not sent (or charged) again


def test_q25_an_upload_offline_for_a_while_says_so_once_and_resumes(
    fake, store, tmp_path, monkeypatch
) -> None:
    from quill.core.scribe.model import ScribeOffline

    said, results = [], []
    runner = _runner(fake, store, tmp_path, said, results)
    runner.jobs.add([_job(tmp_path, "docx")])
    real = runner.client.create_from_file
    calls = {"n": 0}

    def offline_four_times(path, password=None):
        calls["n"] += 1
        if calls["n"] <= 4:
            raise ScribeOffline("no network", server_code="offline")
        return real(path, password)

    monkeypatch.setattr(runner.client, "create_from_file", offline_four_times)
    runner.run_once()
    assert results and results[0].state == q.DONE and len(fake.uploads) == 1
    assert [t for k, t in said if k == "offline"] == [
        "Upload of Notes.pdf was interrupted; QUILL will resume it."
    ]


def test_q25_a_long_outage_keeps_reconnecting_well_past_eight_tries(fake, store, tmp_path) -> None:
    from quill.core.scribe.model import ScribeOffline

    clock = _Clock()
    clock.t = 0.0
    results, waits = [], []
    runner = _runner(fake, store, tmp_path, results=results, clock=clock)
    runner._sleep = lambda s: (waits.append(s), setattr(clock, "t", clock.t + s))
    real_factory = runner._channel_factory
    drops = {"n": 0}

    def factory(doc_id):
        if drops["n"] < 30:
            drops["n"] += 1
            raise ScribeOffline("no network", server_code="offline")
        return real_factory(doc_id)

    runner._channel_factory = factory
    runner.jobs.add([_job(tmp_path, "docx")])
    runner.run_once()
    assert results and results[0].state == q.DONE
    assert max(waits) == 60.0 and clock.t > 20 * 60  # twenty minutes offline, then done


def test_q25_an_outage_longer_than_two_hours_ends_the_job_with_retry(fake, store, tmp_path) -> None:
    from quill.core.scribe.model import ScribeOffline

    clock = _Clock()
    clock.t = 0.0
    runner = _runner(fake, store, tmp_path, clock=clock)
    runner._sleep = lambda s: setattr(clock, "t", clock.t + s)

    def never(_doc_id):
        raise ScribeOffline("no network", server_code="offline")

    runner._channel_factory = never
    [job] = runner.jobs.add([_job(tmp_path, "docx")])
    runner.run_once()
    job = runner.jobs.get(job.id)
    assert job.state == q.FAILED and job.error_code == "QUILL-SCRIBE-OFFLINE"
    assert clock.t >= 2 * 60 * 60


# ------------------------------------------------------------------ Q33: Open Preview


def test_q33_open_preview_opens_the_documents_own_page_signed_in(fake, store) -> None:
    url = ScribeClient(store).preview_url("doc 1")
    assert "/auth/browser_from_oauth?" in url and "documents%2Fdoc%25201" in url


# ------------------------------------------------------------------ the low findings


def test_q26_a_result_always_gets_its_formats_own_extension(tmp_path) -> None:
    client = ScribeClient(memory_store())
    assert client._target_name("Notes.hta", "Notes", "docx") == "Notes.docx"
    assert client._target_name("Notes.DOCX", "Notes", "docx") == "Notes.DOCX"
    assert client._target_name("", "Notes", "brf") == "Notes.brf"


def test_q29_a_refresh_token_another_app_already_used_is_not_a_sign_out(fake) -> None:
    """Two apps refresh at once; if Scribe rotated the token, the loser reads the
    vault again instead of signing the person out."""
    shared = memory_store()
    shared.save(auth.StoredTokens("old", "spent-refresh", 0))  # expired: must refresh
    real_load = shared.load
    calls = {"n": 0}

    def load_then_rotated():
        calls["n"] += 1
        if calls["n"] == 1:
            return real_load()  # what this app saw before refreshing
        return auth.StoredTokens("access-1", "refresh-1", 4_000_000_000)  # the other app's

    shared.load = load_then_rotated  # type: ignore[method-assign]
    assert shared.access_token() == "access-1"


def test_q30_only_a_401_or_403_handshake_is_a_sign_out(monkeypatch) -> None:
    import types

    from quill.core.scribe import channel
    from quill.core.scribe.model import ScribeOffline, ScribeSignInRequired

    class Refused(Exception):
        def __init__(self, status: int, text: str) -> None:
            super().__init__(text)
            self.response = types.SimpleNamespace(status_code=status)

    def fake_connect(status, text):
        def connect(*_a, **_k):
            raise Refused(status, text)

        return connect

    module = types.ModuleType("websockets.sync.client")
    monkeypatch.setitem(__import__("sys").modules, "websockets.sync.client", module)
    module.connect = fake_connect(500, "server said 401 somewhere in its text")
    with pytest.raises(ScribeOffline):
        channel.connect("wss://scribeit.io/socket/websocket")
    module.connect = fake_connect(403, "Forbidden")
    with pytest.raises(ScribeSignInRequired):
        channel.connect("wss://scribeit.io/socket/websocket")


def test_q31_the_private_word_copy_is_removed_when_the_job_is_done(fake, store, tmp_path) -> None:
    import tempfile

    folder = Path(tempfile.mkdtemp(prefix="quill-scribe-"))
    copy = folder / "Untitled.docx"
    copy.write_bytes(b"PK fake docx")
    results = []
    runner = _runner(fake, store, tmp_path, results=results)
    runner.jobs.add([
        q.Job(
            title="Untitled.docx",
            source_path=str(copy),
            formats=["docx"],
            folder=str(tmp_path / "out" / "Untitled"),
        )
    ])
    runner.run_once()
    assert results and results[0].state == q.DONE
    assert not folder.exists()


def test_q43_links_are_named_by_their_file_and_lose_their_token(fake, store, tmp_path) -> None:
    assert q.link_name("https://x.org/files/My%20Notes.pdf?token=abc") == "My Notes.pdf"
    assert q.link_name("https://x.org/") == "x.org"
    assert q.scrub_link("https://x.org/a.pdf?token=abc#p2") == "https://x.org/a.pdf"
    results = []
    runner = _runner(fake, store, tmp_path, results=results)
    link = "https://x.org/files/Notes.pdf?token=secret"
    runner.jobs.add([q.Job(title=link, source_url=link, formats=["docx"], folder=str(tmp_path))])
    runner.run_once()
    text = (tmp_path / "shared" / "queue.json").read_text(encoding="utf-8")
    assert "secret" not in text and results[0].name == "Notes.pdf"


def test_q43_finished_jobs_are_pruned_after_thirty_days(tmp_path) -> None:
    jobs = q.JobQueue(tmp_path)
    [old] = jobs.add([_job(tmp_path, "docx", state=q.DONE)])
    jobs.update(old.id, lambda j: None)
    import json as _json

    data = _json.loads(jobs.path.read_text(encoding="utf-8"))
    data["jobs"][0]["updated"] = time.time() - q.PRUNE_AFTER_S - 10
    jobs.path.write_text(_json.dumps(data), encoding="utf-8")
    [new] = jobs.add([_job(tmp_path, "docx")])
    assert [j.id for j in jobs.jobs()] == [new.id]


def test_q46_sign_out_always_wipes(fake, store, monkeypatch) -> None:
    from quill.core.scribe import http as scribe_http

    def broken(*_a, **_k):
        raise ValueError("something unexpected in the HTTP stack")

    monkeypatch.setattr(scribe_http, "request", broken)
    assert auth.sign_out(store) is True
    assert store.load() is None
