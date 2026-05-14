"""Disk-first JSONL emit and wire publish for the publisher's record stream.

Heartbeat records skip the disk write (wire-only).
"""
from __future__ import annotations
import json
import os
import time

import zmq


class Emitter:
    """Owns the JSONL file handle, the ZMQ PUB socket, and the seq counter."""

    def __init__(self, *, fp, pub_socket: zmq.Socket):
        self._fp = fp
        self._pub = pub_socket
        self._seq = 0

    def _take_seq(self) -> int:
        s = self._seq
        self._seq += 1
        return s

    def _stamps(self, kernel_t_mono_ns: int | None) -> tuple[float, int]:
        t = time.time()
        t_mono = kernel_t_mono_ns if kernel_t_mono_ns is not None else time.monotonic_ns()
        return t, t_mono

    def _assemble(self, partial: dict, *, kernel_t_mono_ns: int | None) -> dict:
        """Build the full record with 'type' first."""
        seq = self._take_seq()
        t, t_mono = self._stamps(kernel_t_mono_ns)
        type_field = partial["type"]
        rest = {k: v for k, v in partial.items() if k != "type"}
        return {"type": type_field, "seq": seq, "t": t, "t_mono": t_mono, **rest}

    def emit(self, partial: dict, *, kernel_t_mono_ns: int | None = None) -> dict:
        """Stamp, fsync to disk, then publish to wire."""
        record = self._assemble(partial, kernel_t_mono_ns=kernel_t_mono_ns)

        line = json.dumps(record, separators=(",", ":"))
        self._fp.write(line + "\n")
        self._fp.flush()
        os.fsync(self._fp.fileno())

        self._send_best_effort(record)
        return record

    def emit_wire_only(self, partial: dict) -> dict:
        """Stamp and publish to wire only."""
        record = self._assemble(partial, kernel_t_mono_ns=None)
        self._send_best_effort(record)
        return record

    def _send_best_effort(self, record: dict) -> None:
        """Send the record on the wire. Drop silently if the asyncio loop has
        closed (RuntimeError, shutdown path). HWM drops happen inside the ZMQ
        background thread."""
        try:
            self._pub.send_json(record, flags=zmq.NOBLOCK)
        except RuntimeError:
            pass

    def emit_file_only(self, partial: dict) -> dict:
        """Stamp and write to disk only (meta path)."""
        record = self._assemble(partial, kernel_t_mono_ns=None)
        line = json.dumps(record, separators=(",", ":"))
        self._fp.write(line + "\n")
        self._fp.flush()
        os.fsync(self._fp.fileno())
        return record
