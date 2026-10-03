import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from jobhub.desktop import icon, native_windows
from jobhub.desktop.styles import apply_appearance


def test_window_icon_prefers_ico_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert icon.window_icon_path().name == "starployment.ico"
    # The sidebar mark loads icon_path() as a pixmap and must stay the 256px PNG.
    assert icon.icon_path().name == "starployment-icon-256.png"


def test_window_icon_uses_png_elsewhere(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert icon.window_icon_path().name == "starployment-icon-256.png"


def test_title_bar_sync_is_a_noop_offscreen():
    app = QApplication.instance() or QApplication([])
    window = QWidget()
    window.show()
    # Offscreen is not the native Windows platform: nothing to set, nothing raised.
    native_windows.set_dark_title_bar(window, True)
    native_windows.sync_title_bars(app)
    apply_appearance(app, "Dark")
    apply_appearance(app, "System")
    window.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows taskbar identity")
def test_app_user_model_id_is_set():
    import ctypes

    native_windows.set_app_user_model_id()
    value = ctypes.c_wchar_p()
    ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(value))
    assert value.value == native_windows.APP_USER_MODEL_ID
