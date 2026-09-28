"""Locate and apply JobHub's desktop icon in source and frozen builds."""
from __future__ import annotations

import sys
from pathlib import Path


def icon_path() -> Path:
    """Return the bundled 256px PNG path, supporting source and PyInstaller runs."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        candidate = Path(frozen_root) / "assets" / "jobhub-icon-256.png"
    else:
        candidate = Path(__file__).resolve().parents[2] / "packaging" / "assets" / "jobhub-icon-256.png"
    return candidate


def apply_application_icon(app) -> None:
    """Set a QApplication icon when the generated asset is present."""
    from PySide6.QtGui import QIcon

    path = icon_path()
    if path.is_file():
        app.setWindowIcon(QIcon(str(path)))
