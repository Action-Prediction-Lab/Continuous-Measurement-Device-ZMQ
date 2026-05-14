"""Device discovery, exclusive grab, and CLOCK_MONOTONIC timestamping."""
from __future__ import annotations
import fcntl
import logging
import struct
import time

import evdev


logger = logging.getLogger(__name__)


# EVIOCSCLOCKID from <linux/input.h>: _IOW('E', 0xa0, int)
# = (1 << 30) | (sizeof(int) << 16) | ('E' << 8) | 0xa0
_EVIOCSCLOCKID = 0x400445a0


class DeviceNotFoundError(Exception):
    pass


def find_device(vid_hex: str, pid_hex: str) -> evdev.InputDevice:
    """Find the InputDevice matching vendor:product (lowercase hex, no 0x prefix)."""
    vid = int(vid_hex, 16)
    pid = int(pid_hex, 16)

    matches: list[evdev.InputDevice] = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except (PermissionError, OSError) as exc:
            logger.debug("skipping %s: %s", path, exc)
            continue
        if dev.info.vendor == vid and dev.info.product == pid:
            matches.append(dev)
        else:
            dev.close()

    if not matches:
        raise DeviceNotFoundError(
            f"no /dev/input/event* device matches vendor={vid_hex} product={pid_hex}"
        )

    if len(matches) > 1:
        logger.warning(
            "multiple devices match vendor=%s product=%s (%d total); using %s",
            vid_hex, pid_hex, len(matches), matches[0].path,
        )
        for extra in matches[1:]:
            extra.close()

    return matches[0]


def grab_and_set_monotonic_clock(device: evdev.InputDevice) -> None:
    """Grab the device and set its evdev clock to CLOCK_MONOTONIC.

    Raises OSError if the grab fails (EBUSY when another process holds the grab)
    or the clock change fails. python-evdev does not expose EVIOCSCLOCKID, so the
    ioctl is issued directly.
    """
    device.grab()
    fcntl.ioctl(device.fd, _EVIOCSCLOCKID, struct.pack("i", time.CLOCK_MONOTONIC))


def device_info_for_meta(device: evdev.InputDevice, vid_hex: str, pid_hex: str) -> dict:
    """Build the device_info dict for the meta record."""
    return {
        "vid":        vid_hex,
        "pid":        pid_hex,
        "name":       device.name,
        "evdev_path": device.path,
    }


def kernel_t_mono_ns(event: evdev.InputEvent) -> int:
    """Return the kernel CLOCK_MONOTONIC timestamp in integer nanoseconds.

    Computed from event.sec and event.usec directly; event.timestamp() returns
    a float and loses ns precision at long uptimes.
    """
    return event.sec * 1_000_000_000 + event.usec * 1_000
