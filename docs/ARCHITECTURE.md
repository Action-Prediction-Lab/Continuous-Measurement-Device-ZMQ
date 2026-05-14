# Architecture

## Overview

Our application turns USB rotary-knob HID devices into a continuous behavioural time series. A single Python process inside a privileged Docker container exclusively grabs the knob's events, accumulates each detent as an unbounded signed integer, and emits every event as a JSON record to two synchronised sinks: a ZMQ PUB socket for live consumers and a durable JSONL trace file. A REP socket accepts session-mark annotations from external clients.

We follow the 'do one thing and do it well' approach: this application is a robust publisher; analysis and interpretation live downstream.

## External surfaces

```
   physical detent on knob
            │
            ▼ (USB HID interrupt, bInterval=8 ms)
   /dev/input/eventN (host)
            │
            ▼ (volume mount, kernel evdev)
   [ publisher inside container ]
            │
            ├─▶ ./logs/cmd_[<session>_]<iso>.jsonl (durable trace, host-mounted; <session>_ present if SESSION_ID set)
            │
            ├─▶ tcp://*:5580 (ZMQ PUB)              (live event stream)
            │
            └─◀─ tcp://*:5581 (ZMQ REP)              (session-mark control plane, inbound)
```

Inbound: 
- USB events from the knob. 
- REP messages from external clients. 

Outbound: 
- PUB messages to subscribers. 
- JSON lines to disk.

## Module map

The runtime package is `cmd_publisher/`. Files split by responsibility.

| Module | Responsibility | Imports |
|---|---|---|
| `cmd_publisher.publisher` | Asyncio orchestration. `Config` (env vars), `Publisher` class, `main()` entry point, signal handlers, lifecycle. | All below |
| `cmd_publisher.translator` | `translate(event, accumulator) → (partial_record, new_accumulator)`. Maps evdev events to tick / button records. | stdlib only |
| `cmd_publisher.records` | Schema-lock reference builders for `meta`, `start`, `heartbeat`, `stop`, `session_mark`. Used by `test_schema_lock.py`. | stdlib only |
| `cmd_publisher.emit` | `Emitter` class. `emit()` writes disk-first (fsync per record) then publishes on the wire. `emit_wire_only()` for heartbeats. `emit_file_only()` for meta. | `json`, `os`, `time`, `zmq` |
| `cmd_publisher.device` | `find_device` (scan by VID:PID), `grab_and_set_monotonic_clock` (EVIOCGRAB + EVIOCSCLOCKID ioctl via fcntl), `kernel_t_mono_ns` (integer-ns from event.sec/usec). | `evdev`, `fcntl`, `struct` |
| `cmd_publisher.subscriber` | Host-side CLI tool. Connects to PUB, prints JSON per line on stdout. | `zmq` |
| `cmd_publisher.visualiser` | Host-side multi-window matplotlib live view. Optional derivative panels via causal Savitzky-Golay. | `matplotlib`, `numpy`, `zmq` |

The translator and record builders are pure; `device.py` and `emit.py` wrap the kernel and ZMQ surfaces; `publisher.py` composes them with asyncio. `subscriber.py` and `visualiser.py` run host-side and only talk to the publisher over the wire.

## Data flow

For a single tick event from physical rotation to durable record:

```
   1. Participant rotates knob by one detent.
   2. Rotary encoder closes contacts; firmware emits HID consumer-control press (Volume Up/Down).
   3. USB host polls the device (bInterval = 8 ms). The press is delivered as an interrupt transfer.
   4. Kernel input subsystem stamps the event with CLOCK_MONOTONIC (EVIOCSCLOCKID is called at startup).
   5. evdev surfaces the event on /dev/input/eventN. Our reader_task is async-awaiting the read.
   6. reader_task calls translate(event, position) → returns the partial record {"type":"tick","delta":+1,
      "position":<new>,"raw_key":"KEY_VOLUMEUP"}.
   7. emit() assembles the full record by adding seq, t (time.time()), and t_mono
      (from event.sec*1e9 + event.usec*1e3).
   8. emit() writes the JSON line to the JSONL file, flushes, calls os.fsync(fp.fileno()).
   9. emit() calls pub_sock.send_json(record, flags=zmq.NOBLOCK). Subscribers receive it, or it drops
      at the ZMQ HWM. The file is the authoritative record.
   10. Function returns; reader_task awaits the next event.
```

For session-mark records, an external client sends a REQ to `tcp://localhost:5581`; the `control_task` validates, calls `emit()` (same path as steps 7-10), and sends a structured response back on the REP socket.

