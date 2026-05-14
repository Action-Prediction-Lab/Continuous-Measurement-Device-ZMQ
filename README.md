# Continuous Measurement Device ZMQ

Plug and play capture of continuous behavioural time series from consumer USB volume knobs (rotary encoder). The aforementioned time series is emitted both on a ZeroMQ PUB socket for live consumers and recorded to a JSONL trace file.

## Host requirements

Linux with Docker and `docker compose`. A USB rotary-knob HID device plugged in; defaults to vendor:product `c001:1dea` (any volume-knob HID device works, set `DEVICE_VID` and `DEVICE_PID` in `.env` for a different device).

## Quick start

Plug in the USB knob.

```bash
git clone https://github.com/Action-Prediction-Lab/Continuous-Measurement-Device-ZMQ.git
cp .env.example .env
docker compose up --build
```

### Test
In a second terminal on the host (the subscriber needs `pyzmq` available; if you have not set up a host-side venv yet, see *Unit Tests* below):

```bash
python3 -m cmd_publisher.subscriber
```

Turn the knob; `tick` records with `delta: +/-1` for clockwise and anti-clockwise respectively.

Importantly the host's audio volume should NOT change (the publisher grabs the device exclusively).

## Wire format

Records are single-line JSON objects on the PUB socket (`tcp://*:5580` by default) and in the JSONL trace file (`./logs/cmd_<iso>.jsonl`). Seven record types: `meta`, `start`, `tick`, `button`, `session_mark`, `heartbeat`, `stop`. Schema version `cmd.v1`; full schema in `cmd_publisher/schema/cmd.v1.schema.json`.

Heartbeats are wire-only (never written to the JSONL file). 

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `ZMQ_HOST` | `*` | Interface to bind the PUB and REP sockets |
| `ZMQ_PORT` | `5580` | PUB socket port |
| `CONTROL_PORT` | `5581` | REP socket port for session-mark annotations |
| `LOG_DIR` | `./logs` | Host directory for JSONL files (mounted to `/logs`) |
| `DEVICE_VID` | `c001` | Target USB vendor (lowercase hex, no 0x) |
| `DEVICE_PID` | `1dea` | Target USB product (lowercase hex, no 0x) |
| `SESSION_ID` | (unset) | Optional opaque session label |

## Session annotations

The REP socket on `:5581` accepts `session_mark` annotations from any client. The JSONL gains a `session_mark` record on success.

```python
import zmq
ctx = zmq.Context()
s = ctx.socket(zmq.REQ)
s.connect("tcp://localhost:5581")
s.send_json({"action": "session_mark", "phase": "start", "session_id": "P01"})
print(s.recv_json())  # {"status": "ok", "seq": ..., "t": ..., "t_mono": ...}
```

## Unit Tests
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r cmd_publisher/requirements-dev.txt
PYTHONPATH=. pytest cmd_publisher/tests/ -v
```

## License

See `LICENSE`.
