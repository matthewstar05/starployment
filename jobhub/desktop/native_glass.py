"""AppKit Liquid Glass materials under the Qt foreground on macOS 26+.

Qt keeps one backing store and owns input, focus, and layout. Reparenting each
QWidget's NSView into NSGlassEffectView corrupts Qt's layout/backing-store
coordinates. Instead, a window-sized AppKit material plane follows the Qt
widgets, with NSGlassEffectContainerView batching the real glass surfaces.
Only public AppKit selectors are used. Other platforms and offscreen rendering
never create native views and retain the painted presentation.
"""
from __future__ import annotations

import ctypes
import logging
import os
import platform
import sys
from collections.abc import Callable
from functools import lru_cache, partial

import shiboken6

from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import QApplication, QAbstractScrollArea, QGraphicsOpacityEffect, QWidget

from .native import _message, _objc
from .theme import DARK, current_colors, current_scheme

log = logging.getLogger(__name__)


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _Size(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _Rect(ctypes.Structure):
    _fields_ = [("origin", _Point), ("size", _Size)]


def _rect(x: float, y: float, width: float, height: float) -> _Rect:
    return _Rect(_Point(x, y), _Size(width, height))


class _Runtime:
    def __init__(self):
        self.lib = _objc()
        self.lib.objc_getClass.argtypes = [ctypes.c_char_p]
        self.lib.objc_getClass.restype = ctypes.c_void_p
        self.cls = self.lib.objc_getClass
        self.sel = self.lib.sel_registerName
        self.object = _message(self.lib, ctypes.c_void_p)
        self.set_object = _message(self.lib, None, ctypes.c_void_p)
        self.set_long = _message(self.lib, None, ctypes.c_long)
        self.set_bool = _message(self.lib, None, ctypes.c_bool)
        self.set_double = _message(self.lib, None, ctypes.c_double)
        self.set_rect = _message(self.lib, None, _Rect)
        self.init_rect = _message(self.lib, ctypes.c_void_p, _Rect)
        self.release = _message(self.lib, None)
        self.get_long = _message(self.lib, ctypes.c_long)
        self.color = _message(self.lib, ctypes.c_void_p, *([ctypes.c_double] * 4))
        if platform.machine() == "x86_64":
            # NSRect is 32 bytes. Intel objc_msgSend uses the stret entry point.
            address = ctypes.cast(self.lib.objc_msgSend_stret, ctypes.c_void_p).value
            self._get_rect = ctypes.CFUNCTYPE(
                None, ctypes.POINTER(_Rect), ctypes.c_void_p, ctypes.c_void_p,
            )(address)
        else:
            self._get_rect = _message(self.lib, _Rect)

    def bounds(self, view, selector: bytes = b"bounds") -> _Rect:
        if platform.machine() == "x86_64":
            result = _Rect()
            self._get_rect(ctypes.byref(result), view, self.sel(selector))
            return result
        return self._get_rect(view, self.sel(selector))

    def view(self, name: bytes, frame: _Rect):
        allocated = self.object(self.cls(name), self.sel(b"alloc"))
        view = self.init_rect(allocated, self.sel(b"initWithFrame:"), frame)
        if not view:
            raise RuntimeError(f"Could not create {name.decode()}")
        return view

    def appearance(self):
        name = b"NSAppearanceNameDarkAqua" if current_scheme() == DARK else b"NSAppearanceNameAqua"
        string = _message(self.lib, ctypes.c_void_p, ctypes.c_char_p)(
            self.cls(b"NSString"), self.sel(b"stringWithUTF8String:"), name,
        )
        return _message(self.lib, ctypes.c_void_p, ctypes.c_void_p)(
            self.cls(b"NSAppearance"), self.sel(b"appearanceNamed:"), string,
        )


@lru_cache(maxsize=1)
def _runtime() -> _Runtime | None:
    try:
        runtime = _Runtime()
        if runtime.cls(b"NSGlassEffectView") and runtime.cls(b"NSGlassEffectContainerView"):
            return runtime
    except (OSError, AttributeError, ValueError, TypeError):
        log.exception("Native Liquid Glass is unavailable")
    return None


def native_glass_available() -> bool:
    if sys.platform != "darwin" or QGuiApplication.platformName() != "cocoa":
        return False
    if os.environ.get("JOBHUB_NATIVE_GLASS", "1").strip().lower() in {"0", "false", "off", "no"}:
        return False
    return _runtime() is not None


def prepare_window(window: QWidget) -> None:
    """Request an alpha backing store before Qt creates the Cocoa window."""
    if native_glass_available():
        window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        window.setProperty("jobhubNativeGlassPrepared", True)


def window_has_native_glass(widget: QWidget) -> bool:
    return bool(widget.window().property("jobhubNativeGlass"))


def refresh_window_materials(widget: QWidget) -> None:
    backend = getattr(widget.window(), "_jobhub_native_materials", None)
    if backend is not None:
        backend.schedule()


def visible_rect(widget: QWidget, window: QWidget) -> QRectF:
    """Clip a material to its Qt ancestors, including scroll-area viewports."""
    origin = widget.mapTo(window, QPoint(0, 0))
    result = QRectF(origin.x(), origin.y(), widget.width(), widget.height())
    ancestor = widget.parentWidget()
    while ancestor is not None:
        point = ancestor.mapTo(window, QPoint(0, 0))
        result = result.intersected(QRectF(point.x(), point.y(), ancestor.width(), ancestor.height()))
        if ancestor is window:
            break
        ancestor = ancestor.parentWidget()
    return result


# NSView autoresizing masks. Native frames use a bottom-left origin while Qt
# lays out from the top-left; MinYMargin keeps a view's distance from the top
# edge while AppKit resizes the window, so nothing jumps before a resync.
_NS_VIEW_MIN_Y_MARGIN = 8
# Room around each material for the soft shadow AppKit draws outside its shape.
# Zero: neighboring margins overlap and AppKit batches them into visible blocks.
SHADOW_MARGIN = 0


class _WindowHook(QObject):
    """Object-level filter on the window and its QWindow.

    The application-wide filter sees a top-level Resize before Qt's layouts
    run. Object filters run after them, so syncing here places native glass
    at the new layout in the same frame. UpdateRequest arrives just before Qt
    repaints, which keeps animations and scrolling in step with the glass.
    """

    def __init__(self, backend: "_WindowMaterials"):
        super().__init__(backend)
        self.backend = backend

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        backend = self.backend
        if kind == QEvent.Type.Resize and watched is backend.window:
            backend.refresh()
        elif kind in (QEvent.Type.UpdateRequest, QEvent.Type.Expose, QEvent.Type.Paint) and backend._pending:
            backend.refresh()
        return False


class _WindowMaterials(QObject):
    """One native plane per window, below Qt's transparent foreground view."""

    def __init__(self, window: QWidget):
        super().__init__(window)
        self.window = window
        self.runtime = runtime = _runtime()
        self.surfaces: list[GlassSurface] = []
        self._pending = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.refresh)
        self._appearance = None
        self.active = False
        self.host = self.base = self.group = self.plane = self.clipped_plane = None
        # Never raise from here: an exception after QObject construction leaves
        # Qt holding a parented object whose Python wrapper is already gone,
        # which crashes when Qt later deletes it. Callers check ``active``.
        if runtime is None:
            return
        try:
            self._install()
        except Exception:  # noqa: BLE001
            log.exception("Could not install native Liquid Glass")
            self._release_views()
            return
        self.active = True
        window.setProperty("jobhubNativeGlass", True)
        window.update()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            app.aboutToQuit.connect(self.detach)
        self._hook = _WindowHook(self)
        window.installEventFilter(self._hook)
        handle = window.windowHandle()
        if handle is not None:
            handle.installEventFilter(self._hook)
        self.refresh()

    def _install(self) -> None:
        window, runtime = self.window, self.runtime
        self.qt_view = int(window.winId())
        self.ns_window = runtime.object(self.qt_view, runtime.sel(b"window"))
        if not self.ns_window:
            raise RuntimeError("The Qt window has no Cocoa window")
        self.parent_view = runtime.object(self.qt_view, runtime.sel(b"superview"))
        if not self.parent_view:
            raise RuntimeError("The Cocoa foreground has no parent view")
        outer = runtime.bounds(self.qt_view, b"frame")
        frame = _rect(0, 0, outer.size.width, outer.size.height)
        self.host = runtime.view(b"NSView", outer)
        self.base = runtime.view(b"NSVisualEffectView", frame)
        self.group = runtime.view(b"NSGlassEffectContainerView", frame)
        self.plane = runtime.view(b"NSView", frame)
        self.clipped_plane = runtime.view(b"NSView", frame)
        for view in (self.host, self.base, self.group, self.plane, self.clipped_plane):
            runtime.set_long(view, runtime.sel(b"setAutoresizingMask:"), 18)
        runtime.set_long(self.base, runtime.sel(b"setMaterial:"), 21)  # underWindowBackground
        runtime.set_long(self.base, runtime.sel(b"setBlendingMode:"), 0)  # behindWindow
        runtime.set_long(self.base, runtime.sel(b"setState:"), 0)  # followsWindowActiveState
        runtime.set_double(self.group, runtime.sel(b"setSpacing:"), 0.0)
        runtime.set_object(self.group, runtime.sel(b"setContentView:"), self.plane)
        runtime.set_object(self.host, runtime.sel(b"addSubview:"), self.base)
        runtime.set_object(self.host, runtime.sel(b"addSubview:"), self.group)
        # Container batching elevates descendants above intermediate masks.
        # Scroll materials need their own unbatched ancestry so clipping works.
        runtime.set_object(self.host, runtime.sel(b"addSubview:"), self.clipped_plane)
        add_below = _message(runtime.lib, None, ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p)
        add_below(self.parent_view, runtime.sel(b"addSubview:positioned:relativeTo:"), self.host, -1, self.qt_view)
        runtime.set_bool(self.ns_window, runtime.sel(b"setOpaque:"), False)
        clear = runtime.object(runtime.cls(b"NSColor"), runtime.sel(b"clearColor"))
        runtime.set_object(self.ns_window, runtime.sel(b"setBackgroundColor:"), clear)

    def _release_views(self) -> None:
        runtime = self.runtime
        if self.host:
            runtime.release(self.host, runtime.sel(b"removeFromSuperview"))
        for name in ("clipped_plane", "plane", "group", "base", "host"):
            view = getattr(self, name, None)
            if view:
                runtime.release(view, runtime.sel(b"release"))
                setattr(self, name, None)

    def eventFilter(self, watched, event) -> bool:
        if isinstance(watched, QWidget) and watched.window() is self.window:
            if event.type() in {
                QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide,
                QEvent.Type.LayoutRequest, QEvent.Type.PaletteChange, QEvent.Type.StyleChange,
                QEvent.Type.ZOrderChange, QEvent.Type.WindowStateChange,
                QEvent.Type.EnabledChange,
            }:
                self.schedule()
            if watched is self.window and event.type() in {QEvent.Type.Close, QEvent.Type.DeferredDelete}:
                self.detach()
        return False

    def schedule(self) -> None:
        if self.active and not self._pending:
            self._pending = True
            self._timer.start(0)

    def refresh(self) -> None:
        self._pending = False
        self._timer.stop()
        if not self.active:
            return
        if not shiboken6.isValid(self.window):
            self.detach()
            return
        runtime = self.runtime
        runtime.set_rect(self.host, runtime.sel(b"setFrame:"), runtime.bounds(self.qt_view, b"frame"))
        if self._appearance != current_scheme():
            self._appearance = current_scheme()
            appearance = runtime.appearance()
            runtime.set_object(self.host, runtime.sel(b"setAppearance:"), appearance)
            runtime.set_object(self.ns_window, runtime.sel(b"setAppearance:"), appearance)
            for view in (self.base, self.group, self.plane, self.clipped_plane):
                runtime.set_object(view, runtime.sel(b"setAppearance:"), appearance)
        height = runtime.bounds(self.plane).size.height
        for surface in list(self.surfaces):
            if not shiboken6.isValid(surface) or not shiboken6.isValid(surface.widget):
                # The widget died with its parent, which sends no DeferredDelete
                # to it. Release its native view so no orphan glass stays behind.
                _release_native(surface._native)
                continue
            try:
                surface._sync(height)
            except RuntimeError:   # deleted mid-sync by a nested event
                _release_native(surface._native)

    def detach(self) -> None:
        if not self.active:
            return
        self.active = False
        self._timer.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        for surface in list(self.surfaces):
            if shiboken6.isValid(surface):
                surface._remove()
            else:
                _release_native(surface._native)
        self.window.setProperty("jobhubNativeGlass", False)
        self._release_views()


