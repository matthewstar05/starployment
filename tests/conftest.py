"""Shared fixtures. Every store lives in a temporary directory; nothing touches real data."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# Human pacing (typing delays, time on page, off-screen headed browser) makes
# real submissions look like a person; tests use local forms and run fast.
os.environ.setdefault("JOBHUB_HUMAN_PACING", "0")

from jobhub import config, db


@pytest.fixture(scope="session")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from jobhub.desktop.theme import apply_appearance

    application = QApplication.instance() or QApplication([])
    apply_appearance(application, "Light")
    yield application


@pytest.fixture(autouse=True)
def clean_qt_widgets():
    yield
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance()
    if application is None:
        return
    for widget in list(application.topLevelWidgets()):
        try:
            widget.close()
            widget.deleteLater()
        except Exception:
            pass
    for widget in list(application.allWidgets()):
        try:
            if widget.parent() is None:
                widget.close()
                widget.deleteLater()
        except Exception:
            pass
    application.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    application.processEvents()



@pytest.fixture
def store(tmp_path, monkeypatch):
    """An isolated, initialized database plus the private data folders."""
    db.close()
    paths = {
        "DATA": tmp_path, "DB_PATH": tmp_path / "test.db", "RESUMES": tmp_path / "resumes",
        "TAILORED": tmp_path / "tailored", "SCREENSHOTS": tmp_path / "screenshots",
        "RESUME_LIBRARY": tmp_path / "resumes" / "library", "AGENT_SCRATCH": tmp_path / "agent_scratch",
        "BROWSER_PROFILE": tmp_path / "browser",
    }
    for name, value in paths.items():
        monkeypatch.setattr(config, name, value)
        if name != "DB_PATH":
            value.mkdir(parents=True, exist_ok=True)
    db.init()
    yield tmp_path
    db.close()


def write_pdf(path: Path, lines: list[str]) -> Path:
    from reportlab.pdfgen import canvas
    pdf = canvas.Canvas(str(path))
    for index, line in enumerate(lines):
        pdf.drawString(72, 740 - 18 * index, line)
    pdf.save()
    return path


@pytest.fixture
def make_pdf():
    return write_pdf
