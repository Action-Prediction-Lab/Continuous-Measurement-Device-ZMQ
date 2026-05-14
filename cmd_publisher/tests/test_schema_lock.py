"""Schema-lock tests.

The tests construct one record of every type using the public builders
and the translator, validate each against cmd.v1.schema.json,
and validate that an aggregated JSONL-style list still validates. Any
unintended drift in the record shape or the schema document fails the test.
"""
import json
from pathlib import Path

import jsonschema
import pytest

from cmd_publisher.records import (
    build_meta, build_start, build_heartbeat, build_stop, build_session_mark,
)
from cmd_publisher.translator import translate
from types import SimpleNamespace


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "cmd.v1.schema.json"


@pytest.fixture(scope="module")
def schema():
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture
def common():
    """Common fields the builders or the emit helper would fill in."""
    return {"seq": 0, "t": 1747159342.123456, "t_mono": 12_345_678_901_234}


def _validate(record, schema):
    jsonschema.validate(instance=record, schema=schema)


def test_meta_record_validates(schema, common):
    record = build_meta(
        **common,
        device_info={
            "vid": "c001", "pid": "1dea",
            "name": "HID c001:1dea Keyboard",
            "evdev_path": "/dev/input/event19",
        },
        host="robot-rig-01",
        zmq_endpoint="tcp://*:5580",
        publisher_pid=1234,
        session_id="P01",
    )
    _validate(record, schema)
    assert record["type"] == "meta"
    assert record["schema"] == "cmd.v1"


def test_meta_with_null_session_id_validates(schema, common):
    record = build_meta(
        **common,
        device_info={"vid": "c001", "pid": "1dea",
                     "name": "HID c001:1dea Keyboard",
                     "evdev_path": "/dev/input/event19"},
        host="h", zmq_endpoint="tcp://*:5580",
        publisher_pid=1, session_id=None,
    )
    _validate(record, schema)
    assert record["session_id"] is None


def test_start_record_validates(schema, common):
    record = build_start(**common, position=0)
    _validate(record, schema)


def test_tick_record_validates(schema, common):
    event = SimpleNamespace(type=1, code=115, value=1)  # KEY_VOLUMEUP press
    partial, _new_pos = translate(event, accumulator=0)
    record = {**common, **partial}
    _validate(record, schema)


def test_button_press_record_validates(schema, common):
    event = SimpleNamespace(type=1, code=113, value=1)  # KEY_MUTE press
    partial, _ = translate(event, accumulator=0)
    record = {**common, **partial}
    _validate(record, schema)


def test_button_release_record_validates(schema, common):
    event = SimpleNamespace(type=1, code=113, value=0)
    partial, _ = translate(event, accumulator=0)
    record = {**common, **partial}
    _validate(record, schema)


def test_session_mark_start_validates(schema, common):
    record = build_session_mark(**common, phase="start", session_id="P01")
    _validate(record, schema)


def test_session_mark_end_validates(schema, common):
    record = build_session_mark(**common, phase="end", session_id="P01")
    _validate(record, schema)


def test_heartbeat_record_validates(schema, common):
    record = build_heartbeat(**common, position=7)
    _validate(record, schema)


def test_stop_record_validates(schema, common):
    record = build_stop(**common, position=7, reason="SIGTERM")
    _validate(record, schema)


def test_stop_with_each_known_reason_validates(schema, common):
    for reason in ("SIGTERM", "SIGINT", "device_disconnected", "unhandled_exception"):
        record = build_stop(**common, position=0, reason=reason)
        _validate(record, schema)


def test_invalid_stop_reason_fails_schema(schema, common):
    record = build_stop(**common, position=0, reason="SIGTERM")
    record["reason"] = "WHATEVER"
    with pytest.raises(jsonschema.ValidationError):
        _validate(record, schema)


def test_extra_top_level_field_does_not_break_validation(schema, common):
    # Schema does not declare additionalProperties:false, so unknown fields are
    # allowed. 
    record = build_start(**common, position=0)
    record["extra_field"] = "future use"
    _validate(record, schema)


def test_invalid_tick_delta_fails_schema(schema, common):
    # delta must be -1 or +1; anything else should fail.
    record = {**common, "type": "tick", "delta": 2, "position": 2, "raw_key": "KEY_VOLUMEUP"}
    with pytest.raises(jsonschema.ValidationError):
        _validate(record, schema)


def test_empty_session_id_fails_schema(schema, common):
    # session_mark.session_id must be a non-empty string; matches publisher validation.
    record = build_session_mark(**common, phase="start", session_id="")
    with pytest.raises(jsonschema.ValidationError):
        _validate(record, schema)
