"""A lock file, for the read-modify-write two processes can both be inside.

QUILL and QUILL Lite are separate processes that share files when the listener
asks them to -- one personal dictionary (``share_quill_dictionary``), one
abbreviation library (``share_quill_abbreviations``). Every one of those is
read-modify-write: load the list, add an entry, write the list back. Two apps
open at once, two words taught within a moment of each other, and one of them is
simply gone, because the second read happened before the first write and the
second write put back a list that never had the first word in it (bad.md S10).

A lock *file* rather than a lock object, because the two are separate processes
and an in-process lock is invisible across that boundary.
``O_CREAT | O_EXCL`` is the one thing every filesystem agrees is atomic.

**It never blocks forever.** After the timeout it proceeds anyway and says so in
the log. Losing one taught word to a race is a smaller failure than an editor
that will not let you teach one at all -- and without the fall-through, a stale
lock left by a process that crashed would be permanent, which is the worst
outcome of the three.

**A lock left by a crash is broken, not worked around.** The holder writes its
pid into the file; a lock file older than :data:`STALE_LOCK_S` belongs to a
process that died inside its write (no write here takes a fraction of that), so
it is removed and taken properly. Without that, one crash disabled the lock for
good and made every later write wait the full timeout first -- harmless for a
dictionary taught twice a day, a two-second freeze per update for the Scribe
queue, which writes many times a minute (independent review Q10, 2026-11-21).

**``strict=True``** never writes unguarded: after the timeout it raises
:class:`TimeoutError` instead. For a claim that must be atomic across processes
(the Scribe runner lock and job claim), going ahead without the lock is the one
wrong answer.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["DEFAULT_LOCK_TIMEOUT_S", "STALE_LOCK_S", "guarded_write"]

logger = logging.getLogger(__name__)

#: How long to wait for another process to finish before going ahead anyway.
#: Two seconds is far longer than any of these writes takes and short enough
#: that a stale lock is not a hang somebody has to diagnose.
DEFAULT_LOCK_TIMEOUT_S = 2.0

#: How often to look again while waiting. Short enough that the common case --
#: the other process finishing immediately -- costs nothing measurable.
_POLL_S = 0.02

#: A lock file older than this was left by a process that died inside its
#: write: every guarded write here takes milliseconds.
STALE_LOCK_S = 10.0


def _break_if_stale(lock_path: Path, stale_s: float) -> bool:
    """Remove a lock file left by a crash; True when one was removed."""
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return False
    if age < stale_s:
        return False
    try:
        lock_path.unlink()
    except OSError:
        return False
    logger.warning("Lock %s was left by a process that stopped; removed it", lock_path)
    return True


@contextmanager
def guarded_write(
    path: Path,
    *,
    timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
    strict: bool = False,
    stale_s: float = STALE_LOCK_S,
) -> Iterator[None]:
    """Hold a lock beside *path* for the duration of the block.

    Read and write must both happen *inside* the block, not either side of it:
    the point is that nothing else rewrites the file between the two. With
    ``strict`` a lock that cannot be had raises :class:`TimeoutError` rather
    than going ahead unguarded.
    """
    lock_path = path.with_name(path.name + ".lock")
    handle: int | None = None
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(handle, f"{os.getpid()} {time.time():.3f}".encode("ascii"))
            except OSError:
                pass
            break
        except FileExistsError:
            if _break_if_stale(lock_path, stale_s):
                continue
            if time.monotonic() >= deadline:
                if strict:
                    raise TimeoutError(f"Lock {lock_path} is held by another process") from None
                logger.warning("Lock %s is held; writing anyway", lock_path)
                break
            time.sleep(_POLL_S)
        except OSError:
            # An unwritable folder is the caller's problem to report, not one to
            # turn into a hang here.
            if strict:
                raise TimeoutError(f"Lock {lock_path} could not be made") from None
            break
    try:
        yield
    finally:
        if handle is not None:
            try:
                os.close(handle)
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
