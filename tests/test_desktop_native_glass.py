"""Synthetic tests for the optional Cocoa Liquid Glass bridge."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtWidgets import QApplication, QGraphicsOpacityEffect, QScrollArea, QWidget

from jobhub.desktop import native_glass


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize(
    ("host_platform", "qt_platform", "disabled"),
    [
        ("linux", "cocoa", None),
        ("darwin", "offscreen", None),
        ("darwin", "cocoa", "0"),
    ],
)
def test_platform_guards_do_not_initialize_objc(
    monkeypatch, app, host_platform, qt_platform, disabled
):
    monkeypatch.setattr(native_glass.sys, "platform", host_platform)
    monkeypatch.setattr(
        native_glass.QGuiApplication, "platformName", staticmethod(lambda: qt_platform)
    )
    if disabled is not None:
        monkeypatch.setenv("JOBHUB_NATIVE_GLASS", disabled)

    def unexpected_runtime():
        pytest.fail("platform/feature guard should run before creating the ObjC runtime")

    monkeypatch.setattr(native_glass, "_runtime", unexpected_runtime)
    assert native_glass.native_glass_available() is False

    window = QWidget()
    native_glass.prepare_window(window)
    assert not window.property("jobhubNativeGlassPrepared")
    assert not window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    window.close()


def test_prepare_window_requests_alpha_only_when_native_runtime_is_available(monkeypatch, app):
    monkeypatch.setattr(native_glass, "native_glass_available", lambda: True)
    window = QWidget()

    native_glass.prepare_window(window)

    assert window.property("jobhubNativeGlassPrepared") is True
    assert window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    window.close()


def test_visible_rect_clips_to_widget_ancestors(app):
    window = QWidget()
    window.resize(400, 300)
    parent = QWidget(window)
    parent.setGeometry(20, 30, 100, 80)
    child = QWidget(parent)
    child.setGeometry(70, 60, 80, 40)

    assert native_glass.visible_rect(child, window) == QRectF(90, 90, 30, 20)
    window.close()


def test_visible_rect_tracks_scroll_viewport_clipping(app):
    window = QWidget()
    window.resize(300, 180)
    scroll = QScrollArea(window)
    scroll.setGeometry(20, 20, 100, 100)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    content = QWidget()
    content.resize(300, 200)
    child = QWidget(content)
    child.setGeometry(80, 20, 40, 20)
    scroll.setWidget(content)

    window.show()
    app.processEvents()
    viewport = scroll.viewport()
    child_rect = QRectF(child.mapTo(window, child.rect().topLeft()), child.size())
    viewport_rect = QRectF(viewport.mapTo(window, viewport.rect().topLeft()), viewport.size())
    expected = child_rect.intersected(viewport_rect)
    assert native_glass.visible_rect(child, window) == expected
    assert expected.width() < child.width()

    scroll.horizontalScrollBar().setValue(50)
    app.processEvents()
    child_rect = QRectF(child.mapTo(window, child.rect().topLeft()), child.size())
    viewport_rect = QRectF(viewport.mapTo(window, viewport.rect().topLeft()), viewport.size())
    expected = child_rect.intersected(viewport_rect)
    assert native_glass.visible_rect(child, window) == expected
    assert expected.width() == child.width()
    window.close()


class _FakeRuntime:
    """Small ObjC-shaped runtime for exercising view allocation and geometry."""

    def __init__(self, window: QWidget):
        self.window = window
        self.lib = object()
        self._next = 1000
        self.frames = {}
        self.kinds = {}
        self.calls = []
        self.fail_on_selector = None

    def cls(self, name):
        return name

    def sel(self, name):
        return name

    def object(self, receiver, selector):
        self.calls.append(("object", receiver, selector))
        if selector == b"window":
            return 501
        if selector == b"superview":
            return 502
        if selector == b"layer":
            return 503
        if selector == b"clearColor":
            return 504
        return None

    def view(self, name, frame):
        self._next += 1
        self.kinds[self._next] = name
        self.frames[self._next] = frame
        self.calls.append(("view", name, self._next))
        return self._next

    def bounds(self, view, selector=b"bounds"):
        if selector == b"frame" and view not in self.frames:
            return native_glass._rect(0, 0, self.window.width(), self.window.height())
        return self.frames[view]

    def set_object(self, view, selector, value):
        self.calls.append(("set_object", view, selector, value))

    def set_long(self, view, selector, value):
        self.calls.append(("set_long", view, selector, value))

    def set_bool(self, view, selector, value):
        self.calls.append(("set_bool", view, selector, value))
        if selector == self.fail_on_selector:
            raise RuntimeError("synthetic native view setup failure")

    def set_double(self, view, selector, value):
        self.calls.append(("set_double", view, selector, value))

    def set_rect(self, view, selector, frame):
        self.frames[view] = frame
        self.calls.append(("set_rect", view, selector, frame))

    def release(self, view, selector):
        self.calls.append(("release", view, selector))

    def color(self, *components):
        value = tuple(components)
        self.calls.append(("color", value))
        return value

    def appearance(self):
        value = native_glass.current_scheme()
        self.calls.append(("appearance", value))
        return value


def test_native_surface_lifecycle_tracks_geometry_opacity_and_theme(monkeypatch, app):
    window = QWidget()
    window.resize(300, 200)
    parent = QWidget(window)
    parent.setGeometry(10, 15, 100, 100)
    fade = QGraphicsOpacityEffect(parent)
    fade.setOpacity(0.5)
    parent.setGraphicsEffect(fade)
    child = QWidget(parent)
    child.setGeometry(60, 75, 80, 50)
    runtime = _FakeRuntime(window)
    scheme = ["Light"]
    callback_rect = QRectF(2, 3, 50, 40)

    monkeypatch.setattr(native_glass, "_runtime", lambda: runtime)
    monkeypatch.setattr(native_glass, "native_glass_available", lambda: True)
    monkeypatch.setattr(native_glass, "current_scheme", lambda: scheme[0])
    monkeypatch.setattr(
        native_glass, "current_colors",
        lambda: {"accent": "#123456" if scheme[0] == "Light" else "#654321"},
    )

    def fake_message(_library, _restype, *_argtypes):
        return lambda *args: runtime.calls.append(("message", *args))

    monkeypatch.setattr(native_glass, "_message", fake_message)
    native_glass.prepare_window(window)
    window.show()
    child.show()
    app.processEvents()

    surface = native_glass.GlassSurface(
        child, 12, rect=lambda: callback_rect,
        tint=lambda: ("accent", 0.25), opacity=lambda: 0.8
    )
    surface._attach()
    backend = surface.backend
    assert backend is not None and backend.active
    backend.refresh()
    assert surface.active
    assert native_glass.native_material_count(window) == 1

    clip_frame = runtime.frames[surface.clipper]
    glass_frame = runtime.frames[surface.glass]
    assert (clip_frame.origin.x, clip_frame.origin.y) == (70, 85)
    assert (clip_frame.size.width, clip_frame.size.height) == (40, 25)
    assert (glass_frame.origin.x, glass_frame.origin.y) == (2, -18)
    assert (glass_frame.size.width, glass_frame.size.height) == (50, 40)
    assert callback_rect == QRectF(2, 3, 50, 40)
    assert ("set_double", surface.glass, b"setAlphaValue:", 0.4) in runtime.calls
    assert surface._native_parent is backend.clipped_plane
    assert ("set_object", backend.host, b"addSubview:", backend.clipped_plane) in runtime.calls
    assert ("set_object", backend.clipped_plane, b"addSubview:", surface.clipper) in runtime.calls

    old_frame_count = sum(call[0] == "set_rect" and call[1] == surface.glass for call in runtime.calls)
    child.move(55, 70)
    backend.refresh()
    new_frame_count = sum(call[0] == "set_rect" and call[1] == surface.glass for call in runtime.calls)
    assert new_frame_count > old_frame_count

    scheme[0] = "Dark"
    surface.refresh()
    backend.refresh()
    assert ("appearance", "Dark") in runtime.calls
    assert ("set_object", backend.clipped_plane, b"setAppearance:", "Dark") in runtime.calls
    assert any(call[0] == "color" and call[1][2:5] == pytest.approx((101 / 255, 67 / 255, 33 / 255))
               for call in runtime.calls)

    clipped_plane = backend.clipped_plane
    backend.detach()
    assert not backend.active
    assert native_glass.native_material_count(window) == 0
    assert window.property("jobhubNativeGlass") is False
    assert any(call[0] == "release" for call in runtime.calls)
    assert any(call[:3] == ("release", clipped_plane, b"release") for call in runtime.calls)
    window.close()


def test_partial_native_installation_rolls_back_every_allocated_view(monkeypatch, app):
    window = QWidget()
    window.resize(300, 200)
    child = QWidget(window)
    child.setGeometry(10, 20, 80, 40)
    runtime = _FakeRuntime(window)
    runtime.fail_on_selector = b"setOpaque:"

    monkeypatch.setattr(native_glass, "_runtime", lambda: runtime)
    monkeypatch.setattr(native_glass, "native_glass_available", lambda: True)
    monkeypatch.setattr(native_glass, "current_scheme", lambda: "Light")
    monkeypatch.setattr(native_glass, "current_colors", lambda: {"accent": "#123456"})
    monkeypatch.setattr(
        native_glass, "_message",
        lambda *_args: lambda *args: runtime.calls.append(("message", *args)),
    )
    native_glass.prepare_window(window)
    window.show()
    child.show()
    app.processEvents()

    surface = native_glass.GlassSurface(child, 8)
    surface._attach()

    allocated = {call[2] for call in runtime.calls if call[0] == "view"}
    released = {call[1] for call in runtime.calls if call[0] == "release" and call[2] == b"release"}
    assert len(allocated) == 5
    assert released == allocated
    assert any(call[0] == "release" and call[2] == b"removeFromSuperview" for call in runtime.calls)
    assert surface.backend is None
    assert not window.property("jobhubNativeGlass")
    assert getattr(window, "_jobhub_native_materials", None) is None
    window.close()


def test_scroll_surface_uses_unbatched_clipped_plane(monkeypatch, app):
    window = QWidget()
    window.resize(300, 200)
    scroll = QScrollArea(window)
    scroll.setGeometry(20, 20, 100, 100)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    content = QWidget()
    content.resize(300, 200)
    child = QWidget(content)
    child.setGeometry(10, 10, 30, 20)
    scroll.setWidget(content)
    runtime = _FakeRuntime(window)

    monkeypatch.setattr(native_glass, "_runtime", lambda: runtime)
    monkeypatch.setattr(native_glass, "native_glass_available", lambda: True)
    monkeypatch.setattr(native_glass, "current_scheme", lambda: "Light")
    monkeypatch.setattr(native_glass, "current_colors", lambda: {"accent": "#123456"})
    monkeypatch.setattr(
        native_glass, "_message",
        lambda *_args: lambda *args: runtime.calls.append(("message", *args)),
    )
    native_glass.prepare_window(window)
    window.show()
    app.processEvents()

    surface = native_glass.GlassSurface(child, 8)
    surface._attach()
    backend = surface.backend
    assert backend is not None and backend.active
    backend.refresh()

    assert surface._scroll_clipped
    assert surface._native_parent is backend.clipped_plane
    assert ("set_object", backend.clipped_plane, b"addSubview:", surface.clipper) in runtime.calls
    backend.detach()
    window.close()


def _native_setup(monkeypatch, window):
    runtime = _FakeRuntime(window)
    monkeypatch.setattr(native_glass, "_runtime", lambda: runtime)
    monkeypatch.setattr(native_glass, "native_glass_available", lambda: True)
    monkeypatch.setattr(native_glass, "current_scheme", lambda: "Light")
    monkeypatch.setattr(native_glass, "current_colors", lambda: {"accent": "#123456"})
    monkeypatch.setattr(native_glass, "_message",
                        lambda _library, _restype, *_argtypes: (lambda *args: runtime.calls.append(("message", *args))))
    native_glass.prepare_window(window)
    return runtime


def test_resize_syncs_glass_synchronously_after_layout(monkeypatch, app):
    """Glass must follow a window resize in the same frame, not a later timer tick."""
    from PySide6.QtWidgets import QVBoxLayout

    window = QWidget()
    window.resize(300, 200)
    layout = QVBoxLayout(window)
    panel = QWidget()
    layout.addWidget(panel)
    runtime = _native_setup(monkeypatch, window)
    window.show()
    app.processEvents()
    surface = native_glass.GlassSurface(panel, 10)
    surface._attach()
    backend = surface.backend
    backend.refresh()
    window.resize(420, 260)   # no event processing: the window's resize handler must sync by itself
    frame = runtime.frames[surface.clipper]
    assert (frame.size.width, frame.size.height) == (panel.width(), panel.height())
    assert ("set_long", surface.clipper, b"setAutoresizingMask:", native_glass._NS_VIEW_MIN_Y_MARGIN) in runtime.calls
    backend.detach()
    window.close()


def test_pending_sync_flushes_before_qt_paints(monkeypatch, app):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QPaintEvent
    from PySide6.QtCore import QRect

    window = QWidget()
    window.resize(300, 200)
    child = QWidget(window)
    child.setGeometry(10, 10, 80, 40)
    runtime = _native_setup(monkeypatch, window)
    window.show()
    child.show()
    app.processEvents()
    surface = native_glass.GlassSurface(child, 8)
    surface._attach()
    backend = surface.backend
    backend.refresh()
    child.move(40, 50)         # schedules a sync for later
    assert backend._pending
    backend._hook.eventFilter(window, QEvent(QEvent.Type.UpdateRequest))
    assert not backend._pending
    frame = runtime.frames[surface.clipper]
    assert frame.origin.x == 40
    backend._hook.eventFilter(window, QPaintEvent(QRect(0, 0, 1, 1)))   # nothing pending: no extra work
    backend.detach()
    window.close()


def test_glass_of_a_widget_deleted_with_its_parent_is_released(monkeypatch, app):
    """A child destroyed by its parent never receives DeferredDelete; its glass must still go."""
    import shiboken6
    from PySide6.QtCore import QEvent

    window = QWidget()
    window.resize(300, 200)
    row = QWidget(window)
    row.setGeometry(0, 0, 200, 60)
    button = QWidget(row)
    button.setGeometry(10, 10, 60, 30)
    runtime = _native_setup(monkeypatch, window)
    window.show()
    row.show()
    button.show()
    app.processEvents()
    surface = native_glass.GlassSurface(button, 8)
    surface._attach()
    backend = surface.backend
    clipper = surface.clipper
    assert native_glass.native_material_count(window) == 1
    row.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(button)
    backend.refresh()          # must not raise on the dead widget
    assert native_glass.native_material_count(window) == 0
    assert ("release", clipper, b"removeFromSuperview") in runtime.calls
    backend.detach()
    window.close()


def test_hidden_glass_is_shown_again_after_a_refresh_reset(monkeypatch, app):
    window = QWidget()
    window.resize(300, 200)
    child = QWidget(window)
    child.setGeometry(10, 10, 80, 40)
    runtime = _native_setup(monkeypatch, window)
    window.show()
    child.show()
    app.processEvents()
    surface = native_glass.GlassSurface(child, 8)
    surface._attach()
    backend = surface.backend
    backend.refresh()
    child.hide()
    backend.refresh()
    assert ("set_bool", surface.clipper, b"setHidden:", True) in runtime.calls
    surface.refresh()          # resets the cached geometry signature
    child.show()
    backend.refresh()
    assert runtime.calls[-50:].count(("set_bool", surface.clipper, b"setHidden:", False)) >= 1
    backend.detach()
    window.close()
