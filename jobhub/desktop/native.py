"""Native window chrome for the desktop client.

On macOS the window content extends under a transparent title bar, so the
floating glass sidebar holds the traffic-light buttons, as in current macOS
apps. Every other platform, the offscreen test platform, and
``JOBHUB_NATIVE_TITLEBAR=0`` keep a standard framed window.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

# Qt::ExpandedClientAreaHint (Qt 6.9+) reuses the MaximizeUsingFullscreenGeometryHint
# bit, so PySide lists it only as an enum alias; fall back to the raw value.
EXPANDED_CLIENT_AREA_HINT = Qt.WindowType.__members__.get(
    "ExpandedClientAreaHint", Qt.WindowType(0x00400000)
)
# Height of the macOS title-bar band the content shares. An empty unified
# toolbar makes the band 52pt tall, which centers the traffic lights lower,
# inside the floating sidebar, as in current macOS apps.
TITLE_BAR_HEIGHT = 52


def native_chrome_enabled() -> bool:
    if sys.platform != "darwin":
        return False
    if os.environ.get("JOBHUB_NATIVE_TITLEBAR", "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return QGuiApplication.platformName() == "cocoa"


def configure_window_chrome(window: QWidget) -> bool:
    """Request a full-height content area with a transparent title bar.

    Call before the window is first shown. Returns True when the content now
    shares the title-bar band, so the caller can reserve room for the
    traffic lights.
    """
    if not native_chrome_enabled():
        return False
    try:
        window.setWindowFlags(
            window.windowFlags()
            | EXPANDED_CLIENT_AREA_HINT
            | Qt.WindowType.NoTitleBarBackgroundHint
        )
        window.setAttribute(Qt.WidgetAttribute.WA_ContentsMarginsRespectsSafeArea, False)
    except (TypeError, ValueError, RuntimeError):
        return False
    style_title_bar(window)
    return True


def _objc():
    path = ctypes.util.find_library("objc")
    if not path:
        raise OSError("libobjc is unavailable")
    library = ctypes.cdll.LoadLibrary(path)
    library.sel_registerName.restype = ctypes.c_void_p
    library.sel_registerName.argtypes = [ctypes.c_char_p]
    return library


def _message(library, restype, *argtypes):
    """A typed ``objc_msgSend`` prototype (arm64 requires exact, non-variadic types)."""
    address = ctypes.cast(library.objc_msgSend, ctypes.c_void_p).value
    return ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(address)


def _ns_window(library, window: QWidget):
    view = ctypes.c_void_p(int(window.winId()))
    get_window = _message(library, ctypes.c_void_p)
    return get_window(view, library.sel_registerName(b"window"))


def style_title_bar(window: QWidget) -> bool:
    """Hide the centered title and attach an empty unified toolbar.

    The title stays set for the Window menu, Mission Control, and assistive
    technology; the sidebar already shows the app name. Safe to call more than
    once. Any failure leaves the standard title bar in place.
    """
    if sys.platform != "darwin" or QGuiApplication.platformName() != "cocoa":
        return False
    try:
        library = _objc()
        ns_window = _ns_window(library, window)
        if not ns_window:
            return False
        sel = library.sel_registerName
        send_long = _message(library, None, ctypes.c_long)
        send_long(ns_window, sel(b"setTitleVisibility:"), 1)  # NSWindowTitleHidden
        get_object = _message(library, ctypes.c_void_p)
        if not get_object(ns_window, sel(b"toolbar")):
            library.objc_getClass.restype = ctypes.c_void_p
            library.objc_getClass.argtypes = [ctypes.c_char_p]
            make_string = _message(library, ctypes.c_void_p, ctypes.c_char_p)
            identifier = make_string(library.objc_getClass(b"NSString"), sel(b"stringWithUTF8String:"),
                                     b"JobHubUnifiedToolbar")
            toolbar = get_object(library.objc_getClass(b"NSToolbar"), sel(b"alloc"))
            init_toolbar = _message(library, ctypes.c_void_p, ctypes.c_void_p)
            toolbar = init_toolbar(toolbar, sel(b"initWithIdentifier:"), identifier)
            send_object = _message(library, None, ctypes.c_void_p)
            send_object(ns_window, sel(b"setToolbar:"), toolbar)
            send_long(ns_window, sel(b"setToolbarStyle:"), 3)  # NSWindowToolbarStyleUnified
            send_long(ns_window, sel(b"setTitlebarSeparatorStyle:"), 1)  # NSTitlebarSeparatorStyleNone
        return True
    except (OSError, AttributeError, ValueError, TypeError):
        return False


def hide_title_text(window: QWidget) -> bool:
    """Backward-compatible name for ``style_title_bar``."""
    return style_title_bar(window)