For heartbeat records, the `heartbeat_task` calls `emit_wire_only()` every 1 Hz. Heartbeats go to the wire.

For the meta record at startup, `setup()` calls `emit_file_only()` once. Meta is written to the file with fsync.

## Concurrency model

Single asyncio event loop. Four coroutines.

```
                     asyncio event loop
                            │
        ┌──────────┬────────┴────────┬──────────────┐
        │          │                 │              │
   reader_task heartbeat_task   control_task   shutdown_task
        │          │                 │              │
        │ async    │ asyncio.sleep   │ recv_json    │ awaits
        │ for evt  │ (1.0)           │ on REP       │ _shutdown_event
        │ in dev   │                 │              │
        │ async    │                 │              │
        │ read loop│                 │              │
        │          │                 │              │
        └──────────┴────────┬────────┴──────────────┘
                            │
                            ▼ (shared mutable state)
              Publisher self.emitter / self.position / self._shutdown_event
```

- `reader_task` is the kernel-event ingestor; it blocks on `device.async_read_loop()` and processes events as they arrive. 
- `heartbeat_task` is a simple 1 Hz tick. `control_task` services the REP socket. 
- `shutdown_task` awaits `_shutdown_event` and returns when set.

`run()` creates all four tasks, awaits `shutdown_task`, then cancels the other three and gathers them with `return_exceptions=True`. The shutdown event is set by:

- Signal handler (SIGTERM, SIGINT) registered via `loop.add_signal_handler()`.
- `reader_task` catching `OSError` from the device read loop (device unplugged).
- Any task crashing on an unhandled exception (each task wraps its body in `except Exception` that sets `_shutdown_reason = "unhandled_exception"` and trips the event).

Every task path sets the shutdown event, so `run()` always returns.

Because everything runs in one event loop, `Emitter` and the `position` counter are accessed without locks. Coroutines yield cooperatively; the order of effects is well-defined.

## Container lifecycle and grab semantics

```
   docker compose up
            │
            ▼
   main() reads env → Config object
            │
            ├── ValueError (bad ZMQ_PORT etc.) → log, return 3
            │
            ▼
   find_device(c001:1dea) opens /dev/input/eventN
            │
            ├── DeviceNotFoundError → log, teardown, return 2
            │
            ▼
   device.grab() + EVIOCSCLOCKID via fcntl ioctl    ← exclusive ownership BEGINS
            │
            ▼
   bind PUB :5580 and REP :5581
            │
            ├── OSError or zmq.ZMQError → log, teardown, return 3
            │
            ▼
   open JSONL file, emit meta (file only), emit start (file + wire)
            │
            ▼
   asyncio.run(pub.run())                            ← four tasks running
            │
            │   SIGTERM | SIGINT | device-disconnected (OSError) | unhandled_exception
            ▼
   _shutdown_event set → shutdown_task returns → cancel + gather others
            │
            ▼
   emit stop (file + wire), fsync, close file, ungrab device, close sockets
            │
            ▼
   fd close → kernel auto-releases grab              ← happens on ANY exit including SIGKILL
```

The grab semantics are necessary: `EVIOCGRAB` is held by the file descriptor of the open device. When the fd closes, for any reason (clean exit, SIGKILL, crash, container removal), the kernel releases the grab. The container lifecycle is therefore exactly the window of exclusive ownership.

Kernel-stamped CLOCK_MONOTONIC timestamps require `EVIOCSCLOCKID` to be set after grab. `python-evdev` exposes no method for this, so we call `fcntl.ioctl(device.fd, _EVIOCSCLOCKID, ...)` directly (constant computed from `_IOW('E', 0xa0, int)`).

Failure-mode exit codes from `main()`:

| Code | Cause |
|---|---|
| 0 | Clean shutdown via SIGTERM / SIGINT |
| 1 | Unhandled exception inside `run()` |
| 2 | `DeviceNotFoundError` at startup (no matching VID:PID) |
| 3 | Config parsing error (`ValueError`) or socket bind / device grab error (`OSError` / `zmq.ZMQError`) |

`docker compose`'s `restart: unless-stopped` policy restarts the container on any non-zero exit *except* when the user explicitly stops the container via `docker stop`, `docker kill`, or `docker compose down`.

## Wire format

Schema version `cmd.v1`. Full JSON Schema at `cmd_publisher/schema/cmd.v1.schema.json`. Schema-lock tests in `cmd_publisher/tests/test_schema_lock.py` exercise every record type and the negative cases.

Every record carries four common fields:

