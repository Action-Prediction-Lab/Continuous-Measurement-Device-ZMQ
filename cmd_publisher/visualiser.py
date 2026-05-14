"""Real-time visualiser for the Continuous Measurement Device publisher.

Subscribes to tcp://localhost:5580 and opens one matplotlib window per panel
requested via --derivatives (1=position, 2=+velocity, 3=+acceleration, 4=+jerk).
Derivatives use causal Savitzky-Golay smoothing at 30 Hz internal resampling.

Run on the host, not in the container:
    python -m cmd_publisher.visualiser
    python -m cmd_publisher.visualiser --derivatives 4 --window 60
"""
from __future__ import annotations
import argparse
import datetime as dt
import os
import sys
import time
from math import factorial
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.animation as animation
import numpy as np

import zmq


SAMPLE_RATE_HZ = 30
SG_WINDOW = 21
SG_DEGREE = 4
DT_SECONDS = 1.0 / SAMPLE_RATE_HZ
DT_NS = int(1e9 / SAMPLE_RATE_HZ)
HEARTBEAT_TIMEOUT_S = 2.0
REDRAW_INTERVAL_MS = 33

PANEL_LABELS = ["position", "velocity", "acceleration", "jerk"]
PANEL_UNITS = ["ticks", "ticks/s", "ticks/s²", "ticks/s³"]


def _causal_sg_coeffs(window: int, degree: int) -> np.ndarray:
    """Return shape (degree+1, window). Row k, dotted with the last `window`
    samples (oldest first, newest last), yields the k-th derivative at the
    end of the window, in units of [signal] per sample.
    """
    t = np.arange(-(window - 1), 1, dtype=float)
    A = np.vander(t, degree + 1, increasing=True)
    pinv = np.linalg.pinv(A)
    return np.array([factorial(k) * pinv[k] for k in range(degree + 1)])


SG_COEFFS = _causal_sg_coeffs(SG_WINDOW, SG_DEGREE)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Real-time visualiser for the Continuous Measurement Device.",
    )
    p.add_argument(
        "--derivatives", "-d", type=int, default=1, choices=[1, 2, 3, 4],
        help="Number of panels: 1=position, 2=+velocity, 3=+acceleration, 4=+jerk",
    )
    p.add_argument(
        "--window", "-w", type=float, default=None,
        help="Rolling window in seconds. Omit for growing window from t=0.",
    )
    p.add_argument("--zmq-host", default=os.environ.get("ZMQ_HOST", "localhost"))
    p.add_argument("--zmq-port", default=os.environ.get("ZMQ_PORT", "5580"))
    return p.parse_args()


