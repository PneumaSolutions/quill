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
    assert b_results and b_results[0].state == q.DONE
    assert fake.started == ["docx"]  # A never started the conversion it lost
    assert len(_files(b_results[0])) == 1


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
