"""Shared fixtures. Every store lives in a temporary directory; nothing touches real data."""
from __future__ import annotations

import os
from pathlib import Path
import re

import pytest

# Human pacing (typing delays, time on page, off-screen headed browser) makes
# real submissions look like a person; tests use local forms and run fast.
os.environ.setdefault("JOBHUB_HUMAN_PACING", "0")
os.environ.setdefault("JOBHUB_UI_PREFS", "0")        # never read or write the real app's view choices
os.environ.setdefault("JOBHUB_GOVERNOR", "0")       # tests never wait on the machine's load

from jobhub import config, db


@pytest.fixture(scope="session")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from jobhub.desktop.theme import apply_appearance

    application = QApplication.instance() or QApplication([])
    apply_appearance(application, "Light")
    yield application


@pytest.fixture
def assert_windows_private_acl():
    """Check Windows owner-only permissions through the native ACL descriptor."""
    def check(path: Path) -> None:
        if os.name != "nt":
            pytest.fail("Windows ACL assertion used outside Windows")
        import ctypes
        from ctypes import wintypes
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32")
        pointer = ctypes.c_void_p
        advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                                                pointer, pointer, pointer, pointer, ctypes.POINTER(pointer)]
        advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [pointer, wintypes.DWORD, wintypes.DWORD,
                                                                                ctypes.POINTER(wintypes.LPWSTR), pointer]
        kernel.LocalFree.argtypes = [pointer]
        descriptor = pointer()
        result = advapi.GetNamedSecurityInfoW(str(path), 1, 4, None, None, None, None, ctypes.byref(descriptor))
        assert result == 0, f"Cannot read the file's Windows DACL: {result}"
        text = wintypes.LPWSTR()
        try:
            assert advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 4, ctypes.byref(text), None), ctypes.get_last_error()
            sddl = text.value.upper()
        finally:
            if text:
                kernel.LocalFree(text)
            kernel.LocalFree(descriptor)
        assert "D:P" in sddl, f"DACL is not protected from parent inheritance: {sddl}"
        assert re.search(r"\(A;[^)]*;;;OW\)", sddl), f"DACL has no owner-rights grant: {sddl}"
        for sid in ("WD", "BU", "AU"):
            assert not re.search(rf";;;{sid}\)", sddl), f"DACL grants access to {sid}: {sddl}"

    return check


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
