"""Locate and apply Starployment's desktop icon in source and frozen builds."""
from __future__ import annotations

import sys
from pathlib import Path


def _assets_dir() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "assets"
    return Path(__file__).resolve().parents[2] / "packaging" / "assets"


def icon_path() -> Path:
    """Return the bundled 256px PNG path, supporting source and PyInstaller runs."""
    return _assets_dir() / "starployment-icon-256.png"


def window_icon_path() -> Path:
    """Icon for windows and the taskbar.

    Windows uses the multi-size .ico so the taskbar and title bar get crisp
    16-48px renditions; elsewhere (or without the .ico) use the PNG.
    """
    ico = _assets_dir() / "starployment.ico"
    if sys.platform == "win32" and ico.is_file():
        return ico
    return icon_path()


def apply_application_icon(app) -> None:
    """Set a QApplication icon when the generated asset is present."""
    from PySide6.QtGui import QIcon

    path = window_icon_path()
    if path.is_file():
        app.setWindowIcon(QIcon(str(path)))
