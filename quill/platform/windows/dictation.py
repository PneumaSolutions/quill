"""Press Windows+H, the shortcut that opens Windows' own voice typing.

The keys are pressed with ``keybd_event``, which returns nothing (``VOID``).
Until 2026-11-21 the code checked its "return value", which under ctypes'
default ``int`` result is whatever was left in the return register: usually
not zero, so the function raised straight after pressing the Windows key and
never let it go. The Windows key then stayed logically held for the whole
desktop, every later key became a Windows shortcut, and the keyboard looked
dead even after QUILL was closed, until the Windows key was pressed again or
the computer restarted. Now the result is declared ``None`` and every key that
went down is released in a ``finally``, whatever happens in between.
"""

from __future__ import annotations

from collections.abc import Callable

try:  # pragma: no cover - Windows-only runtime hook
    import ctypes
except ImportError:  # pragma: no cover - non-Windows fallback
    ctypes = None  # type: ignore[assignment]

VK_LWIN = 0x5B
VK_H = 0x48
KEYEVENTF_KEYUP = 0x0002

KeyEvent = Callable[[int, int, int, int], object]


def press_chord(keys: tuple[int, ...], key_event: KeyEvent) -> None:
    """Press *keys* in order and release them in reverse, always.

    A key that went down is released even when a later press raises, so a
    failure can never leave a modifier held system-wide.
    """
    pressed: list[int] = []
    try:
        for key_code in keys:
            key_event(key_code, 0, 0, 0)
            pressed.append(key_code)
    finally:
        for key_code in reversed(pressed):
            key_event(key_code, 0, KEYEVENTF_KEYUP, 0)


def launch_windows_dictation() -> None:
    if ctypes is None or not hasattr(ctypes, "windll"):
        raise OSError("Windows dictation is only available on Windows")

    keybd_event = ctypes.windll.user32.keybd_event  # type: ignore[attr-defined]
    keybd_event.restype = None  # VOID: there is no result to check
    press_chord((VK_LWIN, VK_H), keybd_event)