| Field | Type | Source |
|---|---|---|
| `type` | string | one of `meta`, `start`, `tick`, `button`, `session_mark`, `heartbeat`, `stop` |
| `seq` | int | monotonic per-publisher counter starting at 0 |
| `t` | float | wall-clock seconds, `time.time()` at emit time |
| `t_mono` | int | monotonic ns. For `tick` and `button`: kernel-stamped CLOCK_MONOTONIC from the event. For publisher-driven records: `time.monotonic_ns()` at emit time. |

Tick and button records carry kernel-stamped precision from the originating event. Publisher-driven records (meta, start, heartbeat, session_mark, stop) carry `time.monotonic_ns()` taken at emit time. Intra-stream durations should be computed from `t_mono`. 

Record types:

```json
// meta — first line of JSONL file only. NEVER on the wire.
{"type":"meta","seq":0,"t":...,"t_mono":...,"schema":"cmd.v1","device":{...},
 "host":"...","zmq_endpoint":"tcp://*:5580","publisher_pid":1234,"session_id":null}

// start — wire AND file (line 2 of JSONL).
{"type":"start","seq":1,"t":...,"t_mono":...,"position":0}

// tick — wire AND file. One per knob detent.
{"type":"tick","seq":...,"t":...,"t_mono":...,
 "delta":+1,"position":...,"raw_key":"KEY_VOLUMEUP"}

// button — wire AND file. Press and release events.
{"type":"button","seq":...,"t":...,"t_mono":...,"action":"press","code":"KEY_MUTE"}

// session_mark — wire AND file. Emitted in response to a REP request.
{"type":"session_mark","seq":...,"t":...,"t_mono":...,
 "phase":"start","session_id":"P01"}

// heartbeat — WIRE ONLY. 1 Hz.
{"type":"heartbeat","seq":...,"t":...,"t_mono":...,"position":...}

// stop — wire AND file (final line on clean shutdown).
{"type":"stop","seq":...,"t":...,"t_mono":...,
 "position":...,"reason":"SIGTERM"}
```

The JSONL file is the authoritative record. The wire is a live tap on the same record stream, omitting heartbeats.

Schema forward compatibility: `additionalProperties` is left open, so future-added fields are accepted by older subscribers. Adding new record types or changing existing field semantics bumps the schema version.

## Error handling and recovery

The publisher fails loudly at startup and recovers loudly at runtime.

**Startup failures** (before `run()`):

- Missing device → exit 2.
- Bad env config → exit 3.
- Device busy / port in use → exit 3.

All three call `teardown()` before exit so any partial state (a grabbed device, a half-bound socket) is released cleanly. Logged to stderr, visible in `docker logs`.

**Runtime failures** (inside `run()`):