def main() -> int:
    args = parse_args()

    ctx = zmq.Context.instance()
    sub = ctx.socket(zmq.SUB)
    sub.connect(f"tcp://{args.zmq_host}:{args.zmq_port}")
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    print(f"# subscribed to tcp://{args.zmq_host}:{args.zmq_port}", file=sys.stderr)

    tick_t: list[int] = []
    tick_p: list[int] = []
    button_t: list[int] = []
    sm_t: list[int] = []
    sm_session: list[str] = []

    state = {
        "t_start_ns": None,
        "last_msg_t_ns": None,
        "last_recv_wall": None,
        "last_position": 0,
        "last_seq": 0,
    }

    n = args.derivatives
    figs: list[plt.Figure] = []
    axes: list[plt.Axes] = []
    lines: list[plt.Line2D] = []
    readouts: list[plt.Text] = []
    button_markers: list[list] = [[] for _ in range(n)]
    sm_markers: list[list] = [[] for _ in range(n)]

    for k in range(n):
        fig, ax = plt.subplots(figsize=(8, 3))
        title_extra = "" if k == 0 else f"  [Sav-Gol deg {SG_DEGREE}, window {SG_WINDOW}, causal]"
        fig.canvas.manager.set_window_title(
            f"CMD visualiser — {PANEL_LABELS[k]} ({PANEL_UNITS[k]})"
        )
        ax.set_title(f"{PANEL_LABELS[k]} ({PANEL_UNITS[k]}){title_extra}")
        ax.set_xlabel("time since publisher start (s)")
        ax.set_ylabel(f"{PANEL_LABELS[k]} ({PANEL_UNITS[k]})")
        ax.grid(alpha=0.3)
        ax.axhline(0, color="black", lw=0.5, alpha=0.4)
        (line,) = ax.plot([], [], lw=1.2)
        txt = ax.text(
            0.99, 0.97, "", transform=ax.transAxes, ha="right", va="top",
            fontsize=9, family="monospace",
            bbox=dict(boxstyle="round", facecolor="white", alpha=0.7),
        )
        figs.append(fig)
        axes.append(ax)
        lines.append(line)
        readouts.append(txt)

    def save_one(idx: int) -> None:
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        path = Path.cwd() / f"viz_{stamp}_{PANEL_LABELS[idx]}.png"
        figs[idx].savefig(path, dpi=150, bbox_inches="tight")
        print(f"# saved {path}", file=sys.stderr)

    def save_all() -> None:
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        for k_, fig in enumerate(figs):
            if not plt.fignum_exists(fig.number):
                continue
            path = Path.cwd() / f"viz_{stamp}_{PANEL_LABELS[k_]}.png"
            fig.savefig(path, dpi=150, bbox_inches="tight")
            print(f"# saved {path}", file=sys.stderr)

    def on_key(event) -> None:
        if event.key == "s":
            try:
                idx = figs.index(event.canvas.figure)
            except ValueError:
                return
            save_one(idx)
        elif event.key == "S":
            save_all()

    for fig in figs:
        fig.canvas.mpl_connect("key_press_event", on_key)

    def drain_zmq() -> None:
        while True:
            try:
                msg = sub.recv_json(flags=zmq.NOBLOCK)
            except zmq.Again:
                return
            t_mono = msg.get("t_mono")
            tp = msg.get("type")
            if state["t_start_ns"] is None and t_mono is not None:
                state["t_start_ns"] = t_mono
            if t_mono is not None:
                state["last_msg_t_ns"] = t_mono
            state["last_recv_wall"] = time.monotonic()
            if "seq" in msg:
                state["last_seq"] = msg["seq"]
            if tp == "tick":
                tick_t.append(t_mono)
                tick_p.append(msg["position"])
                state["last_position"] = msg["position"]
            elif tp == "button":
                button_t.append(t_mono)
            elif tp == "session_mark":
                sm_t.append(t_mono)
                sm_session.append(msg.get("session_id", ""))
            elif tp == "heartbeat":
                state["last_position"] = msg["position"]
            elif tp == "start":
                state["last_position"] = msg.get("position", 0)

    def compute_panel(k: int, view_start_ns: int, view_end_ns: int):
        """Return (xs_seconds, ys) for panel k, or (None, None) if no data."""
        if not tick_t:
            return None, None
        t_arr = np.asarray(tick_t, dtype=np.int64)
        p_arr = np.asarray(tick_p, dtype=np.int64)
        t_start = state["t_start_ns"]

        if k == 0:
            mask = (t_arr >= view_start_ns) & (t_arr <= view_end_ns)
            xs = (t_arr[mask] - t_start) / 1e9
            ys = p_arr[mask].astype(float)
            return xs, ys

        grid_start = view_start_ns - (SG_WINDOW - 1) * DT_NS
        n_samples = int((view_end_ns - grid_start) / DT_NS) + 1
        if n_samples < SG_WINDOW:
            return None, None
        grid_ns = grid_start + DT_NS * np.arange(n_samples)
        idx = np.searchsorted(t_arr, grid_ns, side="right") - 1
        valid = idx >= 0
        if not valid.any():
            return None, None
        safe = np.where(valid, idx, 0)
        samples = p_arr[safe].astype(float)
        samples[~valid] = np.nan

        coeffs = SG_COEFFS[k]
        deriv = np.full(n_samples, np.nan)
        for i in range(SG_WINDOW - 1, n_samples):
            window = samples[i - SG_WINDOW + 1 : i + 1]
            if np.isnan(window).any():
                continue
            deriv[i] = float(np.dot(coeffs, window)) / (DT_SECONDS ** k)
        out_ns = grid_ns[SG_WINDOW - 1 :]
        out_vals = deriv[SG_WINDOW - 1 :]
        in_view = (out_ns >= view_start_ns) & (out_ns <= view_end_ns)
        xs = (out_ns[in_view] - t_start) / 1e9
        ys = out_vals[in_view]
        return xs, ys

    def refresh_markers(k: int, view_start_ns: int, view_end_ns: int) -> None:
        # Remove old, add new in window. Cheap because there are few markers.
        for ln in button_markers[k]:
            ln.remove()
        button_markers[k].clear()
        for ln in sm_markers[k]:
            ln.remove()
        sm_markers[k].clear()
        t_start = state["t_start_ns"]
        if t_start is None:
            return
        for t_ns in button_t:
            if view_start_ns <= t_ns <= view_end_ns:
                ln = axes[k].axvline(
                    (t_ns - t_start) / 1e9, color="red", lw=0.6, alpha=0.5,
                )
                button_markers[k].append(ln)
        for t_ns, sid in zip(sm_t, sm_session):
            if view_start_ns <= t_ns <= view_end_ns:
                ln = axes[k].axvline(
                    (t_ns - t_start) / 1e9, color="purple", lw=0.8, alpha=0.7,
                    linestyle="--",
                )
                sm_markers[k].append(ln)

    def animate(_frame):
        drain_zmq()
        if state["t_start_ns"] is None:
            return lines

        t_now_s = (state["last_msg_t_ns"] - state["t_start_ns"]) / 1e9
        view_end_ns = state["last_msg_t_ns"]
        if args.window is None:
            view_start_ns = state["t_start_ns"]
            x_min, x_max = 0.0, max(t_now_s, 1.0)
        else:
            view_start_ns = max(state["t_start_ns"], view_end_ns - int(args.window * 1e9))
            x_min, x_max = max(0.0, t_now_s - args.window), t_now_s

        alive = (
            state["last_recv_wall"] is not None
            and (time.monotonic() - state["last_recv_wall"]) < HEARTBEAT_TIMEOUT_S
        )
        status = "publisher: connected" if alive else "publisher: silent"

        for k in range(n):
            if not plt.fignum_exists(figs[k].number):
                continue
            xs, ys = compute_panel(k, view_start_ns, view_end_ns)
            if xs is None:
                lines[k].set_data([], [])
            else:
                lines[k].set_data(xs, ys)
                axes[k].set_xlim(x_min, x_max)
                if len(ys) > 0:
                    finite = ys[np.isfinite(ys)]
                    if finite.size > 0:
                        ymin = float(np.min(finite))
                        ymax = float(np.max(finite))
                        pad = max(0.5, 0.1 * (ymax - ymin)) if ymax > ymin else 1.0
                        axes[k].set_ylim(ymin - pad, ymax + pad)
            refresh_markers(k, view_start_ns, view_end_ns)

            if k == 0:
                readouts[k].set_text(
                    f"position: {state['last_position']}\n"
                    f"seq:      {state['last_seq']}\n"
                    f"{status}"
                )
            else:
                cur = float("nan") if xs is None or len(ys) == 0 else float(ys[-1])
                readouts[k].set_text(
                    f"{PANEL_LABELS[k]}: {cur:8.2f} {PANEL_UNITS[k]}\n"
                    f"seq:      {state['last_seq']}\n"
                    f"{status}"
                )
            if k != 0:
                figs[k].canvas.draw_idle()

        return lines

    # The FuncAnimation must be assigned to a name; matplotlib garbage-collects
    # it otherwise and the animation never runs.
    _ani = animation.FuncAnimation(
        figs[0], animate, interval=REDRAW_INTERVAL_MS,
        blit=False, cache_frame_data=False,
    )

    try:
        plt.show()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
