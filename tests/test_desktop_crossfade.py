import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QLabel

from jobhub.desktop.ui import JobHubWindow
from jobhub.desktop.widgets import Motion


def _pump(app, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        # deleteLater() only runs from an event loop; flush it as the running app would.
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        time.sleep(0.01)


def _overlays(window):
    root = window.centralWidget()
    return [child for child in root.findChildren(QLabel) if child.parent() is root and child.pixmap() is not None
            and not child.pixmap().isNull()]


def test_theme_crossfade_runs_every_change_and_leaves_no_overlay(monkeypatch):
    # Animated path: the fallback (non-native-glass) crossfade used on Windows and Linux.
    monkeypatch.setenv("JOBHUB_REDUCED_MOTION", "0")
    app = QApplication.instance() or QApplication([])
    window = JobHubWindow()
    window.show()
    app.processEvents()
    calls = []
    try:
        before = len(_overlays(window))
        window.crossfade(lambda: calls.append("first"))
        _pump(app, Motion.SLOW / 1000 + 0.3)  # let it finish: the overlay deletes itself
        # Regression: the finished fade's animation was deleted with its overlay, and the
        # next switch called stop() on it, raised, and never applied the theme.
        window.crossfade(lambda: calls.append("second"))
        # A switch during a running fade must replace it, not stack a frozen frame.
        window.crossfade(lambda: calls.append("third"))
        _pump(app, Motion.SLOW / 1000 + 0.3)
        assert calls == ["first", "second", "third"]
        assert len(_overlays(window)) == before
    finally:
        window.close()
        window.deleteLater()
