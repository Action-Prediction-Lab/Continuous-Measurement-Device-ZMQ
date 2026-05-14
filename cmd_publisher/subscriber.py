"""Bare host-side subscriber: connect, subscribe to everything, print JSON per line."""
import json
import os
import sys

import zmq


def main() -> int:
    host = os.environ.get("ZMQ_HOST", "localhost")
    port = os.environ.get("ZMQ_PORT", "5580")
    ctx = zmq.Context.instance()
    sub = ctx.socket(zmq.SUB)
    sub.connect(f"tcp://{host}:{port}")
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    print(f"# subscribed to tcp://{host}:{port}", file=sys.stderr)
    try:
        while True:
            print(json.dumps(sub.recv_json(), ensure_ascii=False), flush=True)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