def _release_native(native: dict, *_args) -> None:
    """Remove a surface's AppKit views. Safe to call twice, and after Qt objects die."""
    backend, clipper, glass = native.get("backend"), native.get("clipper"), native.get("glass")
    native["clipper"] = native["glass"] = None
    if backend is not None:
        backend.surfaces[:] = [s for s in backend.surfaces if getattr(s, "_native", None) is not native]
        if clipper:
            runtime = backend.runtime
            runtime.release(clipper, runtime.sel(b"removeFromSuperview"))
            if glass:
                runtime.release(glass, runtime.sel(b"release"))
            runtime.release(clipper, runtime.sel(b"release"))


class GlassSurface(QObject):
    """A real NSGlassEffectView whose geometry follows an ordinary QWidget."""

    def __init__(self, widget: QWidget, radius: float | Callable[[], float], *,
                 rect: Callable[[], QRectF] | None = None, tint: Callable[[], tuple[str, float] | None] | None = None,
                 opacity: Callable[[], float] | None = None):
        super().__init__(widget)
        self.widget = widget
        self.radius = radius
        self.rect = rect
        self.tint = tint
        self.opacity = opacity
        self.backend: _WindowMaterials | None = None
        self.glass = self.clipper = None
        self._signature = None
        self._hidden = False
        self._scroll_clipped = False
        self._native_parent = None
        # Shared with a plain function so the native views are released even
        # when this QObject is destroyed together with its widget.
        self._native: dict = {"backend": None, "clipper": None, "glass": None}
        if native_glass_available():
            widget.installEventFilter(self)
            self.destroyed.connect(partial(_release_native, self._native))

    @property
    def active(self) -> bool:
        return bool(self.glass and self.backend and self.backend.active)

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.Show:
            self._attach()
        elif event.type() in {QEvent.Type.DeferredDelete, QEvent.Type.ParentAboutToChange}:
            self._remove()
        return False

    def _attach(self) -> None:
        if self.active or not native_glass_available() or not self.widget.isVisible():
            return
        window = self.widget.window()
        if not window.property("jobhubNativeGlassPrepared"):
            return  # Call prepare_window before the window's first winId/show.
        try:
            backend = getattr(window, "_jobhub_native_materials", None)
            if backend is None or not backend.active:
                if window.property("jobhubNativeGlassFailed"):
                    return   # keep the painted fallback instead of retrying every Show
                backend = _WindowMaterials(window)
                if not backend.active:
                    window.setProperty("jobhubNativeGlassFailed", True)
                    # Unparent so Python frees it now; a deferred delete could
                    # outlive the window and double-free during teardown.
                    backend.setParent(None)
                    del backend
                    raise RuntimeError("Native Liquid Glass could not be installed")
                window._jobhub_native_materials = backend
            self.backend = backend
            runtime = backend.runtime
            self.clipper = runtime.view(b"NSView", _rect(0, 0, 1, 1))
            runtime.set_long(self.clipper, runtime.sel(b"setAutoresizingMask:"), _NS_VIEW_MIN_Y_MARGIN)
            runtime.set_bool(self.clipper, runtime.sel(b"setClipsToBounds:"), True)
            runtime.set_bool(self.clipper, runtime.sel(b"setWantsLayer:"), True)
            layer = runtime.object(self.clipper, runtime.sel(b"layer"))
            runtime.set_bool(layer, runtime.sel(b"setMasksToBounds:"), True)
            self.glass = runtime.view(b"NSGlassEffectView", _rect(0, 0, 1, 1))
            runtime.set_long(self.glass, runtime.sel(b"setStyle:"), 0)
            runtime.set_object(self.clipper, runtime.sel(b"addSubview:"), self.glass)
            ancestor = self.widget
            clipped = False
            while ancestor is not None and ancestor is not window:
                if isinstance(ancestor, QAbstractScrollArea):
                    clipped = True
                    break
                ancestor = ancestor.parentWidget()
            native_parent = backend.clipped_plane if clipped else backend.plane
            runtime.set_object(native_parent, runtime.sel(b"addSubview:"), self.clipper)
            self._scroll_clipped = clipped
            self._native_parent = native_parent
            self._native.update(backend=backend, clipper=self.clipper, glass=self.glass)
            backend.surfaces.append(self)
            self.widget.setProperty("nativeGlass", True)
            self.widget.style().unpolish(self.widget)
            self.widget.style().polish(self.widget)
            self.widget.update()
            backend.schedule()
        except (OSError, AttributeError, ValueError, TypeError, RuntimeError):
            log.exception("Could not attach native Liquid Glass")
            self._remove()

    def refresh(self) -> None:
        self._signature = None
        if self.backend:
            self.backend.schedule()

    def _sync(self, height: float) -> None:
        if not self.active:
            return
        widget, backend = self.widget, self.backend
        runtime = backend.runtime
        window = backend.window
        visible = widget.isVisible() and widget.window() is window
        clip = QRectF()
        if visible:
            origin = widget.mapTo(window, QPoint(0, 0))
            body = QRectF(self.rect() if self.rect else widget.rect())
            body.translate(origin.x(), origin.y())
            parent = widget.parentWidget()
            ancestors = (visible_rect(parent, window) if parent is not None and widget is not window
                         else QRectF(0, 0, window.width(), window.height()))
            if not body.intersected(ancestors).isEmpty():
                # Leave room for the material's own soft shadow. A clipper the
                # exact size of the widget cuts that shadow into a visible
                # rectangle. Ancestor bounds (scroll viewports, panels) still clip.
                clip = body.adjusted(-SHADOW_MARGIN, -SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN).intersected(ancestors)
        if not visible or clip.isEmpty():
            if not self._hidden:
                runtime.set_bool(self.clipper, runtime.sel(b"setHidden:"), True)
                self._hidden = True
            return
        if self._hidden:
            runtime.set_bool(self.clipper, runtime.sel(b"setHidden:"), False)
            self._hidden = False
        expanded = body.adjusted(-SHADOW_MARGIN, -SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN)
        native_parent = backend.clipped_plane if self._scroll_clipped or not clip.contains(expanded) else backend.plane
        if self._native_parent != native_parent:
            runtime.set_object(native_parent, runtime.sel(b"addSubview:"), self.clipper)
            self._native_parent = native_parent
        radius = self.radius() if callable(self.radius) else self.radius
        tint = self.tint() if self.tint else None
        opacity = max(0.0, min(1.0, self.opacity() if self.opacity else 1.0))
        ancestor = widget
        while ancestor is not None:
            effect = ancestor.graphicsEffect()
            if isinstance(effect, QGraphicsOpacityEffect) and effect.isEnabled():
                opacity *= effect.opacity()
            if ancestor is backend.window:
                break
            ancestor = ancestor.parentWidget()
        signature = (tuple((r.x(), r.y(), r.width(), r.height()) for r in (clip, body)),
                     radius, tint, opacity, current_scheme(), height)
        if signature == self._signature:
            return
        self._signature = signature
        runtime.set_object(self.glass, runtime.sel(b"setAppearance:"), runtime.appearance())
        runtime.set_rect(self.clipper, runtime.sel(b"setFrame:"),
                         _rect(clip.x(), height - clip.bottom(), clip.width(), clip.height()))
        runtime.set_rect(self.glass, runtime.sel(b"setFrame:"),
                         _rect(body.x() - clip.x(), clip.bottom() - body.bottom(), body.width(), body.height()))
        runtime.set_double(self.glass, runtime.sel(b"setCornerRadius:"), radius)
        runtime.set_double(self.glass, runtime.sel(b"setAlphaValue:"), opacity)
        color = None
        if tint:
            token, alpha = tint
            value = QColor(current_colors()[token])
            color = runtime.color(runtime.cls(b"NSColor"), runtime.sel(b"colorWithSRGBRed:green:blue:alpha:"),
                                  value.redF(), value.greenF(), value.blueF(), alpha)
        runtime.set_object(self.glass, runtime.sel(b"setTintColor:"), color)

    def _remove(self) -> None:
        _release_native(self._native)
        if self.backend and self in self.backend.surfaces:
            self.backend.surfaces.remove(self)
        self.glass = self.clipper = None
        self._native_parent = None
        self._signature = None
        self._hidden = False
        if shiboken6.isValid(self.widget):
            self.widget.setProperty("nativeGlass", False)


def native_material_count(window: QWidget) -> int:
    backend = getattr(window, "_jobhub_native_materials", None)
    return sum(surface.active for surface in backend.surfaces) if backend and backend.active else 0
