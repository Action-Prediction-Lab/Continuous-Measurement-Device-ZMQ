"""Event-to-record translator.

Consumes a single evdev.InputEvent and the current accumulator position,
and returns either a partial record dict (with type-specific fields) or
None for ignored events. The caller (emit) is responsible for adding seq,
t, and t_mono.
"""
from __future__ import annotations
from typing import Any, Optional, Tuple

EV_KEY = 1

# evdev key codes. Numeric values from include/uapi/linux/input-event-codes.h.
KEY_VOLUMEDOWN = 114
KEY_VOLUMEUP   = 115
KEY_MUTE       = 113

# Event values: 0 = release, 1 = press, 2 = auto-repeat.
_VALUE_PRESS   = 1
_VALUE_RELEASE = 0

_TICK_KEYS = {
    KEY_VOLUMEUP:   ("KEY_VOLUMEUP",   +1),
    KEY_VOLUMEDOWN: ("KEY_VOLUMEDOWN", -1),
}


def translate(
    event: Any,
    accumulator: int,
) -> Tuple[Optional[dict], int]:
    """Translate one evdev event into a partial record dict and new accumulator.

    Returns (record, new_accumulator). record is None for ignored events
    (releases of tick keys, auto-repeats, EV_SYN, unknown keys). The
    accumulator is mutated only by tick presses.
    """
    if event.type != EV_KEY:
        return None, accumulator

    if event.code in _TICK_KEYS:
        if event.value != _VALUE_PRESS:
            return None, accumulator
        raw_name, delta = _TICK_KEYS[event.code]
        new_pos = accumulator + delta
        record = {
            "type": "tick",
            "delta": delta,
            "position": new_pos,
            "raw_key": raw_name,
        }
        return record, new_pos

    if event.code == KEY_MUTE:
        if event.value == _VALUE_PRESS:
            return {"type": "button", "action": "press", "code": "KEY_MUTE"}, accumulator
        if event.value == _VALUE_RELEASE:
            return {"type": "button", "action": "release", "code": "KEY_MUTE"}, accumulator
        return None, accumulator  # auto-repeat ignored

    return None, accumulator
