"""Runs the family queue, one document at a time (wx-free; PRD 7.4, 7.4a).

One :class:`QueueRunner` per app, on its own daemon thread. It works only
while its app holds the :class:`~quill.core.scribe.queue.RunnerLock`, so a
single app uploads and speaks for the whole family; when that app closes,
another takes over and finishes the job (a job already uploaded is never
uploaded again: the document id is in the queue file).

While a job runs, a second thread beats the lock and renews the job's claim
every :data:`~quill.core.scribe.queue.HEARTBEAT_S`, and before each step
(upload, each conversion, each download) the runner checks it still owns
both. An app that lost them -- it was asleep, or frozen, long enough for
another to take over -- stops without touching the job (independent review
Q1).

For each job: upload (unless already uploaded), send the per-document
settings, then for each format in Scribe's build order start the conversion,
follow the channel, and download. Each change is written to the queue file
and handed to ``on_say`` as a sentence of a kind -- ``start``, ``stage``,
``percent``, ``result``, ``error``, ``offline``, ``needs`` -- for the app to
speak or not (its settings decide; start, result and error always speak).

A job that needs the person never opens a dialog: it moves to a "needs"
state and says so once.
"""

from __future__ import annotations

import logging
import threading
import time
import zipfile
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from quill.core.scribe import queue as q
from quill.core.scribe.auth import TokenStore
from quill.core.scribe.channel import DocumentChannel
from quill.core.scribe.client import ScribeClient
from quill.core.scribe.library import Library, unpack
from quill.core.scribe.model import (
    DocumentSettings,
    ScribeBusy,
    ScribeCancelled,
    ScribeError,
    ScribeGone,
    ScribeNeedsPages,
    ScribeOffline,
    ScribePasswordNeeded,
    ScribeSaveFailed,
    ScribeSignInRequired,
    format_label,
    plain,
)

logger = logging.getLogger(__name__)

SayFn = Callable[[str, str, "q.Job"], None]
ChangeFn = Callable[["q.Job"], None]
IDLE_S = 1.0
OFFLINE_SPEAK_AFTER_S = 5.0
SLOW_DOWN_S = 60.0
MAX_RECONNECTS = 8
MILESTONES = (25, 50, 75)
PASSWORD_TRIES = 3


class _JobCancelled(ScribeCancelled):
    """The job was cancelled or removed from the queue while it ran."""

    code = "QUILL-SCRIBE-CANCELLED-JOB"

    def __init__(self) -> None:
        super().__init__("Stopped waiting.")


class _LostOwnership(ScribeCancelled):
    """Another app took the runner lock or this job's claim: stop quietly."""

    code = "QUILL-SCRIBE-CANCELLED-OWNER"

    def __init__(self) -> None:
        super().__init__("Another app is running this job now.")


