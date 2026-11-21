"""The family's one queue of Scribe for Documents jobs (wx-free; PRD 7.4).

One queue per Windows user, shared by every running QUILL app: a JSON file
(``queue.json``) in the shared QUILL data folder, rewritten atomically inside
a lock file (:func:`quill.core.file_lock.guarded_write`), so two apps never
lose each other's changes. Each document is one job and each format one step
within it, because Scribe runs one conversion per document.

**One runner.** Only the app holding ``runner.lock`` (a heartbeat file; stale
after :data:`RUNNER_STALE_S`) uploads, follows and downloads -- so two apps
never upload at the same moment -- and only it speaks (PRD 7.4a). Every
other app reads the same file and shows the same state silently. When the
holder closes, the next app to look takes the lock and carries on: closing an
app never loses a job.

The holder beats the lock every :data:`HEARTBEAT_S` for as long as a job runs
(``runner.py`` beats it from its own thread), and each job is **claimed**:
:meth:`JobQueue.claim` marks it with the runner's pid and a lease, under the
strict lock, so a second app can never take a job another live app is running.
Before the 2026-11-21 independent review (Q1) the lock was beaten only while
the queue was idle, so any job longer than twenty seconds let a second app
upload it again.

Nothing secret is ever written here: no tokens, no passwords. A PDF password
lives only in the memory of the app that is running the job.

**A file it cannot read is never overwritten.** Only a missing file is an empty
queue. A file that is locked by antivirus or a backup tool, damaged, or written
by a newer QUILL (a higher ``schema``) makes every write refuse with
:class:`QueueUnreadable` after a few quick retries, and fields or rows this
version does not know are written back as they were (independent review Q11:
one bad read used to erase every job).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from quill.core.file_lock import guarded_write
from quill.core.scribe.model import ScribeError, format_label, in_build_order, labels_sentence

QUEUE_FILE = "queue.json"
RUNNER_LOCK_FILE = "runner.lock"
RUNNER_STALE_S = 20.0
#: How often the runner beats the lock and renews its job's lease.
HEARTBEAT_S = 5.0
#: How long a lock or claim write may wait for the queue's lock file.
_CLAIM_WAIT_S = 1.0
_SCHEMA = 1
_READ_TRIES = 3
_READ_RETRY_S = 0.05

# Job states. "Waiting for you" states need the person; nothing pops up for them.
WAITING = "waiting"
WAITING_YOU = "waiting_you"  # batch: Send Next File (PRD 7.4, "Batch exception")
UPLOADING = "uploading"
CONVERTING = "converting"
DOWNLOADING = "downloading"
DONE = "done"
FAILED = "failed"
NEEDS_PASSWORD = "needs_password"
NEEDS_SIGNIN = "needs_signin"
NEEDS_PAGES = "needs_pages"
SLOWED = "slowed"  # rate_limited: a silent countdown
GONE = "gone"
CANCELLED = "cancelled"

ACTIVE = frozenset({UPLOADING, CONVERTING, DOWNLOADING, SLOWED})
NEEDS_YOU = frozenset({WAITING_YOU, NEEDS_PASSWORD, NEEDS_SIGNIN, NEEDS_PAGES})
FINISHED = frozenset({DONE, FAILED, GONE, CANCELLED})

STATE_WORDS: dict[str, str] = {
    WAITING: "waiting",
    WAITING_YOU: "waiting for you: Send Next File",
    UPLOADING: "uploading",
    CONVERTING: "converting",
    DOWNLOADING: "downloading",
    DONE: "done",
    FAILED: "did not finish",
    NEEDS_PASSWORD: "needs password",
    NEEDS_SIGNIN: "needs you to sign in",
    NEEDS_PAGES: "needs more pages",
    SLOWED: "waiting: Scribe asked QUILL to slow down",
    GONE: "no longer in your Scribe account",
    CANCELLED: "stopped waiting",
}


def shared_folder() -> Path:
    """``<QUILL data>/scribe``: QUILL's data folder, which the family shares."""
    from quill.core.paths import app_data_dir

    return app_data_dir() / "scribe"


