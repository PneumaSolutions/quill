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

Failures are bounded (independent review, 2026-11-21). An unexpected error
(not one of Scribe's coded answers) is retried a few times, each after a
longer wait set on the job (``retry_at``), and then the job is marked "did not
finish" with Retry; jobs behind it run meanwhile. A format already saved is
never fetched again (``Job.saved``). ``rate_limited`` -- as a REST answer or a
channel ``error`` event -- waits longer each time it comes and is said once a
job. After a reconnect, and every :data:`POLL_S` while waiting, the outputs are
read over REST, so a conversion that finished while QUILL was not listening is
downloaded rather than waited for.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from quill.core.scribe import queue as q
from quill.core.scribe.auth import TokenStore
from quill.core.scribe.channel import DocumentChannel
from quill.core.scribe.client import ScribeClient
from quill.core.scribe.library import Library, title_stem
from quill.core.scribe.model import (
    ScribeCancelled,
    ScribeError,
    ScribeGone,
    ScribeNeedsPages,
    ScribePasswordNeeded,
    ScribeSaveFailed,
    ScribeSignInRequired,
    format_label,
    plain,
)
from quill.core.scribe.runner_parts import (
    _duration,
    _mark_done,
    _password_given,
    _remove_private_copy,
    _set_fields,
    _set_state,
    _unzip_page,
    cancel_message,
    cancel_waiting,
    hand_over_password,
    take_handed_password,
)
from quill.core.scribe.runner_steps import (
    ConversionSteps,
)

logger = logging.getLogger(__name__)

SayFn = Callable[[str, str, "q.Job"], None]
ChangeFn = Callable[["q.Job"], None]
IDLE_S = 1.0
RETRY_DELAYS_S = (10.0, 60.0, 300.0)  # after an unexpected failure; then it ends
SIGNIN_CHECK_S = 10.0
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


class QueueRunner(ConversionSteps):
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
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], float] = time.time,
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
        self._stop = threading.Event()
        # Waits end early when the app closes, so it hands the lock on at once.
        self._sleep = sleep or self._stop.wait
        self._clock = clock
        self._now = now
        self._passwords: dict[str, str] = {}
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._milestones: dict[str, set[int]] = {}
        self._stages_said: dict[str, set[str]] = {}
        self._resumed_done: list[q.Job] = []
        self._lost = threading.Event()
        self._handed: set[str] = set()  # passwords this app left for another app
        self._slowed_said: set[str] = set()
        self._next_signin_check = 0.0

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self.resume_after_sign_in()
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._loop, name="quill-scribe-queue", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        # A password this app left for another app and nobody collected goes
        # with it: it must not stay in the vault after the app closes (Q24).
        for job_id in list(self._handed):
            take_handed_password(self.client.store, job_id)
        self._handed.clear()
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

    def resume_after_sign_in(self) -> int:
        """Jobs paused for sign-in run again once there is a sign-in (PRD 11)."""
        try:
            if not self.client.store.signed_in():
                return 0
            count = self.jobs.resume_signed_out()
        except (OSError, q.QueueUnreadable):
            return 0
        if count:
            self.wake()
        return count

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
            self._handed.add(job_id)
        else:
            self._passwords[job_id] = password
        # Only a job waiting for its password goes on: a later file of a batch
        # still waits for Send Next File (SCR-POL-3; independent review Q9).
        self.jobs.update(job_id, _password_given)
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
        failures = 0
        while not self._stop.is_set():
            try:
                worked = self.run_once()
                failures = 0
            except Exception as exc:  # noqa: BLE001 - the loop must outlive any one job
                # Never a tight loop: each failure in a row waits longer.
                failures += 1
                logger.warning("Scribe queue runner step failed: %s", type(exc).__name__)
                self._wake.wait(min(60.0, IDLE_S * 2**failures))
                self._wake.clear()
                continue
            if not worked:
                self._wake.wait(IDLE_S)
                self._wake.clear()

    def run_once(self) -> bool:
        """Run the next job if this app may. True when a job was worked on."""
        pid = self.lock.pid
        self._deliver_results()
        if self.jobs.next_runnable(pid=pid, now=self._now()) is None:
            if self.lock.held_here():
                self.lock.beat()
            self._maybe_resume_after_sign_in()
            return False
        if not self.lock.acquire():
            return False
        job = self.jobs.claim(pid, now=self._now())
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

    def _deliver_results(self) -> None:
        """Say each finished job once, in the app that started it.

        The app that started a job announces and opens its result even when
        another app ran it. Only when that app has closed does the app running
        the queue say it, as "finished while QUILL was closed" (independent
        review Q14: it used to say that for an app that was still open).
        """
        try:
            done = [j for j in self.jobs.jobs() if j.state == q.DONE and not j.delivered]
        except (OSError, q.QueueUnreadable):
            return
        for job in done:
            mine = job.owner_pid == self.lock.pid
            if not mine and (q.pid_alive(job.owner_pid) or not self.lock.held_here()):
                continue
            try:
                claimed = self.jobs.mark_delivered(job.id)
            except (OSError, q.QueueUnreadable):
                return
            if claimed is not None:
                self._result(claimed)

    def _maybe_resume_after_sign_in(self) -> None:
        """Another app may have signed in: look now and then while jobs wait for it."""
        if self._clock() < self._next_signin_check:
            return
        self._next_signin_check = self._clock() + SIGNIN_CHECK_S
        if any(j.state == q.NEEDS_SIGNIN for j in self.jobs.jobs()):
            self.resume_after_sign_in()

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
        except q.QueueUnreadable as exc:
            # Nothing can be recorded on the job; it is tried again once the
            # file can be read (the queue is never written over).
            logger.warning("Scribe queue unreadable: %s", exc.code)
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
            url = self._purchase_url(exc.extra.get("purchase_url"))
            try:
                self._update(job, lambda j: setattr(j, "purchase_url", url))
            except _JobCancelled:
                return
            self._needs(
                job,
                q.NEEDS_PAGES,
                exc,
                f"{exc.message} You can still read the first two pages: Open Preview, "
                "in Scribe Conversions.",
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
        except Exception as exc:  # noqa: BLE001 - bounded below, never a tight loop
            self._unexpected(job, exc)

    def _unexpected(self, job: q.Job, exc: BaseException) -> None:
        """Not one of Scribe's coded answers: try again later, a few times, then stop.

        Independent review Q3: such an error left the job active, so the loop
        took it again a second later, forever, saving a new copy each time.
        """
        logger.warning("Scribe job step failed: %s", type(exc).__name__)
        fresh = self.jobs.get(job.id)
        if fresh is None or fresh.state == q.CANCELLED:
            return
        tries = fresh.attempts + 1
        if tries > len(RETRY_DELAYS_S):
            reason = (
                plain(exc)
                if isinstance(exc, ScribeError)
                else (f"something went wrong on this computer ({type(exc).__name__})")
            )
            failure = ScribeError(reason)

            def _give_up(j: q.Job) -> None:
                j.attempts = 0
                j.retry_at = 0.0

            self.jobs.update(job.id, _give_up)
            self._finish_badly(
                job,
                q.FAILED,
                failure,
                f"Scribe for Documents could not convert {job.name}: {reason}.",
            )
            return
        delay = RETRY_DELAYS_S[tries - 1]
        when = self._now() + delay

        def _later(j: q.Job) -> None:
            j.attempts = tries
            j.retry_at = when
            j.message = f"Something went wrong; {self.app} tries again in {_duration(delay)}."

        try:
            self._update(job, _later)
        except _JobCancelled:
            pass

    def _purchase_url(self, offered: Any) -> str:
        """The server's purchase link, only when it is Scribe's own https page (Q27)."""
        url = str(offered or "")
        parts = urlsplit(url)
        own = urlsplit(self.client.purchase_url()).hostname or ""
        host = parts.hostname or ""
        if parts.scheme == "https" and (
            host == own or host.endswith(".scribeit.io") or host == "scribeit.io"
        ):
            return url
        return self.client.purchase_url()

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

    def _download(self, job: q.Job, format_id: str) -> q.Job:
        self._check_owner(job)
        job = self._update(
            job, lambda j: _set_state(j, q.DOWNLOADING, f"Downloading {format_label(format_id)}.")
        )
        folder = Path(job.folder)
        stem = title_stem(job.title or job.name)
        twin = {"html": "offline_html", "offline_html": "html"}.get(format_id, "")
        if twin and twin in job.done_formats:
            # Both come from the one offline_html download: never fetch it twice.
            return self._update(job, partial(_mark_done, format_id=format_id, path=""))
        already = job.saved.get(format_id, "")
        if already and Path(already).exists():
            # Saved before something else failed: never a second copy (Q3).
            return self._update(job, partial(_mark_done, format_id=format_id, path=already))
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
        saved = str(path)
        job = self._update(job, lambda j: j.saved.__setitem__(format_id, saved))
        try:
            self.library.record(job.document_id, job.title or job.name, [saved])
        except OSError as exc:  # the local record is a convenience; the file is saved
            logger.warning("Scribe library record not written: %s", type(exc).__name__)
        return self._update(job, partial(_mark_done, format_id=format_id, path=saved))

    def _done(self, job: q.Job) -> None:
        job = self._update(job, lambda j: _set_fields(j, state=q.DONE, percent=100, message=""))
        self._forget_password(job.id)
        self._stages_said.pop(job.id, None)
        self._slowed_said.discard(job.id)
        _remove_private_copy(job)
        self._deliver_results()

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


__all__ = [
    "QueueRunner",
    "cancel_message",
    "cancel_waiting",
    "hand_over_password",
    "take_handed_password",
]
