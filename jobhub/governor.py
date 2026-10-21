"""Keep the computer responsive: start extra background work only while it has headroom.

Sends (a browser each) and agent calls run side by side up to the candidate's
settings, but each one past the first waits while the machine is busy: the
one-minute load is above ``LOAD_PER_CORE`` per performance core, or macOS
reports less than ``MIN_FREE_MEMORY`` percent of memory free. One task always
runs, so work slows down under pressure but never stalls.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable

LOAD_PER_CORE = 0.7
MIN_FREE_MEMORY = 20          # percent, from kern.memorystatus_level
SAMPLE_SECONDS = 2.0

_lock = threading.Lock()
_sample: tuple[float, bool] = (0.0, False)


def _sysctl(name: str) -> int | None:
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["/usr/sbin/sysctl", "-n", name], capture_output=True, text=True, timeout=2)
        return int(out.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


_CORES = _sysctl("hw.perflevel0.logicalcpu") or os.cpu_count() or 4


def memory_free_percent() -> int | None:
    return _sysctl("kern.memorystatus_level")


def busy() -> bool:
    """True when the machine has no room for more background work (sampled every 2 s)."""
    global _sample
    if os.environ.get("JOBHUB_GOVERNOR", "1") == "0":
        return False
    with _lock:
        taken, value = _sample
        if time.monotonic() - taken < SAMPLE_SECONDS:
            return value
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = 0.0
    free = memory_free_percent()
    value = load > LOAD_PER_CORE * _CORES or (free is not None and free < MIN_FREE_MEMORY)
    with _lock:
        _sample = (time.monotonic(), value)
    return value


def wait_for_headroom(others_running: Callable[[], int] | int, stop: threading.Event | None = None,
                      max_wait: float = 600.0, poll: float = 1.0) -> None:
    """Wait while the machine is busy, unless nothing else is running. Never waits past ``max_wait``."""
    deadline = time.monotonic() + max_wait
    while others_running() if callable(others_running) else others_running:
        if not busy() or time.monotonic() > deadline or (stop is not None and stop.is_set()):
            return
        time.sleep(poll)