@dataclass(slots=True)
class Job:
    """One document and the formats wanted from it."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    app: str = "QUILL"  # the app that started it (results open only there)
    owner_pid: int = field(default_factory=os.getpid)
    title: str = ""
    source_path: str = ""
    source_url: str = ""
    document_id: str = ""  # set once uploaded: a retry never uploads again
    formats: list[str] = field(default_factory=list)
    done_formats: list[str] = field(default_factory=list)
    settings: dict[str, Any] | None = None  # per-document settings to send
    save_account_defaults: bool = False
    folder: str = ""  # where results go
    state: str = WAITING
    stage: str = ""
    current_format: str = ""
    percent: int = 0  # of the whole job, not of one format
    eta_s: float | None = None
    message: str = ""  # the latest full message, for the details field
    error_code: str = ""  # Copy Details only; never spoken
    purchase_url: str = ""
    results: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    resumed: bool = False  # picked up by an app other than the one that started it
    purpose: str = ""  # "import": open as a new tab; "braille": the braille view
    password_tries: int = 0
    runner_pid: int = 0  # the app running it now (0: nobody); see JobQueue.claim
    lease: float = 0.0  # runner_pid's claim lasts until this time
    attempts: int = 0  # unexpected failures so far (the runner gives up after a few)
    retry_at: float = 0.0  # not run again before this time (after an unexpected failure)
    busy_count: int = 0  # times Scribe asked to slow down (each wait is longer)
    saved: dict[str, str] = field(default_factory=dict)  # format -> file already saved
    extra: dict[str, Any] = field(default_factory=dict, repr=False)  # a newer version's fields

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Job:
        names = {f.name for f in fields(cls)} - {"extra"}
        job = cls(**{k: v for k, v in data.items() if k in names})
        job.extra = {k: v for k, v in data.items() if k not in names and k != "extra"}
        job.formats = in_build_order(job.formats)
        return job

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        extra = data.pop("extra", None) or {}
        return {**extra, **data}

    @property
    def name(self) -> str:
        if self.source_path:
            return Path(self.source_path).name
        return self.title or self.source_url or "document"

    @property
    def remaining(self) -> list[str]:
        return [f for f in self.formats if f not in self.done_formats]

    def stage_text(self) -> str:
        """Where the job is now, in words: the Stage column."""
        if self.state == CONVERTING and self.stage:
            from quill.core.scribe.model import stage_words

            return f"{format_label(self.current_format)} {stage_words(self.stage)}"
        return STATE_WORDS.get(self.state, self.state)

    def row_text(self) -> str:
        """The list row: changes only when the stage changes (PRD 7.4)."""
        return f"{self.name}: {self.stage_text()} ({labels_sentence(self.formats)})"

    def details(self) -> str:
        """The details field: percent, time left and the latest full message."""
        parts = [f"{self.percent} percent"]
        if self.eta_s:
            minutes = max(1, round(self.eta_s / 60))
            parts.append(f"about {minutes} minute{'s' if minutes != 1 else ''} left")
        if self.results:
            parts.append("Saved: " + "; ".join(self.results))
        if self.message:
            parts.append(self.message)
        return ". ".join(parts) + "."


class QueueUnreadable(ScribeError):
    """``queue.json`` is there but cannot be read: nothing is written over it."""

    code = "QUILL-SCRIBE-QUEUE"


class JobQueue:
    """The shared ``queue.json``. Every change is a locked read-modify-write."""

    def __init__(self, folder: Path | None = None) -> None:
        self._folder = folder

    @property
    def folder(self) -> Path:
        return self._folder or shared_folder()

    @property
    def path(self) -> Path:
        return self.folder / QUEUE_FILE

    def _load(self, *, strict: bool) -> list[Job | dict[str, Any]]:
        """Every row: a :class:`Job`, or the raw row when this version cannot read it.

        ``strict`` (for a write) raises :class:`QueueUnreadable` rather than
        answering "empty" for a file that is there but unreadable or newer.
        """
        data: Any = None
        for attempt in range(_READ_TRIES):
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                break
            except FileNotFoundError:
                return []
            except (OSError, ValueError):
                if attempt + 1 < _READ_TRIES:
                    time.sleep(_READ_RETRY_S)
        else:
            return self._unreadable(strict, "could not be read")
        rows = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return self._unreadable(strict, "is not a queue QUILL understands")
        schema = data.get("schema", _SCHEMA)
        if strict and isinstance(schema, int) and schema > _SCHEMA:
            raise QueueUnreadable(
                "A newer QUILL wrote the Scribe for Documents queue. Use that version, "
                "or update this one, to change it."
            )
        entries: list[Job | dict[str, Any]] = []
        for row in rows:
            try:
                entries.append(Job.from_json(row) if isinstance(row, dict) else row)
            except (TypeError, ValueError):
                entries.append(row)  # kept, untouched, for the version that wrote it
        return entries

    def _unreadable(self, strict: bool, why: str) -> list[Job | dict[str, Any]]:
        if strict:
            raise QueueUnreadable(
                f"The Scribe for Documents queue ({self.path}) {why}, so QUILL left it as "
                "it is. Try again in a moment."
            )
        return []

    def _read(self) -> list[Job]:
        return [e for e in self._load(strict=False) if isinstance(e, Job)]

    def _read_for_write(self) -> tuple[list[Job | dict[str, Any]], list[Job]]:
        entries = self._load(strict=True)
        return entries, [e for e in entries if isinstance(e, Job)]

    def _write(self, entries: Iterable[Job | dict[str, Any]]) -> None:
        from quill.core.storage import write_json_atomic

        rows = [e.to_json() if isinstance(e, Job) else e for e in entries]
        write_json_atomic(self.path, {"schema": _SCHEMA, "jobs": rows})

    def jobs(self) -> list[Job]:
        return self._read()

    def get(self, job_id: str) -> Job | None:
        return next((j for j in self._read() if j.id == job_id), None)

    def mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    def add(self, jobs: list[Job], *, batch_allowed: bool = False) -> list[Job]:
        """Queue documents. Without the batch exception, only the first goes on
        its own; the rest wait for Send Next File (PRD 7.4)."""
        self.folder.mkdir(parents=True, exist_ok=True)
        with guarded_write(self.path):
            existing, _known = self._read_for_write()
            for index, job in enumerate(jobs):
                job.formats = in_build_order(job.formats)
                if not batch_allowed and index > 0:
                    job.state = WAITING_YOU
                    job.message = "Waiting for you. Use Send Next File to send it."
            self._write([*existing, *jobs])
        return jobs

    def update(self, job_id: str, change: Callable[[Job], None]) -> Job | None:
        with guarded_write(self.path):
            entries, jobs = self._read_for_write()
            found = None
            for job in jobs:
                if job.id == job_id:
                    change(job)
                    job.updated = time.time()
                    found = job
            if found is not None:
                self._write(entries)
        return found

    def remove(self, job_id: str) -> None:
        with guarded_write(self.path):
            entries, _jobs = self._read_for_write()
            self._write([e for e in entries if not (isinstance(e, Job) and e.id == job_id)])

    def next_runnable(self, *, pid: int = 0, now: float | None = None) -> Job | None:
        """The oldest job ready to run (a waiting one, or one an app left half done).

        A job another live app has claimed (``runner_pid`` with a lease still
        running) is not offered.
        """
        moment = time.time() if now is None else now
        candidates = [j for j in self._read() if _runnable(j, pid, moment)]
        return min(candidates, key=lambda j: j.created) if candidates else None

    def claim(self, pid: int, *, now: float | None = None) -> Job | None:
        """Take the next runnable job for ``pid``, atomically across processes.

        Under the strict lock: a lock that cannot be had claims nothing, rather
        than risking two apps running one job.
        """
        try:
            with guarded_write(self.path, timeout_s=_CLAIM_WAIT_S, strict=True):
                moment = time.time() if now is None else now
                entries, jobs = self._read_for_write()
                candidates = [j for j in jobs if _runnable(j, pid, moment)]
                if not candidates:
                    return None
                job = min(candidates, key=lambda j: j.created)
                job.runner_pid = pid
                job.lease = moment + RUNNER_STALE_S
                self._write(entries)
                return job
        except (TimeoutError, QueueUnreadable):
            return None

    def renew(self, job_id: str, pid: int) -> bool:
        """Extend ``pid``'s claim on a job. False when another app has it now."""
        try:
            with guarded_write(self.path, timeout_s=_CLAIM_WAIT_S, strict=True):
                entries, jobs = self._read_for_write()
                job = next((j for j in jobs if j.id == job_id), None)
                if job is None:
                    return True  # removed: the runner finds out on its next update
                if job.runner_pid not in (0, pid) and job.lease > time.time():
                    return False
                job.runner_pid = pid
                job.lease = time.time() + RUNNER_STALE_S
                self._write(entries)
                return True
        except (TimeoutError, QueueUnreadable):
            return False

    def release(self, job_id: str, pid: int) -> None:
        """Let go of ``pid``'s claim (the job finished, failed or now needs the person)."""

        def _drop(job: Job) -> None:
            if job.runner_pid == pid:
                job.runner_pid = 0
                job.lease = 0.0

        self.update(job_id, _drop)

    def resume_signed_out(self) -> int:
        """After a sign-in: every job paused for it waits to run again (PRD 11).

        Returns how many were resumed. Independent review Q4: before this, a
        job that needed sign-in stayed paused after the person signed in.
        """
        count = 0
        with guarded_write(self.path):
            entries, jobs = self._read_for_write()
            for job in jobs:
                if job.state == NEEDS_SIGNIN:
                    job.state = WAITING
                    job.message = "Signed in again; carrying on."
                    job.error_code = ""
                    job.updated = time.time()
                    count += 1
            if count:
                self._write(entries)
        return count

    def waiting_count(self) -> int:
        return sum(1 for j in self._read() if j.state not in FINISHED)


