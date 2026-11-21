"""How the Scribe queue runner follows one document's conversions (wx-free).

The channel half of :class:`quill.core.scribe.runner.QueueRunner`, split from
``runner.py`` (GATE-11): reconnecting after a drop, starting each format,
reading its events, the REST polling fallback (PRD 3.5), and slowing down
when Scribe asks. Independent review, 2026-11-21: Q6 (a reconnect after the
format finished must not hang), Q7 (error events are the same coded errors as
replies) and the rate-limit back-off.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from quill.core.scribe import queue as q
from quill.core.scribe.channel import DocumentChannel
from quill.core.scribe.client import ScribeClient
from quill.core.scribe.model import (
    ScribeBusy,
    ScribeError,
    ScribeOffline,
    ScribePasswordNeeded,
    error_for,
    format_label,
    plain,
)
from quill.core.scribe.runner_parts import _duration, _set_fields, _set_state, _wire

OFFLINE_SPEAK_AFTER_S = 5.0
SLOW_DOWN_S = 60.0  # the first wait when Scribe asks to slow down; it doubles
SLOW_DOWN_MAX_S = 600.0
MAX_SLOW_DOWNS = 6  # then the job ends with Retry
POLL_S = 30.0  # read the outputs over REST this often while waiting (PRD 3.5)
MAX_RECONNECTS = 8
MILESTONES = (25, 50, 75)


class ConversionSteps:
    """Mixed into :class:`~quill.core.scribe.runner.QueueRunner`."""

    app: str
    client: ScribeClient
    _channel_factory: Callable[[str], DocumentChannel]
    _say: Callable[[str, str, q.Job], None]
    _sleep: Callable[[float], Any]
    _clock: Callable[[], float]
    _milestones: dict[str, set[int]]
    _stages_said: dict[str, set[str]]
    _slowed_said: set[str]

    if TYPE_CHECKING:

        def _update(self, job: q.Job, change: Callable[[q.Job], None]) -> q.Job: ...
        def _reload(self, job: q.Job) -> q.Job: ...
        def _check_owner(self, job: q.Job) -> None: ...
        def _download(self, job: q.Job, format_id: str) -> q.Job: ...
        def _done(self, job: q.Job) -> None: ...
        def _password(self, job_id: str) -> str | None: ...
        def _forget_password(self, job_id: str) -> None: ...

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
                self._check_owner(job)
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
        wires = {_wire(f): f for f in job.remaining}
        for output in outputs:
            if output.complete and output.format in wires:
                job = self._download(job, wires[output.format])
        return job

    def _slow_down(self, job: q.Job, *, then: str = q.CONVERTING) -> q.Job:
        """``rate_limited``: said once a job; each wait doubles, up to ten minutes (PRD 11)."""
        if job.id not in self._slowed_said:
            self._slowed_said.add(job.id)
            self._say("error", f"Scribe for Documents asked {self.app} to slow down.", job)
        count = job.busy_count + 1
        if count > MAX_SLOW_DOWNS:
            raise ScribeError(
                "Scribe for Documents kept asking to slow down. Try again later.",
                server_code="rate_limited",
            )
        delay = min(SLOW_DOWN_MAX_S, SLOW_DOWN_S * 2 ** (count - 1))

        def _waiting(j: q.Job) -> None:
            j.busy_count = count
            _set_state(j, q.SLOWED, f"Waiting {_duration(delay)}, as Scribe asked.")

        job = self._update(job, _waiting)
        self._sleep(delay)
        job = self._reload(job)
        self._check_owner(job)
        return self._update(job, lambda j: _set_state(j, then, ""))

    def _one_format(self, channel: DocumentChannel, job: q.Job, format_id: str) -> q.Job:
        # The API does not hand out the html output itself; "Accessible HTML"
        # comes as the offline web page (the same document with its pictures).
        wire = _wire(format_id)
        job = self._update(job, lambda j: _set_fields(j, current_format=format_id, stage=""))
        if format_id in job.saved and Path(job.saved[format_id]).exists():
            return self._download(job, format_id)  # saved before a failure: no conversion
        # A format made before costs nothing and needs no conversion: Scribe
        # would only answer with its id and send nothing more.
        if self._is_complete(job, wire):
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
                # It may have been this format, finished while QUILL was away:
                # then Scribe answers the next start with its id and nothing
                # more, so ask over REST first (independent review Q6).
                if self._is_complete(job, wire):
                    return self._download(job, format_id)
        self._follow(channel, job, wire)
        return self._download(job, format_id)

    def _is_complete(self, job: q.Job, wire: str) -> bool:
        outputs, _locked = self.client.outputs(job.document_id)
        return any(o.format == wire and o.complete for o in outputs)

    def _finished_meanwhile(self, job: q.Job, format_id: str | None) -> bool:
        """The polling fallback: has the format finished (or, for None, is nothing running)?"""
        try:
            outputs, _locked = self.client.outputs(job.document_id)
        except ScribeOffline:
            return False
        if format_id is not None:
            return any(o.format == format_id and o.complete for o in outputs)
        running = [o for o in outputs if not o.is_preview and o.stage not in ("", "complete")]
        return not running

    def _follow(self, channel: DocumentChannel, job: q.Job, format_id: str | None) -> q.Job:
        """Read events until ``format_id`` completes (or any format, when None).

        Every :data:`POLL_S` without the event, the outputs are read over REST:
        a channel that went quiet never leaves the job waiting for good.
        """
        next_poll = self._clock() + POLL_S
        while True:
            job = self._reload(job)
            event = channel.next_event(timeout=5.0)
            if event is None:
                if self._clock() >= next_poll:
                    next_poll = self._clock() + POLL_S
                    if self._finished_meanwhile(job, format_id):
                        return job
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
                if reason == "conversion_in_progress":
                    continue  # queued behind another format: keep waiting (PRD 11)
                # needs_purchase (with its link), rate_limited, not_found... become
                # the same coded errors as a reply would (independent review Q7).
                extra: dict[str, Any] = {}
                if event.get("purchase_url"):
                    extra["purchase_url"] = str(event["purchase_url"])
                raise error_for(reason, message or reason.replace("_", " "), **extra)

    def _on_status(self, job: q.Job, event: dict[str, Any]) -> q.Job:
        stage = str(event.get("stage") or "")
        try:
            progress = float(event.get("progress") or 0.0)
        except (TypeError, ValueError):
            progress = 0.0
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


__all__ = ["MILESTONES", "POLL_S", "SLOW_DOWN_S", "ConversionSteps"]
