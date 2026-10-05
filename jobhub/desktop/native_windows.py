"""Windows taskbar identity and title-bar theming for the desktop client.

A source run is ``python.exe``, so without an explicit AppUserModelID the
taskbar groups Starployment under Python and shows Python's icon (or none).
The title bar belongs to Windows, not Qt; Qt only darkens it reliably when the
system itself is dark, so the app sets DWMWA_USE_IMMERSIVE_DARK_MODE on every
top-level window to match the appearance it is painted with. Every call is a
no-op off Windows and on the offscreen platform, and failures are ignored.
"""
from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QLabel, QWidget

APP_USER_MODEL_ID = "local.starployment.desktop"
# DWMWA_USE_IMMERSIVE_DARK_MODE: 20 on Windows 10 20H1+ and 11, 19 before that.
_DARK_MODE_ATTRIBUTES = (20, 19)


def _is_windows() -> bool:
    return sys.platform == "win32"


def set_app_user_model_id() -> None:
    """Give the process its own taskbar identity. Call before creating windows."""
    if not _is_windows():
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        pass


def _native_windows_enabled() -> bool:
    return _is_windows() and QGuiApplication.platformName() == "windows"


def set_dark_title_bar(widget: QWidget, dark: bool) -> None:
    """Match one top-level window's title bar to the app appearance."""
    if not _native_windows_enabled() or not widget.isWindow():
        return
    try:
        hwnd = ctypes.c_void_p(int(widget.winId()))
        value = ctypes.c_int(1 if dark else 0)
        for attribute in _DARK_MODE_ATTRIBUTES:
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
            )
            if result == 0:
                break
    except (AttributeError, OSError, RuntimeError):
        pass


def _scheme_is_dark() -> bool:
    from .theme import DARK, current_scheme

    return current_scheme() == DARK


def sync_title_bars(app: QApplication) -> None:
    """Apply the current appearance to every existing top-level window."""
    if not _native_windows_enabled():
        return
    dark = _scheme_is_dark()
    for widget in app.topLevelWidgets():
        if widget.testAttribute(Qt.WidgetAttribute.WA_WState_Created):
            set_dark_title_bar(widget, dark)


class _TitleBarFilter(QObject):
    """Themes windows created later (dialogs, the setup wizard, message boxes)."""

    def eventFilter(self, obj, event):  # noqa: N802 (Qt API)
        # Only framed windows that already have a native handle: calling winId()
        # on anything else (stray top-level labels, popups) would create one.
        if (event.type() == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow()
                and obj.windowType() in (Qt.WindowType.Window, Qt.WindowType.Dialog)
                and obj.windowHandle() is not None
                and not isinstance(obj, QLabel)):
            set_dark_title_bar(obj, _scheme_is_dark())
        return False


def install_title_bar_sync(app: QApplication) -> None:
    """Keep title bars in step with the appearance for the life of the app."""
    if not _native_windows_enabled() or getattr(app, "_jobhub_title_bar_filter", None) is not None:
        return
    app._jobhub_title_bar_filter = _TitleBarFilter(app)
    app.installEventFilter(app._jobhub_title_bar_filter)
