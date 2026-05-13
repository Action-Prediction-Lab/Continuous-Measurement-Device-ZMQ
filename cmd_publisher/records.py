"""Lifecycle record builders.

Functions that produce complete record dicts for the publisher-driven
record types: meta, start, heartbeat, stop, session_mark. 
The translator in cmd_publisher.translator assigns the kernel-driven types (tick, button)
by returning partial records; emit.py merges in the common fields.

All builders return dicts that validate against cmd.v1.schema.json.
"""
from __future__ import annotations
from typing import Optional

SCHEMA_ID = "cmd.v1"


def build_meta(
    *,
    seq: int,
    t: float,
    t_mono: int,
    device_info: dict,
    host: str,
    zmq_endpoint: str,
    publisher_pid: int,
    session_id: Optional[str],
) -> dict:
    return {
        "type":          "meta",
        "seq":           seq,
        "t":             t,
        "t_mono":        t_mono,
        "schema":        SCHEMA_ID,
        "device":        device_info,
        "host":          host,
        "zmq_endpoint":  zmq_endpoint,
        "publisher_pid": publisher_pid,
        "session_id":    session_id,
    }


def build_start(*, seq: int, t: float, t_mono: int, position: int) -> dict:
    return {
        "type":     "start",
        "seq":      seq,
        "t":        t,
        "t_mono":   t_mono,
        "position": position,
    }


def build_heartbeat(*, seq: int, t: float, t_mono: int, position: int) -> dict:
    return {
        "type":     "heartbeat",
        "seq":      seq,
        "t":        t,
        "t_mono":   t_mono,
        "position": position,
    }


def build_stop(*, seq: int, t: float, t_mono: int, position: int, reason: str) -> dict:
    return {
        "type":     "stop",
        "seq":      seq,
        "t":        t,
        "t_mono":   t_mono,
        "position": position,
        "reason":   reason,
    }


def build_session_mark(
    *, seq: int, t: float, t_mono: int, phase: str, session_id: str
) -> dict:
    return {
        "type":       "session_mark",
        "seq":        seq,
        "t":          t,
        "t_mono":     t_mono,
        "phase":      phase,
        "session_id": session_id,
    }
