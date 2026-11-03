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
_sample: tuple[float, float, int | None] = (-SAMPLE_SECONDS, 0.0, None)   # (taken, load, free %)


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


def _readings() -> tuple[float, int | None]:
    """The one-minute load and free-memory percent, sampled at most every 2 s."""
    global _sample
    with _lock:
        taken, load, free = _sample
        if time.monotonic() - taken < SAMPLE_SECONDS:
            return load, free
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = 0.0
    free = memory_free_percent()
    with _lock:
        _sample = (time.monotonic(), load, free)
    return load, free


def busy(load_per_core: float = LOAD_PER_CORE, min_free: int = MIN_FREE_MEMORY) -> bool:
    """True when the machine has no room for more background work.

    The defaults are the everyday limits; JARVIS ALL+ passes looser ones so it holds
    back only when the Mac is close to running out of CPU or memory.
    """
    if os.environ.get("JOBHUB_GOVERNOR", "1") == "0":
        return False
    load, free = _readings()
    return load > load_per_core * _CORES or (free is not None and free < min_free)


def wait_for_headroom(others_running: Callable[[], int] | int, stop: threading.Event | None = None,
                      max_wait: float = 600.0, poll: float = 1.0) -> None:
    """Wait while the machine is busy, unless nothing else is running. Never waits past ``max_wait``."""
    deadline = time.monotonic() + max_wait
    while others_running() if callable(others_running) else others_running:
        if not busy() or time.monotonic() > deadline or (stop is not None and stop.is_set()):
            return
        time.sleep(poll)


# macOS quality-of-service classes (sys/qos.h). Threads the worker starts inherit its class.
QOS = {"interactive": 0x21, "initiated": 0x19, "default": 0x15, "utility": 0x11, "background": 0x09}


def set_thread_qos(level: str) -> bool:
    """Run the calling thread (and threads it starts) at a macOS quality-of-service class.

    ``background`` keeps work on the efficiency cores with throttled disk and network, so
    it never competes with what the user is doing; ``utility`` is lower than the window but
    may use every core. No-op elsewhere or when the call is unavailable.
    """
    if sys.platform != "darwin" or level not in QOS:
        return False
    try:
        import ctypes
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        return libc.pthread_set_qos_class_self_np(ctypes.c_uint(QOS[level]), ctypes.c_int(0)) == 0
    except (OSError, AttributeError):
        return False


def same_qos() -> Callable[[], None] | None:
    """A ``ThreadPoolExecutor`` initializer that gives pool threads the caller's QoS class
    (Python threads do not inherit it), or None where there is nothing to carry over."""
    if sys.platform != "darwin":
        return None
    try:
        import ctypes
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        current = int(libc.qos_class_self())
    except (OSError, AttributeError):
        return None
    level = next((name for name, value in QOS.items() if value == current), None)
    if level is None or level in ("default", "initiated", "interactive"):
        return None
    return lambda: set_thread_qos(level)
