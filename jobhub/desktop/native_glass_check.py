"""Standalone macOS Liquid Glass compositor smoke check.

This diagnostic intentionally avoids Starployment's database, profile, controller,
and network paths. It creates synthetic Qt surfaces and captures the resulting
AppKit window through ``screencapture``.
"""
from __future__ import annotations

import json
import platform
import subprocess
import sys
import traceback
import ctypes
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QScrollArea, QVBoxLayout, QWidget,
)


def _write_result(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _unsupported_reason() -> str | None:
    if sys.platform != "darwin":
        return "Native Liquid Glass diagnostics require macOS."
    if QGuiApplication.platformName() != "cocoa":
        return "Native Liquid Glass diagnostics require the Cocoa Qt platform."
    version = platform.mac_ver()[0]
    try:
        major = int(version.split(".", 1)[0])
    except (TypeError, ValueError):
        return f"Could not determine macOS version (reported {version!r})."
    if major < 26:
        return f"NSGlassEffectView requires macOS 26 or newer (found {version})."
    return None


class _Check:
    def __init__(self, app: QApplication, output: Path):
        self.app = app
        self.output = output
        self.result: dict = {
            "diagnostic": "native_glass_check",
            "platform": platform.platform(),
            "python": platform.python_version(),
            "qt_platform": "cocoa",
            "status": "running",
            "captures": [],
            "checks": {},
        }
        self.window: QMainWindow | None = None
        self.dialog: QDialog | None = None
        self._stage = 0
        self._failed = False
        self.timer = QTimer(app)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._run_stage)
        self.watchdog = QTimer(app)
        self.watchdog.setSingleShot(True)
        self.watchdog.timeout.connect(lambda: self._fail("Timed out waiting for Cocoa glass diagnostic."))

    def run(self) -> int:
        from jobhub.desktop.native_glass import native_glass_available, prepare_window
        from jobhub.desktop.styles import apply_appearance
        from jobhub.desktop.theme import DARK, LIGHT
        from jobhub.desktop.widgets import AmbientBackdrop, CapsuleButton, GlassPanel, SegmentedControl, label

        if not native_glass_available():
            self.result.update(status="failed", reason="Native glass API is unavailable on a supported Cocoa host.")
            _write_result(self.output / "diagnostic.json", self.result)
            print(f"Native glass check failed: {self.result['reason']}", file=sys.stderr)
            return 1

        self._apply_appearance = apply_appearance
        self._light = LIGHT
        self._dark = DARK
        self._prepare_window = prepare_window
        self._GlassPanel = GlassPanel
        self._create_window(AmbientBackdrop, CapsuleButton, SegmentedControl, label)
        self.watchdog.start(30000)
        self.timer.start(1500)
        return int(self.app.exec())

    def _unsupported(self, reason: str) -> int:
        self.result.update(status="unsupported", reason=reason)
        _write_result(self.output / "diagnostic.json", self.result)
        print(f"Native glass check unsupported: {reason}", file=sys.stderr)
        return 2

    def _create_window(self, AmbientBackdrop, CapsuleButton, SegmentedControl, label) -> None:
        window = QMainWindow()
        window.setWindowTitle("Starployment Native Glass Diagnostic")
        window.resize(740, 500)
        root = AmbientBackdrop()
        window.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(30, 28, 30, 26)
        layout.setSpacing(16)
        title = QLabel("Native Liquid Glass")
        title.setStyleSheet("font-size: 25px; font-weight: 650; background: transparent;")
        subtitle = QLabel("Synthetic Cocoa compositor check · no candidate data")
        subtitle.setStyleSheet("font-size: 13px; background: transparent;")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        row = QHBoxLayout()
        row.setSpacing(16)
        first = self._GlassPanel(radius=18)
        first.setMinimumHeight(180)
        first_layout = QVBoxLayout(first)
        first_layout.setContentsMargins(20, 18, 20, 18)
        first_layout.addWidget(label("Material surface", "cardTitle"))
        first_layout.addWidget(label("AppKit NSGlassEffectView behind the Qt foreground."))
        segment = SegmentedControl(compact=True)
        segment.addItems(["Light", "Dark", "System"])
        first_layout.addWidget(segment, 0, Qt.AlignmentFlag.AlignLeft)
        first_layout.addStretch(1)

        second = self._GlassPanel(radius=16, tint="accent", tint_alpha=0.08)
        second.setMinimumHeight(180)
        second_layout = QVBoxLayout(second)
        second_layout.setContentsMargins(20, 18, 20, 18)
        second_layout.addWidget(label("Controls stay native to Qt", "cardTitle"))
        second_layout.addWidget(label("This button is synthetic and never submits data."))
        button = CapsuleButton("Diagnostic button", role="primary")
        second_layout.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
        second_layout.addStretch(1)
        row.addWidget(first, 1)
        row.addWidget(second, 1)
        layout.addLayout(row)

        clip_box = QScrollArea(root)
        clip_box.setObjectName("diagnosticClipBox")
        clip_box.setFixedSize(270, 74)
        clip_box.setFrameShape(QFrame.Shape.NoFrame)
        clip_box.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        clip_box.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        clip_box.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        content = QWidget()
        content.setObjectName("diagnosticClipContent")
        content.setFixedSize(455, 74)
        clipped = self._GlassPanel(content, radius=14)
        clipped.setGeometry(185, 0, 270, 74)
        clipped_label = QLabel("Partially clipped glass surface", clipped)
        clipped_label.setStyleSheet("background: transparent; padding: 16px;")
        clip_box.setWidget(content)
        layout.addWidget(clip_box, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        self.window = window
        self._main_surfaces = (first, second, clipped)
        self._clip_box = clip_box
        self._segment = segment
        self._apply_appearance(self.app, self._light)
        self._prepare_window(window)
        window.show()
        window.raise_()
        window.activateWindow()
        window.update()

    def _run_stage(self) -> None:
        try:
            if self._stage == 0:
                self._record_surfaces("light_main", self.window, self._main_surfaces)
                from jobhub.desktop.native_glass import visible_rect

                visible = visible_rect(self._main_surfaces[2], self.window)
                backend = getattr(self.window, "_jobhub_native_materials", None)
                native_surface = next(
                    surface for surface in backend.surfaces if surface.widget is self._main_surfaces[2]
                )
                native_frame = backend.runtime.bounds(native_surface.clipper, b"frame")
                native_clip_width = native_frame.size.width
                self.result["checks"]["clipping"] = {
                    "visible_width": visible.width(),
                    "surface_width": self._main_surfaces[2].width(),
                    "native_clip_width": native_clip_width,
                    "passed": (
                        0 < visible.width() < self._main_surfaces[2].width()
                        and abs(native_clip_width - visible.width()) < 1.0
                    ),
                }
                if not self.result["checks"]["clipping"]["passed"]:
                    raise RuntimeError("Native clipper frame does not match the partially visible widget bounds.")
                self.window.resize(820, 540)
                self._stage = 1
                self.timer.start(900)
                return
            if self._stage == 1:
                self._record_surfaces("light_resized", self.window, self._main_surfaces)
                self.result["checks"]["resize"] = {
                    "size": [self.window.width(), self.window.height()],
                    "native_surface_count": self._native_count(self.window),
                    "passed": self.window.size().width() == 820 and self._native_count(self.window) >= 3,
                }
                if not self.result["checks"]["resize"]["passed"]:
                    raise RuntimeError("Native materials did not survive window resize.")
                self._apply_appearance(self.app, self._dark)
                self._stage = 2
                self.timer.start(900)
                return
            if self._stage == 2:
                self._record_surfaces("dark_main", self.window, self._main_surfaces)
                captures = {item["file"]: item for item in self.result["captures"]}
                light_luminance = captures["light_main.png"]["mean_luminance"]
                dark_luminance = captures["dark_main.png"]["mean_luminance"]
                self.result["checks"]["theme"] = {
                    "light_mean_luminance": light_luminance,
                    "dark_mean_luminance": dark_luminance,
                    "passed": light_luminance > dark_luminance + 25,
                }
                if not self.result["checks"]["theme"]["passed"]:
                    raise RuntimeError("Light and dark compositor captures do not show distinct appearances.")
                self.dialog = QDialog(self.window)
                self.dialog.setWindowTitle("Glass dialog diagnostic")
                self.dialog.resize(430, 210)
                self._prepare_window(self.dialog)
                dialog_layout = QVBoxLayout(self.dialog)
                panel = self._GlassPanel(radius=18, material="thick")
                inner = QVBoxLayout(panel)
                inner.setContentsMargins(22, 20, 22, 20)
                inner.addWidget(QLabel("Generic QDialog glass surface", panel))
                inner.addWidget(QLabel("Close and reopen lifecycle check", panel))
                dialog_layout.addWidget(panel)
                dialog_layout.setContentsMargins(12, 12, 12, 12)
                self._dialog_panel = panel
                self.dialog.show()
                self._stage = 3
                self.timer.start(900)
                return
            if self._stage == 3:
                self._record_surfaces("dialog_first_open", self.dialog, (self._dialog_panel,))
                self.dialog.close()
                self.dialog = None
                self._stage = 4
                self.timer.start(500)
                return
            if self._stage == 4:
                self.dialog = QDialog(self.window)
                self.dialog.setWindowTitle("Glass dialog diagnostic reopened")
                self.dialog.resize(430, 210)
                self._prepare_window(self.dialog)
                dialog_layout = QVBoxLayout(self.dialog)
                panel = self._GlassPanel(radius=18, material="thick")
                inner = QVBoxLayout(panel)
                inner.setContentsMargins(22, 20, 22, 20)
                inner.addWidget(QLabel("Reopened QDialog glass surface", panel))
                dialog_layout.addWidget(panel)
                dialog_layout.setContentsMargins(12, 12, 12, 12)
                self._dialog_panel = panel
                self.dialog.show()
                self._stage = 5
                self.timer.start(900)
                return
            if self._stage == 5:
                self._record_surfaces("dialog_reopened", self.dialog, (self._dialog_panel,))
                self.result["checks"]["dialog_close_reopen"] = {"passed": True}
                self.dialog.close()
                self.window.close()
                self.result["status"] = "passed"
                self.watchdog.stop()
                _write_result(self.output / "diagnostic.json", self.result)
                self.app.quit()
        except Exception as error:  # noqa: BLE001
            self._fail(f"{type(error).__name__}: {error}", traceback.format_exc())

    def _native_count(self, widget: QWidget) -> int:
        from jobhub.desktop.native_glass import native_material_count

        return native_material_count(widget)

    def _record_surfaces(self, name: str, window: QWidget, widgets: tuple[QWidget, ...]) -> None:
        from jobhub.desktop.native_glass import native_material_count
        from jobhub.desktop.native import _message

        backend = getattr(window, "_jobhub_native_materials", None)
        runtime = getattr(backend, "runtime", None)
        expected_class = runtime.cls(b"NSGlassEffectView") if runtime else 0
        identity = _message(runtime.lib, ctypes.c_void_p) if runtime else None
        surfaces = []
        native_surfaces = list(backend.surfaces) if backend else []
        for surface in native_surfaces:
            widget = surface.widget
            pointer = surface.glass if surface and surface.active else 0
            class_name = ""
            is_glass = False
            if pointer and runtime:
                actual = identity(pointer, runtime.sel(b"class"))
                name_func = runtime.lib.class_getName
                name_func.argtypes = [ctypes.c_void_p]
                name_func.restype = ctypes.c_char_p
                class_name = (name_func(actual) or b"").decode("ascii", "replace")
                check = _message(runtime.lib, ctypes.c_bool, ctypes.c_void_p)
                is_glass = bool(check(pointer, runtime.sel(b"isKindOfClass:"), expected_class))
            surfaces.append({
                "widget": widget.objectName() or widget.__class__.__name__,
                "visible": bool(surface and surface.active),
                "objc_class": class_name,
                "is_ns_glass_effect_view": is_glass,
            })
        active_count = native_material_count(window)
        passed = (
            bool(surfaces)
            and active_count >= len(widgets)
            and all(item["is_ns_glass_effect_view"] for item in surfaces)
            and all(
                widget._native_glass is not None and widget._native_glass.active
                for widget in widgets
            )
        )
        self.result["checks"][name] = {
            "native_surface_count": active_count,
            "surfaces": surfaces,
            "passed": passed,
        }
        if not passed:
            raise RuntimeError(f"{name}: expected actual NSGlassEffectView surfaces; got {surfaces!r}")
        path = self.output / f"{name}.png"
        self._capture(window, path)

    def _capture(self, window: QWidget, path: Path) -> None:
        backend = getattr(window, "_jobhub_native_materials", None)
        if backend is None or not backend.active:
            raise RuntimeError("Window native material host is not active for compositor capture.")
        number = backend.runtime.get_long(backend.ns_window, backend.runtime.sel(b"windowNumber"))
        if not number:
            raise RuntimeError("AppKit returned no window number for compositor capture.")
        completed = subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-l", str(number), str(path)],
            capture_output=True, text=True, timeout=12, check=False,
        )
        if completed.returncode != 0 or not path.is_file():
            raise RuntimeError(f"screencapture failed ({completed.returncode}): {completed.stderr.strip()}")
        image = QImage(str(path))
        if image.isNull() or image.width() < 100 or image.height() < 100:
            raise RuntimeError(f"Compositor capture is empty or invalid: {path}")
        sample = image.scaled(64, 64, Qt.AspectRatioMode.IgnoreAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
        luminance = 0.0
        for y in range(sample.height()):
            for x in range(sample.width()):
                color = sample.pixelColor(x, y)
                luminance += 0.2126 * color.red() + 0.7152 * color.green() + 0.0722 * color.blue()
        luminance /= sample.width() * sample.height()
        self.result["captures"].append({
            "file": path.name,
            "width": image.width(),
            "height": image.height(),
            "mean_luminance": round(luminance, 2),
        })

    def _fail(self, message: str, details: str = "") -> None:
        if self._failed:
            return
        self._failed = True
        self.timer.stop()
        self.watchdog.stop()
        self.result.update(status="failed", reason=message)
        if details:
            self.result["traceback"] = details
        for widget in (self.dialog, self.window):
            try:
                if widget is not None:
                    widget.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            _write_result(self.output / "diagnostic.json", self.result)
        except Exception:  # noqa: BLE001
            pass
        print(f"Native glass check failed: {message}", file=sys.stderr)
        self.app.exit(1)


def run(directory: str | Path) -> int:
    output = Path(directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication(["jobhub-native-glass-check"])
    app.setApplicationName("Starployment Native Glass Diagnostic")
    reason = _unsupported_reason()
    if reason:
        result = {
            "diagnostic": "native_glass_check",
            "platform": platform.platform(),
            "qt_platform": QGuiApplication.platformName(),
            "status": "unsupported",
            "reason": reason,
            "captures": [],
            "checks": {},
        }
        _write_result(output / "diagnostic.json", result)
        print(f"Native glass check unsupported: {reason}", file=sys.stderr)
        return 2
    try:
        return _Check(app, output).run()
    except Exception as error:  # noqa: BLE001
        result = {
            "diagnostic": "native_glass_check",
            "platform": platform.platform(),
            "qt_platform": "cocoa",
            "status": "failed",
            "reason": f"{type(error).__name__}: {error}",
            "traceback": traceback.format_exc(),
            "captures": [],
            "checks": {},
        }
        _write_result(output / "diagnostic.json", result)
        print(f"Native glass check failed: {result['reason']}", file=sys.stderr)
        return 1
