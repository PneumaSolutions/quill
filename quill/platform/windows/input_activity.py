"""When Windows last saw a key or the mouse, from any program: a tick count.

Used by dictation in other programs (D-10,
:mod:`quill.core.windows_dictation.external_edits`) to tell whether the person
has typed or clicked since dictation typed its own words, without reading the
other program's text: ``GetLastInputInfo`` says *when*, never *what*. Scribe
for Documents asks :func:`input_idle_seconds` before opening a finished result
on its own, so a new tab never opens under someone's typing (Scribe PRD 7.4a).

Read-only; it sends nothing. ``None`` off Windows or when the call fails, which
the caller treats as "cannot tell".
"""

from __future__ import annotations

import ctypes
import sys

__all__ = ["input_idle_seconds", "last_input_tick"]


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def last_input_tick() -> int | None:
    """Milliseconds since Windows started, at the last keyboard or mouse input."""
    if sys.platform != "win32":
        return None
    try:
        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(_LastInputInfo)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):  # type: ignore[attr-defined]
            return None
        return int(info.dwTime)
    except Exception:  # noqa: BLE001 - "cannot tell" is the answer
        return None


def input_idle_seconds() -> float | None:
    """Seconds since the last keyboard or mouse input; ``None`` when it cannot tell."""
    last = last_input_tick()
    if last is None:
        return None
    try:
        now = int(ctypes.windll.kernel32.GetTickCount())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - "cannot tell" is the answer
        return None
    return ((now - last) & 0xFFFFFFFF) / 1000.0  # both wrap at 2**32 ms
