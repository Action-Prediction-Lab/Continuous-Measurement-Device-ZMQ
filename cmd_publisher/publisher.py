"""Asyncio publisher main: evdev events to ZMQ PUB and JSONL, with REP control."""
from __future__ import annotations
import asyncio
import datetime
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path

import zmq
import zmq.asyncio
from evdev import ecodes

from cmd_publisher.device import (
    find_device, grab_and_set_monotonic_clock, device_info_for_meta,
    kernel_t_mono_ns, DeviceNotFoundError,
)
from cmd_publisher.emit import Emitter
from cmd_publisher.translator import translate


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("cmd_publisher")


class Config:
    """Resolved config from environment variables."""

    def __init__(self) -> None:
        self.zmq_host:     str        = os.environ.get("ZMQ_HOST", "*")
        self.zmq_port:     int        = int(os.environ.get("ZMQ_PORT", "5580"))
        self.control_port: int        = int(os.environ.get("CONTROL_PORT", "5581"))
        self.log_dir:      Path       = Path(os.environ.get("LOG_DIR", "./logs"))
        self.device_vid:   str        = os.environ.get("DEVICE_VID", "c001")
        self.device_pid:   str        = os.environ.get("DEVICE_PID", "1dea")
        self.session_id:   str | None = os.environ.get("SESSION_ID", "") or None

    def jsonl_path(self, start_t: float) -> Path:
        iso = datetime.datetime.fromtimestamp(start_t).strftime("%Y-%m-%dT%H-%M-%S")
        label = f"{self.session_id}_" if self.session_id else ""
        return self.log_dir / f"cmd_{label}{iso}.jsonl"


# Partial-record helpers. The Emitter fills in seq, t, t_mono.

def _start_partial(position: int) -> dict:
    return {"type": "start", "position": position}


def _stop_partial(position: int, reason: str) -> dict:
    return {"type": "stop", "position": position, "reason": reason}


def _heartbeat_partial(position: int) -> dict:
    return {"type": "heartbeat", "position": position}


def _session_mark_partial(phase: str, session_id: str) -> dict:
    return {"type": "session_mark", "phase": phase, "session_id": session_id}


def _meta_partial(
    *,
    device_info: dict,
    host: str,
    zmq_endpoint: str,
    publisher_pid: int,
    session_id: str | None,
) -> dict:
    return {
        "type":          "meta",
        "schema":        "cmd.v1",
        "device":        device_info,
        "host":          host,
        "zmq_endpoint":  zmq_endpoint,
        "publisher_pid": publisher_pid,
        "session_id":    session_id,
    }


