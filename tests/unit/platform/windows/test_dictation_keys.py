"""Windows+H is pressed and always released (the stuck Windows key, 2026-11-21).

``keybd_event`` returns nothing; reading a result from it raised after the
Windows key went down and left it held for the whole desktop. These tests pin
the release with a fake key-event function, so they run anywhere.
"""

from __future__ import annotations

import pytest

from quill.platform.windows import dictation


def test_windows_h_goes_down_in_order_and_comes_up_in_reverse() -> None:
    events: list[tuple[int, int]] = []
    dictation.press_chord(
        (dictation.VK_LWIN, dictation.VK_H),
        lambda vk, _scan, flags, _extra: events.append((vk, flags)),
    )
    up = dictation.KEYEVENTF_KEYUP
    assert events == [
        (dictation.VK_LWIN, 0),
        (dictation.VK_H, 0),
        (dictation.VK_H, up),
        (dictation.VK_LWIN, up),
    ]


def test_a_failure_after_the_windows_key_still_lets_it_go() -> None:
    events: list[tuple[int, int]] = []

    def key_event(vk: int, _scan: int, flags: int, _extra: int) -> None:
        if vk == dictation.VK_H and flags == 0:
            raise OSError("refused")
        events.append((vk, flags))

    with pytest.raises(OSError):
        dictation.press_chord((dictation.VK_LWIN, dictation.VK_H), key_event)
    assert events == [(dictation.VK_LWIN, 0), (dictation.VK_LWIN, dictation.KEYEVENTF_KEYUP)]


def test_a_return_value_is_never_mistaken_for_failure() -> None:
    """The old bug: a non-zero "result" from a VOID function raised mid-chord."""
    events: list[int] = []
    dictation.press_chord((dictation.VK_LWIN,), lambda vk, *_: events.append(vk) or 1)
    assert events == [dictation.VK_LWIN, dictation.VK_LWIN]