- Device disconnect (`OSError` in `reader_task`'s read loop) → sets `_shutdown_reason = "device_disconnected"` and trips the shutdown event. Clean shutdown sequence runs. Container restart policy brings it back up; the new instance finds the device by VID:PID (event-node number may change between runs).
- Disk full or unwritable JSONL (`OSError` in `emit()`) → propagates to the task; that task's broad `except Exception` sets `_shutdown_reason = "unhandled_exception"` and trips the shutdown event. Stop record probably cannot be written; absence of `stop` in the JSONL is the diagnostic. Container restarts; if disk is still full, restart loops.
- ZMQ wire-send: messages drop silently at the HWM (no exception raised). `RuntimeError` from a closed event loop at shutdown is swallowed inside `Emitter._send_best_effort()`. The file is the authoritative record; wire is best-effort.
- Bad REP message (malformed JSON, invalid phase, missing session_id) → `_handle_control_request()` returns a structured error response. The JSONL records valid session marks only; the ZMQ REP state machine stays consistent.

**Hard kill** (SIGKILL, power loss):

JSONL is durable up to the last fsynced event. The absence of a `stop` record at the end of the file is the diagnostic of abnormal termination. The post-hoc parser tolerates a trailing partial line (it skips lines that fail to parse). Container restart brings up a fresh publisher with a new JSONL.


## Visualiser architecture

The visualiser is a separate process on the host, run from the developer's venv against a running publisher.

```
   [ visualiser process (host) ]
            │
            └── ZMQ SUB connected to tcp://localhost:5580
                         │
                         ▼
            One asyncio-free animation loop (matplotlib FuncAnimation @ 30 Hz)
                         │
            Drains the SUB socket non-blocking each frame
                         │
                         ▼
            N matplotlib figures (1-4: position, velocity, accel, jerk)
            Driven by the single FuncAnimation on figure[0]
```

Derivatives are computed via a causal Savitzky-Golay filter at 30 Hz internal resampling. The visualiser maintains its own event buffers (lists of `(t_mono_ns, value)`) and resamples on demand for each animation frame using zero-order hold (`np.searchsorted`). The Savitzky-Golay coefficients are precomputed once via least-squares fit of a polynomial of degree 4 over a window of 21 samples.

The filter is causal so the live view has zero lag relative to the participant's rotation. 

The visualiser subscribes to the PUB socket. Multiple visualisers can run simultaneously; the publisher behaves identically regardless of subscriber count.

Memory: event buffers are small in practice (~10 MB for a 1-hour session) and grow linearly with session length. For indefinite running, switch to a rolling window with `--window N` seconds.

## Extension points

**Adding a new record type.** Three steps:

1. Add the type definition to `cmd_publisher/schema/cmd.v1.schema.json` under `$defs`, and add it to the `oneOf` array at the top.
2. Add a partial-record helper to `cmd_publisher/publisher.py` (e.g. `_my_partial(...)`).
3. Call `self.emitter.emit(_my_partial(...))` from wherever the new event originates. If the record should be file-only or wire-only, use `emit_file_only` or `emit_wire_only` instead.

The schema-lock tests in `test_schema_lock.py` should grow a test that exercises the new type. Bump the schema version if the change is breaking.

**Adding a new sink** (e.g. a second file format, a message queue, a database). Extend `Emitter` with a new method that takes a complete record and forwards it. Add the sink resource to `Publisher.setup()` and tear it down in `teardown()`. The disk-first-then-wire ordering convention is worth preserving: the authoritative sink (currently the JSONL file) writes first; lossy sinks (currently the wire) write second.

**Adding a new control message.** Extend `_handle_control_request()` in `publisher.py` with a new `action` branch. Validate inputs, call `emit()` or perform the side effect, return a structured response. Document the request schema in `cmd_publisher/schema/cmd.v1.schema.json`'s control-plane section.

**Changing the device class** (e.g. supporting a different HID device that emits different keys). The translator in `cmd_publisher/translator.py` is the load-bearing module. It hard-codes the consumer-control key codes (`KEY_VOLUMEUP`, `KEY_VOLUMEDOWN`, `KEY_MUTE`) by integer to keep the unit tests hardware-independent. To support a new device, add new key-code constants and extend the dispatch logic in `translate()`. The schema's `raw_key` field surfaces the new code name to downstream consumers. The unit tests should grow to cover the new device class.

**Changing the autosuspend behaviour.** Autosuspend is managed at the host level via udev. If the host's default is `auto` and the device suspends mid-session, install a udev rule pinning `power/control=on` for the device. The recipe is in the deployment notes below.

## Deployment notes

The apparatus assumes a normally-updated Linux host with Docker. The defaults work plug-and-play for the c001:1dea unit.

**Non-default hardware.** If the knob has a different USB vendor:product, edit `.env`:

```ini
DEVICE_VID=xxxx
DEVICE_PID=yyyy
```

The publisher's `find_device()` scans `/dev/input/event*` by VID:PID at runtime. The wire schema's `device.vid` and `device.pid` surface the new values.

**Autosuspend pinning.** On hosts where USB autosuspend is enabled by default (`/sys/bus/usb/devices/.../power/control` reads `auto`), the device may be suspended mid-session. Install a device-specific udev rule:

```
# /etc/udev/rules.d/99-cmd-knob.rules
ACTION=="add", SUBSYSTEM=="usb",
  ATTR{idVendor}=="c001", ATTR{idProduct}=="1dea",
  ATTR{power/control}="on"
```

Reload with `sudo udevadm control --reload-rules && sudo udevadm trigger`. Verify with `cat /sys/bus/usb/devices/<bus>-<port>/power/control`. 

**Common failure modes and how to diagnose.**

| Symptom | Likely cause | Where to look |
|---|---|---|
| `DeviceNotFoundError` at startup | Device unplugged, or wrong VID:PID in `.env`. | `lsusb -d c001:1dea`; check `/dev/input/by-id/`. |
| Container restart-loops | Disk full or unwritable, or persistent port conflict. | `docker logs cmd_publisher`; `df -h logs/`. |
| Host's audio volume changes when turning knob | `EVIOCGRAB` failed silently, or container is not running. | Container logs; verify `grabbed device` log line at startup. |
| Subscriber sees no events but heartbeats arrive | Knob not producing events (mechanical) or VID:PID mismatch. | `sudo evtest /dev/input/by-id/usb-c001_1dea-event-kbd`. |
| JSONL grows during idle | A `tick` event is being emitted spuriously. | Check `dmesg` for USB device misbehaviour. |
