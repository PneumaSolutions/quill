"""Small pieces of the Scribe queue runner (wx-free): job edits, the password
hand-over between apps, and Cancel Waiting. Split from ``runner.py`` (GATE-11).
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from quill.core.scribe import queue as q
from quill.core.scribe.auth import TokenStore
from quill.core.scribe.library import unpack


def _wire(format_id: str) -> str:
    return "offline_html" if format_id == "html" else format_id


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{round(seconds)} seconds"
    minutes = round(seconds / 60)
    return "a minute" if minutes == 1 else f"{minutes} minutes"


def _password_given(job: q.Job) -> None:
    if job.state == q.NEEDS_PASSWORD:
        _set_state(job, q.WAITING, "Sending with the password.")


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


__all__ = ["cancel_message", "cancel_waiting", "hand_over_password", "take_handed_password"]
