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
