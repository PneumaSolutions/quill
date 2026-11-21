"""The queue's words and small facts (wx-free): the sentences a listener hears
about a job, a link's name, and whether an app is still running.

Split from ``queue.py`` (GATE-11), which re-exports every name here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from quill.core.scribe.queue import ACTIVE, CONVERTING, STATE_WORDS, Job


def link_name(url: str) -> str:
    """A link as a job's name: its file name, never the whole address read aloud
    (Q43): "https://x.org/files/Notes.pdf?token=..." -> "Notes.pdf"."""
    from urllib.parse import unquote, urlsplit

    parts = urlsplit(url)
    last = unquote(parts.path.rstrip("/").rsplit("/", 1)[-1])
    return last or parts.hostname or "link"


def scrub_link(url: str) -> str:
    """The link without its query or fragment, where tokens live (Q43)."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def pid_alive(pid: int) -> bool:
    """Whether a process with this id is running (False for 0 or a bad id)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return int(code.value) == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # someone else's process, alive
    except OSError:
        return False
    return True


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
    "finished_while_closed_sentence",
    "labels_from_names",
    "link_name",
    "pid_alive",
    "result_sentence",
    "scrub_link",
    "short_folder",
    "stage_sentence",
    "status_sentence",
]
