"""quill.core.file_lock: a crashed holder's lock is broken, and strict never writes unguarded."""

from __future__ import annotations

import os
import time

import pytest

from quill.core.file_lock import STALE_LOCK_S, guarded_write


def test_the_lock_file_names_its_holder_and_goes_away(tmp_path) -> None:
    target = tmp_path / "queue.json"
    lock = tmp_path / "queue.json.lock"
    with guarded_write(target):
        assert lock.read_text(encoding="ascii").split()[0] == str(os.getpid())
    assert not lock.exists()


def test_a_lock_left_by_a_crash_is_broken_at_once(tmp_path) -> None:
    """Independent review Q10: a crash inside a write left the lock for good,
    and every later write waited the full timeout, then wrote unguarded."""
    target = tmp_path / "queue.json"
    lock = tmp_path / "queue.json.lock"
    lock.write_text("4242 0", encoding="ascii")
    old = time.time() - STALE_LOCK_S - 5
    os.utime(lock, (old, old))
    started = time.monotonic()
    with guarded_write(target, timeout_s=2.0, strict=True):
        assert lock.read_text(encoding="ascii").split()[0] == str(os.getpid())
    assert time.monotonic() - started < 1.0
    assert not lock.exists()


def test_strict_raises_rather_than_writing_unguarded(tmp_path) -> None:
    target = tmp_path / "queue.json"
    (tmp_path / "queue.json.lock").write_text("4242 0", encoding="ascii")  # fresh: held
    with pytest.raises(TimeoutError):
        with guarded_write(target, timeout_s=0.1, strict=True):
            pytest.fail("entered without the lock")


def test_the_relaxed_form_still_goes_ahead_after_the_timeout(tmp_path) -> None:
    target = tmp_path / "words.json"
    (tmp_path / "words.json.lock").write_text("4242 0", encoding="ascii")
    entered = False
    with guarded_write(target, timeout_s=0.05):
        entered = True
    assert entered
