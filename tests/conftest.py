"""Shared fixtures. Every store lives in a temporary directory; nothing touches real data."""
from __future__ import annotations

from pathlib import Path

import pytest

from jobhub import config, db


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
