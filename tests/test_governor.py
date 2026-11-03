"""The resource governor: extra background work waits while the computer is busy, one task always runs."""
from __future__ import annotations

import time
import sys

import pytest

from jobhub import governor


def machine(monkeypatch, load: float, free: int | None):
    monkeypatch.setenv("JOBHUB_GOVERNOR", "1")
    monkeypatch.setattr(governor, "_sample", (-governor.SAMPLE_SECONDS, 0.0, None))
    monkeypatch.setattr(governor, "_CORES", 10)
    monkeypatch.setattr(governor.os, "getloadavg", lambda: (load, load, load), raising=False)
    monkeypatch.setattr(governor, "memory_free_percent", lambda: free)


def test_busy_follows_load_and_free_memory(monkeypatch):
    machine(monkeypatch, 3.0, 45)
    assert not governor.busy()
    machine(monkeypatch, 9.0, 45)          # above 0.7 per performance core
    assert governor.busy()
    machine(monkeypatch, 3.0, 12)          # memory nearly gone
    assert governor.busy()
    machine(monkeypatch, 3.0, None)        # no memory reading: load alone decides
    assert not governor.busy()


def test_disabled_governor_is_never_busy(monkeypatch):
    machine(monkeypatch, 50.0, 1)
    monkeypatch.setenv("JOBHUB_GOVERNOR", "0")
    assert not governor.busy()


def test_the_first_task_never_waits_and_others_wait_at_most_max_wait(monkeypatch):
    machine(monkeypatch, 50.0, 5)
    started = time.monotonic()
    governor.wait_for_headroom(0)                                   # nothing else running: go now
    assert time.monotonic() - started < 0.1
    governor.wait_for_headroom(lambda: 1, max_wait=0.3, poll=0.05)  # busy: waits, but not forever
    assert 0.25 < time.monotonic() - started < 2


def test_missing_getloadavg_uses_windows_cpu_reading(monkeypatch):
    monkeypatch.setattr(governor.sys, "platform", "win32")
    monkeypatch.delattr(governor.os, "getloadavg", raising=False)
    monkeypatch.setattr(governor, "_windows_load", lambda: 2.5)
    monkeypatch.setattr(governor, "memory_free_percent", lambda: 42)
    monkeypatch.setattr(governor, "_sample", (-governor.SAMPLE_SECONDS - 1, 0.0, None))

    assert governor._readings() == (2.5, 42)


@pytest.mark.skipif(sys.platform != "win32", reason="checks native Windows resource counters")
def test_windows_resource_readings_are_bounded(monkeypatch):
    monkeypatch.setattr(governor.os, "getloadavg", None, raising=False)
    monkeypatch.setattr(governor, "_windows_cpu_sample", None)
    monkeypatch.setattr(governor, "_sample", (-governor.SAMPLE_SECONDS - 1, 0.0, None))

    load, free = governor._readings()

    assert 0.0 <= load <= governor._CORES
    assert free is not None and 0 <= free <= 100


def test_keep_awake_holds_caffeinate_only_while_automatic_applications_are_on(monkeypatch):
    import types
    from jobhub.desktop import controller as module

    started = []

    class FakeProcess:
        def __init__(self, command, **_):
            started.append(command)
            self.alive = True

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.alive = False

    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.QApplication, "platformName", staticmethod(lambda: "cocoa"))
    monkeypatch.setattr(module.subprocess, "Popen", FakeProcess)
    holder = types.SimpleNamespace(_caffeinate=None)
    keep = module.DesktopController._keep_awake
    keep(holder, True)
    keep(holder, True)                                  # already held: no second process
    assert len(started) == 1 and started[0][:3] == ["/usr/bin/caffeinate", "-i", "-w"]
    process = holder._caffeinate
    keep(holder, False)
    assert holder._caffeinate is None and not process.alive
