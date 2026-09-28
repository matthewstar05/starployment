"""The desktop loop catches up once after a computer wakes from sleep."""

from PySide6.QtCore import QObject

from jobhub.desktop import controller as desktop_controller


def test_hourly_cycle_becomes_due_after_wall_clock_advances_during_sleep(monkeypatch):
    worker = desktop_controller.DesktopController.__new__(desktop_controller.DesktopController)
    QObject.__init__(worker)
    worker.scheduler_enabled = True
    worker._pending_scan = False
    worker._next_cycle_at = 150.0
    scans = []
    worker.scan = lambda: scans.append("scan")

    monkeypatch.setattr(desktop_controller.time, "time", lambda: 180.0)
    monkeypatch.setattr(desktop_controller.time, "monotonic", lambda: 100.0)
    worker._cycle_due()

    assert scans == ["scan"]