def _runnable(job: Job, pid: int, now: float) -> bool:
    if job.state != WAITING and job.state not in ACTIVE:
        return False
    if job.retry_at > now:
        return False
    return not (job.runner_pid and job.runner_pid != pid and job.lease > now)


# ------------------------------------------------------------------ the runner lock


class RunnerLock:
    """Which app runs (and speaks for) the queue: a heartbeat file.

    :meth:`acquire` succeeds when nobody holds it, the holder is this process,
    or the holder's heartbeat is older than :data:`RUNNER_STALE_S` (it closed
    or crashed). The holder calls :meth:`beat` every few seconds.
    """

    def __init__(self, folder: Path | None = None, *, app: str = "QUILL") -> None:
        self._folder = folder
        self.app = app
        self.pid = os.getpid()

    @property
    def path(self) -> Path:
        return (self._folder or shared_folder()) / RUNNER_LOCK_FILE

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def holder(self) -> dict[str, Any]:
        data = self._read()
        if not data or time.time() - float(data.get("beat", 0)) > RUNNER_STALE_S:
            return {}
        return data

    def held_here(self) -> bool:
        return self.holder().get("pid") == self.pid

    def acquire(self) -> bool:
        from quill.core.storage import write_json_atomic

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with guarded_write(self.path, timeout_s=_CLAIM_WAIT_S, strict=True):
                current = self.holder()
                if current and current.get("pid") != self.pid:
                    return False
                write_json_atomic(
                    self.path, {"pid": self.pid, "app": self.app, "beat": time.time()}
                )
                return True
        except (TimeoutError, OSError):
            return False

    def beat(self) -> bool:
        return self.acquire()

    def release(self) -> None:
        with guarded_write(self.path):
            if self._read().get("pid") == self.pid:
                try:
                    self.path.unlink()
                except OSError:
                    pass