class QueueRunner:
    def __init__(
        self,
        *,
        app: str = "QUILL",
        jobs: q.JobQueue | None = None,
        lock: q.RunnerLock | None = None,
        client: ScribeClient | None = None,
        library: Library | None = None,
        channel_factory: Callable[[str], DocumentChannel] | None = None,
        on_say: SayFn | None = None,
        on_change: ChangeFn | None = None,
        on_result: ChangeFn | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.app = app
        self.jobs = jobs or q.JobQueue()
        self.lock = lock or q.RunnerLock(app=app)
        store = client.store if client is not None else TokenStore()
        self.client = client or ScribeClient(store)
        self.library = library or Library()
        self._channel_factory = channel_factory or (
            lambda doc_id: DocumentChannel(doc_id, store=store)
        )
        self._say = on_say or (lambda kind, text, job: None)
        self._changed = on_change or (lambda job: None)
        self._result = on_result or (lambda job: None)
        self._sleep = sleep
        self._passwords: dict[str, str] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._milestones: dict[str, set[int]] = {}
        self._stages_said: dict[str, set[str]] = {}
        self._resumed_done: list[q.Job] = []
        self._lost = threading.Event()

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._loop, name="quill-scribe-queue", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        try:
            self.lock.release()
        except OSError:
            pass

    def wake(self) -> None:
        self._wake.set()

    def give_password(self, job_id: str, password: str) -> None:
        """Enter Password...: in memory when this app runs the queue.

        When another app holds the runner lock (PRD 7.4a: the person typed it
        in this app's Scribe Conversions), the password goes to that app
        through the OS secret vault, which it reads once and wipes
        (:func:`hand_over_password`). It is never written to the queue file.
        """
        holder = self.lock.holder()
        if holder and holder.get("pid") != self.lock.pid:
            hand_over_password(self.client.store, job_id, password)
        else:
            self._passwords[job_id] = password
        self.jobs.update(job_id, lambda j: _set_state(j, q.WAITING, "Sending with the password."))
        self.wake()

    def _password(self, job_id: str) -> str | None:
        """This job's password: given here, or handed over by another app."""
        if job_id not in self._passwords:
            handed = take_handed_password(self.client.store, job_id)
            if handed:
                self._passwords[job_id] = handed
        return self._passwords.get(job_id)

    def _forget_password(self, job_id: str) -> None:
        self._passwords.pop(job_id, None)
        take_handed_password(self.client.store, job_id)  # wipes a leftover hand-over

    # ------------------------------------------------------------ the loop

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:  # noqa: BLE001 - the loop must outlive any one job
                logger.exception("Scribe queue runner step failed")
                worked = False
            if not worked:
                self._wake.wait(IDLE_S)
                self._wake.clear()

    def run_once(self) -> bool:
        """Run the next job if this app may. True when a job was worked on."""
        pid = self.lock.pid
        if self.jobs.next_runnable(pid=pid) is None:
            if self.lock.held_here():
                self.lock.beat()
            return False
        if not self.lock.acquire():
            return False
        job = self.jobs.claim(pid)
        if job is None:
            return False
        if job.owner_pid != pid and not job.resumed:
            job = self.jobs.update(job.id, lambda j: setattr(j, "resumed", True)) or job
        self._lost.clear()
        done = threading.Event()
        beater = threading.Thread(
            target=self._heartbeat, args=(job.id, done), name="quill-scribe-beat", daemon=True
        )
        beater.start()
        try:
            self.run_job(job)
        finally:
            done.set()
            beater.join(timeout=5.0)
            if not self._lost.is_set():
                self.jobs.release(job.id, pid)
        return True

    def _heartbeat(self, job_id: str, done: threading.Event) -> None:
        """Keep the runner lock and this job's claim alive while the job runs."""
        while not done.wait(q.HEARTBEAT_S):
            try:
                kept = self.lock.beat() and self.jobs.renew(job_id, self.lock.pid)
            except OSError:
                kept = True  # a passing file error: the next beat tries again
            if not kept:
                self._lost.set()
                return

    def _check_owner(self, job: q.Job) -> None:
        """Before each step: this app still runs the queue and owns this job."""
        if self._lost.is_set() or self._stop.is_set():
            raise _LostOwnership
        if not (self.lock.beat() and self.jobs.renew(job.id, self.lock.pid)):
            self._lost.set()
            raise _LostOwnership

    # ------------------------------------------------------------ one job

    def run_job(self, job: q.Job) -> None:
        self._milestones[job.id] = set()
        self._stages_said.setdefault(job.id, set())
        try:
            if not job.document_id:
                self._upload(job)
                job = self._reload(job)
            if job.settings is not None:
                self._send_settings(job)
            self._convert_all(job)
        except _JobCancelled:
            return
        except ScribePasswordNeeded as exc:
            self._password_refused(job, exc)
        except ScribeSignInRequired as exc:
            self._needs(
                job,
                q.NEEDS_SIGNIN,
                exc,
                "Your Scribe for Documents sign-in has ended. Conversions are paused "
                "until you sign in again.",
            )
        except ScribeNeedsPages as exc:
            url = str(exc.extra.get("purchase_url") or self.client.purchase_url())
            self._update(job, lambda j: setattr(j, "purchase_url", url))
            self._needs(
                job,
                q.NEEDS_PAGES,
                exc,
                f"{exc.message} You can still read the first two pages: Open Preview.",
            )
        except ScribeGone as exc:
            self._finish_badly(job, q.GONE, exc, f"{job.name} is no longer in your Scribe account.")
        except ScribeCancelled:
            return
        except ScribeError as exc:
            sentence = f"Scribe for Documents could not convert {job.name}: {exc.message}"
            if isinstance(exc, ScribeSaveFailed):
                sentence = f"{exc.message} The result stays in your Scribe account."
            self._finish_badly(job, q.FAILED, exc, sentence)

    def _reload(self, job: q.Job) -> q.Job:
        fresh = self.jobs.get(job.id)
        if fresh is None or fresh.state == q.CANCELLED:
            raise _JobCancelled
        return fresh

    def _update(self, job: q.Job, change: Callable[[q.Job], None]) -> q.Job:
        fresh = self.jobs.update(job.id, change)
        if fresh is None:
            raise _JobCancelled
        if fresh.state == q.CANCELLED:
            raise _JobCancelled
        self._changed(fresh)
        return fresh

    def _upload(self, job: q.Job) -> None:
        self._check_owner(job)
        self._update(job, lambda j: _set_state(j, q.UPLOADING, f"Sending {j.name}."))
        self._say("start", f"Scribe for Documents is converting {job.name}.", job)
        password = self._password(job.id)
        if job.source_url:
            document_id = self.client.create_from_url(job.source_url, password)
        else:
            document_id = self.client.create_from_file(Path(job.source_path), password)
        self._check_owner(job)
        self._update(job, lambda j: setattr(j, "document_id", document_id))

    def _send_settings(self, job: q.Job) -> None:
        settings = DocumentSettings.from_json(job.settings)
        self.client.set_document_settings(job.document_id, settings)
        if job.save_account_defaults:
            self.client.set_account_defaults(settings)
        self._update(job, lambda j: setattr(j, "settings", None))

    def _convert_all(self, job: q.Job) -> None:
        job = self._update(job, lambda j: _set_state(j, q.CONVERTING, ""))
        attempts = 0
        offline_since: float | None = None
        offline_said = False
        while job.remaining:
            try:
                with self._channel_factory(job.document_id) as channel:
                    if offline_said:
                        self._say("recovered", "", job)  # silent recovery
                    offline_since, offline_said, attempts = None, False, 0
                    for format_id in list(job.remaining):
                        job = self._one_format(channel, job, format_id)
            except ScribeOffline as exc:
                attempts += 1
                lost = plain(exc)
                offline_since = offline_since or time.monotonic()
                if attempts > MAX_RECONNECTS:
                    raise
                if not offline_said and time.monotonic() - offline_since >= OFFLINE_SPEAK_AFTER_S:
                    self._say(
                        "offline",
                        "Reconnecting to Scribe for Documents; your "
                        "conversion is still running there.",
                        job,
                    )
                    offline_said = True
                self._update(job, partial(_set_fields, message=lost))
                self._sleep(min(30.0, 2.0**attempts))
                job = self._reload(job)
                job = self._collect_finished_while_away(job)
            except ScribeBusy:
                job = self._slow_down(job)
        self._done(job)

    def _collect_finished_while_away(self, job: q.Job) -> q.Job:
        """After a drop, ask whether a format finished meanwhile (polling fallback)."""
        try:
            outputs, _locked = self.client.outputs(job.document_id)
        except ScribeOffline:
            return job
        for output in outputs:
            if output.complete and output.format in job.remaining:
                job = self._download(job, output.format)
        return job

    def _slow_down(self, job: q.Job) -> q.Job:
        if job.state != q.SLOWED:
            self._say("error", f"Scribe for Documents asked {self.app} to slow down.", job)
        job = self._update(
            job, lambda j: _set_state(j, q.SLOWED, "Waiting a minute, as Scribe asked.")
        )
        self._sleep(SLOW_DOWN_S)
        return self._update(self._reload(job), lambda j: _set_state(j, q.CONVERTING, ""))

    def _one_format(self, channel: DocumentChannel, job: q.Job, format_id: str) -> q.Job:
        # The API does not hand out the html output itself; "Accessible HTML"
        # comes as the offline web page (the same document with its pictures).
        wire = "offline_html" if format_id == "html" else format_id
        job = self._update(job, lambda j: _set_fields(j, current_format=format_id, stage=""))
        # A format made before costs nothing and needs no conversion: Scribe
        # would only answer with its id and send nothing more.
        outputs, _locked = self.client.outputs(job.document_id)
        if any(o.format == wire and o.complete for o in outputs):
            return self._download(job, format_id)
        password = self._password(job.id)
        while True:
            self._check_owner(job)
            try:
                channel.start_conversion(wire, password)
                break
            except ScribeError as exc:
                if exc.server_code != "conversion_in_progress":
                    raise
                # Queued: wait for the conversion already running (never an error).
                self._follow(channel, job, None)
        self._follow(channel, job, wire)
        return self._download(job, format_id)

    def _follow(self, channel: DocumentChannel, job: q.Job, format_id: str | None) -> q.Job:
        """Read events until ``format_id`` completes (or any format, when None)."""
        while True:
            job = self._reload(job)
            event = channel.next_event(timeout=5.0)
            if event is None:
                continue
            kind = event.get("type")
            event_format = str(event.get("format") or "")
            if kind == "status" and (format_id is None or event_format == format_id):
                job = self._on_status(job, event)
            elif kind == "conversion_complete":
                if format_id is None or event_format == format_id:
                    return job
            elif kind == "password_required" and not self._password(job.id):
                raise ScribePasswordNeeded(
                    "This document needs its password.", server_code="password_required"
                )
            elif kind == "error":
                reason = str(event.get("reason") or "")
                message = str(event.get("message") or "")
                if reason == "invalid_password":
                    self._forget_password(job.id)
                    raise ScribePasswordNeeded(
                        message or "Password not accepted.", server_code=reason, wrong=True
                    )
                raise ScribeError(message or reason or "unknown error", server_code=reason)

    def _on_status(self, job: q.Job, event: dict[str, Any]) -> q.Job:
        stage = str(event.get("stage") or "")
        progress = float(event.get("progress") or 0.0)
        total = max(1, len(job.formats))
        overall = round((len(job.done_formats) + max(0.0, min(1.0, progress))) / total * 100)
        changed = stage != job.stage
        eta = event.get("estimated_time_remaining")

        def _apply(j: q.Job) -> None:
            j.stage = stage
            j.percent = overall
            j.eta_s = float(eta) if isinstance(eta, (int, float)) else j.eta_s
            label = format_label(j.current_format)
            j.message = f"{label}: {stage.replace('_', ' ')}, {round(progress * 100)} percent."

        job = self._update(job, _apply)
        # Each stage is said once a job: every format passes through
        # "converting", and saying it nine times would be noise (PRD 5, SCR-POL-4).
        said_stages = self._stages_said.setdefault(job.id, set())
        if changed and stage and stage != "complete" and stage not in said_stages:
            said_stages.add(stage)
            self._say("stage", q.stage_sentence(job), job)
        said = self._milestones.setdefault(job.id, set())
        for mark in MILESTONES:
            if overall >= mark and mark not in said:
                said.add(mark)
                self._say("percent", f"{job.name}, {mark} percent.", job)
        return job

    def _download(self, job: q.Job, format_id: str) -> q.Job:
        self._check_owner(job)
        job = self._update(
            job, lambda j: _set_state(j, q.DOWNLOADING, f"Downloading {format_label(format_id)}.")
        )
        folder = Path(job.folder)
        stem = Path(job.title or job.name).stem
        twin = {"html": "offline_html", "offline_html": "html"}.get(format_id, "")
        if twin and twin in job.done_formats:
            # Both come from the one offline_html download: never fetch it twice.
            return self._update(job, partial(_mark_done, format_id=format_id, path=""))
        if format_id == "html":
            # The API does not hand out the html output itself; the offline web
            # page is the same document with its pictures, as a zip.
            path = self.client.download(
                job.document_id, "offline_html", folder, fallback_stem=stem, part_key=job.id
            )
            path = _unzip_page(path, folder, stem)
        else:
            path = self.client.download(
                job.document_id, format_id, folder, fallback_stem=stem, part_key=job.id
            )
        self.library.record(job.document_id, job.title or job.name, [str(path)])
        return self._update(job, partial(_mark_done, format_id=format_id, path=str(path)))

    def _done(self, job: q.Job) -> None:
        job = self._update(job, lambda j: _set_fields(j, state=q.DONE, percent=100, message=""))
        self._forget_password(job.id)
        self._stages_said.pop(job.id, None)
        self._result(job)

    def _password_refused(self, job: q.Job, exc: ScribePasswordNeeded) -> None:
        """``password_required`` waits for the person; a wrong password counts
        a try, and the third ends the job with Retry (PRD 7.3 part 4, 11)."""
        if not exc.extra.get("wrong"):
            self._needs(
                job,
                q.NEEDS_PASSWORD,
                exc,
                f"{job.name} needs its password. Open Scribe Conversions to enter it.",
            )
            return
        try:
            job = self._update(job, lambda j: setattr(j, "password_tries", j.password_tries + 1))
        except _JobCancelled:
            return
        left = PASSWORD_TRIES - job.password_tries
        if left <= 0:
            self._finish_badly(
                job,
                q.FAILED,
                exc,
                f"Scribe for Documents could not convert {job.name}: password not accepted.",
            )
            return
        tries = f"{left} {'tries' if left > 1 else 'try'} left"
        self._needs(
            job,
            q.NEEDS_PASSWORD,
            exc,
            f"Password not accepted for {job.name}. {tries}. "
            "Open Scribe Conversions to enter it again.",
        )

    def _needs(self, job: q.Job, state: str, exc: ScribeError, sentence: str) -> None:
        try:
            job = self._update(
                job, lambda j: _set_fields(j, state=state, message=sentence, error_code=exc.code)
            )
        except _JobCancelled:
            return
        self._say("needs", sentence, job)

    def _finish_badly(self, job: q.Job, state: str, exc: ScribeError, sentence: str) -> None:
        try:
            job = self._update(
                job, lambda j: _set_fields(j, state=state, message=sentence, error_code=exc.code)
            )
        except _JobCancelled:
            return
        self._forget_password(job.id)
        self._stages_said.pop(job.id, None)
        self._say("error", sentence, job)


def _set_state(job: q.Job, state: str, message: str) -> None:
    job.state = state
    if message:
        job.message = message


def _mark_done(job: q.Job, *, format_id: str, path: str) -> None:
    job.done_formats.append(format_id)
    if path:
        job.results.append(path)
    job.state = q.CONVERTING
    job.stage = ""


def _set_fields(job: q.Job, **values: Any) -> None:
    for name, value in values.items():
        setattr(job, name, value)


def _unzip_page(archive: Path, folder: Path, stem: str) -> Path:
    """Unpack an offline web page next to itself and return its HTML page.

    Scribe sends it as one self-contained HTML file today; a zip is unpacked."""
    if not zipfile.is_zipfile(archive):
        return archive
    target = folder / f"{stem} (web page)"
    pages = sorted(
        (n for n in unpack(archive, target) if n.lower().endswith((".html", ".htm"))),
        key=lambda n: (n.count("/"), len(n)),
    )
    archive.unlink(missing_ok=True)
    return target / pages[0] if pages else target


# ------------------------------------------------------------------ hand-over
#
# PRD 7.4a: Enter Password... in one app's Scribe Conversions must reach the
# app running the queue. The password crosses through the OS secret vault (the
# same one that holds the sign-in), under the "scribe" namespace so Sign Out
# wipes it too, and the running app deletes it the moment it reads it. It is
# never written to the queue file or logged.


def _handover_ref(job_id: str) -> Any:
    from quill.core.secrets import SecretRef

    return SecretRef("scribe", f"password-{job_id}")


def hand_over_password(store: TokenStore, job_id: str, password: str) -> None:
    store.manager.set(_handover_ref(job_id), password)


def take_handed_password(store: TokenStore, job_id: str) -> str | None:
    """Read a handed-over password once and wipe it; None when there is none."""
    try:
        ref = _handover_ref(job_id)
        value = store.manager.get(ref)
        if value is not None:
            store.manager.delete(ref)
        return value or None
    except Exception:  # noqa: BLE001 - a locked or missing vault hands nothing over
        return None


cancel_message = (
    "QUILL stopped waiting for {name}. Scribe will still finish it and may use pages; "
    "it stays in My Scribe Documents."
)


def cancel_waiting(jobs: q.JobQueue, job_id: str, app: str = "QUILL") -> str:
    """Cancel Waiting: stop following a job; Scribe carries on (PRD 7.4)."""
    job = jobs.update(job_id, lambda j: _set_state(j, q.CANCELLED, "Stopped waiting."))
    if job is None:
        return ""
    return cancel_message.replace("QUILL", app, 1).format(name=job.name)


__all__ = ["QueueRunner", "cancel_waiting", "hand_over_password", "take_handed_password"]
