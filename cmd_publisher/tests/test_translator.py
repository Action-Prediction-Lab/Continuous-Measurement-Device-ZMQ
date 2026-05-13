"""Unit tests for the event-to-record translator.

Given an evdev InputEvent and the current
accumulator position, return (partial_record_or_None, new_accumulator_position).
Partial records carry the type-specific fields (delta, position, raw_key for
tick; action, code for button) but NOT seq / t / t_mono, which the emit helper
fills in.
"""
from types import SimpleNamespace
import pytest
from cmd_publisher.translator import translate


def _key_event(code_int, value):
    """Build a minimal stand-in for evdev.InputEvent.

    The translator only reads .type, .code, and .value.
    """
    # 1 is evdev.ecodes.EV_KEY
    return SimpleNamespace(type=1, code=code_int, value=value)


def test_volume_up_press_emits_positive_tick():
    event = _key_event(115, value=1)  # 115 = KEY_VOLUMEUP
    record, new_pos = translate(event, accumulator=0)
    assert record == {"type": "tick", "delta": +1, "position": 1, "raw_key": "KEY_VOLUMEUP"}
    assert new_pos == 1


def test_volume_down_press_emits_negative_tick():
    event = _key_event(114, value=1)  # 114 = KEY_VOLUMEDOWN
    record, new_pos = translate(event, accumulator=5)
    assert record == {"type": "tick", "delta": -1, "position": 4, "raw_key": "KEY_VOLUMEDOWN"}
    assert new_pos == 4


def test_volume_release_is_ignored():
    event = _key_event(115, value=0)
    record, new_pos = translate(event, accumulator=3)
    assert record is None
    assert new_pos == 3


def test_volume_autorepeat_is_ignored():
    event = _key_event(115, value=2)
    record, new_pos = translate(event, accumulator=3)
    assert record is None
    assert new_pos == 3


def test_mute_press_emits_button_press():
    event = _key_event(113, value=1)  # 113 = KEY_MUTE
    record, new_pos = translate(event, accumulator=7)
    assert record == {"type": "button", "action": "press", "code": "KEY_MUTE"}
    assert new_pos == 7  # accumulator unchanged


def test_mute_release_emits_button_release():
    event = _key_event(113, value=0)
    record, new_pos = translate(event, accumulator=7)
    assert record == {"type": "button", "action": "release", "code": "KEY_MUTE"}
    assert new_pos == 7


def test_mute_autorepeat_is_ignored():
    event = _key_event(113, value=2)
    record, new_pos = translate(event, accumulator=7)
    assert record is None
    assert new_pos == 7


def test_ev_syn_is_ignored():
    # 0 is evdev.ecodes.EV_SYN
    event = SimpleNamespace(type=0, code=0, value=0)
    record, new_pos = translate(event, accumulator=2)
    assert record is None
    assert new_pos == 2


def test_unknown_key_is_ignored():
    event = _key_event(28, value=1)  # 28 = KEY_ENTER
    record, new_pos = translate(event, accumulator=2)
    assert record is None
    assert new_pos == 2


def test_accumulator_is_unbounded_positive():
    pos = 0
    for _ in range(10_000):
        event = _key_event(115, value=1)
        _record, pos = translate(event, accumulator=pos)
    assert pos == 10_000


def test_accumulator_is_unbounded_negative():
    pos = 0
    for _ in range(10_000):
        event = _key_event(114, value=1)
        _record, pos = translate(event, accumulator=pos)
    assert pos == -10_000


def test_mixed_sequence_accumulates_correctly():
    pos = 0
    sequence = [
        ("KEY_VOLUMEUP",   115, +1, 1),
        ("KEY_VOLUMEUP",   115, +1, 2),
        ("KEY_VOLUMEDOWN", 114, -1, 1),
        ("KEY_VOLUMEUP",   115, +1, 2),
        ("KEY_VOLUMEDOWN", 114, -1, 1),
        ("KEY_VOLUMEDOWN", 114, -1, 0),
        ("KEY_VOLUMEDOWN", 114, -1, -1),
    ]
    for name, code_int, expected_delta, expected_pos in sequence:
        event = _key_event(code_int, value=1)
        record, pos = translate(event, accumulator=pos)
        assert record["delta"] == expected_delta
        assert record["position"] == expected_pos
    assert pos == -1