# ------------------------------------------------------------------ sentences


def stage_sentence(job: Job, *, prefix: bool = False) -> str:
    """ "Notes.pdf, adding image descriptions." (the prefix only when asked)."""
    from quill.core.scribe.model import stage_words

    text = f"{job.name}, {stage_words(job.stage)}."
    return f"Scribe for Documents: {text}" if prefix else text


def status_sentence(job: Job) -> str:
    """Scribe Status: "Notes.pdf: adding image descriptions, 40 percent, about 2 minutes." """
    from quill.core.scribe.model import stage_words

    if job.state == CONVERTING and job.stage:
        words = stage_words(job.stage)
    else:
        words = STATE_WORDS.get(job.state, job.state)
    text = f"{job.name}: {words}"
    if job.state in ACTIVE:
        text += f", {job.percent} percent"
        if job.eta_s:
            minutes = max(1, round(job.eta_s / 60))
            text += f", about {minutes} minute{'s' if minutes != 1 else ''}"
    return text + "."


def short_folder(folder: str) -> str:
    """``C:\\Users\\Mike\\Documents\\Scribe for Documents\\Notes`` -> ``Documents\\...``."""
    home = str(Path.home())
    if folder.startswith(home):
        return folder[len(home) :].lstrip("\\/") or folder
    return folder