class Publisher:
    """Orchestrates the four asyncio tasks and owns the device, sockets, and file."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.device = None
        self.fp = None
        self.zmq_ctx: zmq.asyncio.Context | None = None
        self.pub_sock: zmq.asyncio.Socket | None = None
        self.rep_sock: zmq.asyncio.Socket | None = None
        self.emitter: Emitter | None = None
        self.position: int = 0
        self._shutdown_event = asyncio.Event()
        self._shutdown_reason: str = "unhandled_exception"

    def setup(self) -> None:
        """Grab the device, bind PUB and REP sockets, open the JSONL, write meta and start."""
        self.device = find_device(self.cfg.device_vid, self.cfg.device_pid)
        grab_and_set_monotonic_clock(self.device)
        logger.info("grabbed device %s (%s)", self.device.path, self.device.name)

        self.zmq_ctx = zmq.asyncio.Context()
        self.pub_sock = self.zmq_ctx.socket(zmq.PUB)
        self.pub_sock.bind(f"tcp://{self.cfg.zmq_host}:{self.cfg.zmq_port}")
        self.rep_sock = self.zmq_ctx.socket(zmq.REP)
        self.rep_sock.bind(f"tcp://{self.cfg.zmq_host}:{self.cfg.control_port}")
        logger.info(
            "bound PUB tcp://%s:%d, REP tcp://%s:%d",
            self.cfg.zmq_host, self.cfg.zmq_port,
            self.cfg.zmq_host, self.cfg.control_port,
        )

        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        start_t = time.time()
        path = self.cfg.jsonl_path(start_t)
        self.fp = open(path, "a", buffering=1, encoding="utf-8")
        logger.info("opened JSONL %s", path)

        self.emitter = Emitter(fp=self.fp, pub_socket=self.pub_sock)

        self.emitter.emit_file_only(_meta_partial(
            device_info=device_info_for_meta(
                self.device, self.cfg.device_vid, self.cfg.device_pid,
            ),
            host=socket.gethostname(),
            zmq_endpoint=f"tcp://{self.cfg.zmq_host}:{self.cfg.zmq_port}",
            publisher_pid=os.getpid(),
            session_id=self.cfg.session_id,
        ))
        self.emitter.emit(_start_partial(self.position))

    def teardown(self) -> None:
        """Emit the stop record, close the file, ungrab the device, close sockets."""
        if self.emitter is not None and self.fp is not None and not self.fp.closed:
            try:
                self.emitter.emit(_stop_partial(self.position, self._shutdown_reason))
            except Exception:
                logger.exception("failed to emit stop record")

        if self.fp is not None and not self.fp.closed:
            try:
                self.fp.flush()
                os.fsync(self.fp.fileno())
                self.fp.close()
            except Exception:
                logger.exception("failed to close JSONL")

        if self.device is not None:
            try:
                self.device.ungrab()
            except Exception:
                logger.exception("ungrab failed (may already be released)")
            try:
                self.device.close()
            except Exception:
                logger.exception("device.close() failed")

        if self.pub_sock is not None:
            self.pub_sock.close()
        if self.rep_sock is not None:
            self.rep_sock.close()
        if self.zmq_ctx is not None:
            self.zmq_ctx.term()

    async def reader_task(self) -> None:
        """Consume kernel events through the translator and emit records."""
        try:
            async for event in self.device.async_read_loop():
                # Diagnostic only: warn on unknown EV_KEY codes. translate() also
                # returns None for these but we surface them for smoke-test visibility.
                if event.type == ecodes.EV_KEY:
                    raw = ecodes.keys.get(event.code, None)
                    code_name = raw[0] if isinstance(raw, (list, tuple)) else raw
                    if code_name is None:
                        logger.warning("unknown key code %d ignored", event.code)
                        continue

                partial, self.position = translate(event, self.position)
                if partial is None:
                    continue

                self.emitter.emit(partial, kernel_t_mono_ns=kernel_t_mono_ns(event))
        except OSError as exc:
            logger.warning("device read loop ended: %s", exc)
            self._shutdown_reason = "device_disconnected"
            self._shutdown_event.set()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("reader_task crashed")
            self._shutdown_reason = "unhandled_exception"
            self._shutdown_event.set()

    async def heartbeat_task(self) -> None:
        """Emit a heartbeat record on the wire every 1.0 s."""
        try:
            while True:
                await asyncio.sleep(1.0)
                self.emitter.emit_wire_only(_heartbeat_partial(self.position))
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("heartbeat_task crashed")
            self._shutdown_reason = "unhandled_exception"
            self._shutdown_event.set()

    async def control_task(self) -> None:
        """Service the REP socket: validate, record session_mark, reply with status."""
        try:
            while True:
                try:
                    req = await self.rep_sock.recv_json()
                except asyncio.CancelledError:
                    break
                response = self._handle_control_request(req)
                await self.rep_sock.send_json(response)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("control_task crashed")
            self._shutdown_reason = "unhandled_exception"
            self._shutdown_event.set()

    def _handle_control_request(self, req: object) -> dict:
        if not isinstance(req, dict):
            return {"status": "error", "reason": "request must be a JSON object"}
        action = req.get("action")
        if action != "session_mark":
            return {"status": "error", "reason": f"unknown action: {action!r}"}
        phase = req.get("phase")
        if phase not in ("start", "end"):
            return {"status": "error", "reason": f"invalid phase: {phase!r}"}
        session_id = req.get("session_id")
        if not isinstance(session_id, str) or session_id == "":
            return {"status": "error", "reason": "session_id must be a non-empty string"}

        try:
            record = self.emitter.emit(_session_mark_partial(phase, session_id))
        except Exception as exc:
            logger.exception("session_mark emit failed")
            return {"status": "error", "reason": f"emit failed: {type(exc).__name__}"}
        return {
            "status": "ok",
            "seq":    record["seq"],
            "t":      record["t"],
            "t_mono": record["t_mono"],
        }

    async def shutdown_task(self) -> None:
        """Wait for the shutdown event; logs the resolved reason."""
        await self._shutdown_event.wait()
        logger.info("shutdown triggered, reason=%s", self._shutdown_reason)

    def _install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        def _trip(reason: str) -> None:
            self._shutdown_reason = reason
            self._shutdown_event.set()
        loop.add_signal_handler(signal.SIGTERM, _trip, "SIGTERM")
        loop.add_signal_handler(signal.SIGINT,  _trip, "SIGINT")

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        self._install_signal_handlers(loop)

        tasks = [
            asyncio.create_task(self.reader_task(),    name="reader"),
            asyncio.create_task(self.heartbeat_task(), name="heartbeat"),
            asyncio.create_task(self.control_task(),   name="control"),
            asyncio.create_task(self.shutdown_task(),  name="shutdown"),
        ]

        # The shutdown_task returns when the event is set; cancel the rest after.
        shutdown = tasks[-1]
        await shutdown

        for t in tasks[:-1]:
            t.cancel()
        await asyncio.gather(*tasks[:-1], return_exceptions=True)

        return 0


def main() -> int:
    cfg = Config()
    pub = Publisher(cfg)
    try:
        pub.setup()
    except DeviceNotFoundError as exc:
        logger.error("startup failed: %s", exc)
        return 2
    except OSError as exc:
        logger.error("startup failed (device busy or port in use): %s", exc)
        return 3

    try:
        rc = asyncio.run(pub.run())
    except Exception:
        logger.exception("publisher crashed")
        pub._shutdown_reason = "unhandled_exception"
        pub.teardown()
        return 1
    else:
        pub.teardown()
        return rc


if __name__ == "__main__":
    sys.exit(main())