def result_sentence(job: Job, *, opened: str = "", app: str = "QUILL", offer: bool = False) -> str:
    """The one result sentence (FUX-04): what was made, where, and what opened.

    "Scribe for Documents made Notes.docx and Notes.brf in Documents\\Scribe
    for Documents\\Notes. Word is open in QUILL."
    """
    names = [Path(p).name for p in job.results]
    folders = {str(Path(p).parent) for p in job.results}
    where = short_folder(folders.pop()) if len(folders) == 1 else "your Scribe folder"
    made = (
        f"Scribe for Documents made {labels_from_names(names)} in {where}."
        if names
        else f"Scribe for Documents finished {job.name}."
    )
    if opened:
        return f"{made} {opened} is open in {app}."
    if offer and names:
        return f"{made} Open it from Scribe Conversions."
    return made


def labels_from_names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def finished_while_closed_sentence(jobs: list[Job], app: str = "QUILL") -> str:
    """ "Scribe for Documents finished 2 documents while QUILL was closed: Notes and
    Chapter 2, in Documents\\Scribe for Documents." """
    from quill.core.scribe.library import title_stem

    titles = [title_stem(j.name) for j in jobs]
    folders = {short_folder(str(Path(j.results[0]).parent.parent)) for j in jobs if j.results}
    where = f", in {folders.pop()}" if len(folders) == 1 else ""
    count = "1 document" if len(jobs) == 1 else f"{len(jobs)} documents"
    return (
        f"Scribe for Documents finished {count} while {app} was closed: "
        f"{labels_from_names(titles)}{where}."
    )


__all__ = [
    "ACTIVE",
    "CANCELLED",
    "CONVERTING",
    "DONE",
    "DOWNLOADING",
    "FAILED",
    "FINISHED",
    "GONE",
    "HEARTBEAT_S",
    "NEEDS_PAGES",
    "NEEDS_PASSWORD",
    "NEEDS_SIGNIN",
    "NEEDS_YOU",
    "RUNNER_STALE_S",
    "SLOWED",
    "STATE_WORDS",
    "UPLOADING",
    "WAITING",
    "WAITING_YOU",
    "Job",
    "JobQueue",
    "QueueUnreadable",
    "RunnerLock",
    "finished_while_closed_sentence",
    "result_sentence",
    "shared_folder",
    "stage_sentence",
    "status_sentence",
]
