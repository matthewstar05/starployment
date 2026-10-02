"""Rendered regressions for the desktop client's light and dark appearances.

The first-run wizard previously combined a light dialog with a nearly black tab
viewport. These tests inspect both Qt's palette and rendered widget surfaces so
child widgets cannot silently fall back to the host palette.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _still_frames(monkeypatch):
    """Sample settled frames: page transitions would move content under the sample points."""
    monkeypatch.setenv("JOBHUB_REDUCED_MOTION", "1")


def _luminance(color) -> float:
    channels = [color.redF(), color.greenF(), color.blueF()]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def _contrast(first, second) -> float:
    brighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (brighter + 0.05) / (darker + 0.05)


def _rendered_surface(widget, offset=(16, 16)):
    """Sample what the user sees at an inset of ``widget``.

    Pages are transparent over the window's ambient backdrop and glass
    panels, so sample the composited top-level window rather than grabbing
    the widget alone (which would return transparent pixels).
    """
    from PySide6.QtCore import QPoint

    top = widget.window()
    image = top.grab().toImage()
    assert image.width() > 24 and image.height() > 24
    point = widget.mapTo(top, QPoint(*offset))
    ratio = image.devicePixelRatio() or 1.0
    return image.pixelColor(int(point.x() * ratio), int(point.y() * ratio))


def _assert_appearance_palette(app, appearance):
    from PySide6.QtGui import QPalette

    palette = app.palette()
    window = palette.color(QPalette.ColorRole.Window)
    base = palette.color(QPalette.ColorRole.Base)
    text = palette.color(QPalette.ColorRole.WindowText)
    if appearance == "Dark":
        assert _luminance(window) < 0.20
        assert _luminance(base) < 0.28
        assert _luminance(text) > 0.82
    else:
        assert _luminance(window) > 0.82
        assert _luminance(base) > 0.82
        assert _luminance(text) < 0.20


def _save_snapshot(widget, tmp_path, filename):
    """Always keep pytest's artifact; optionally copy a stable review image."""
    path = tmp_path / filename
    assert widget.grab().save(str(path))
    output_dir = os.environ.get("JOBHUB_THEME_SCREENSHOTS")
    if output_dir:
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        assert widget.grab().save(str(destination / filename))


@pytest.mark.parametrize("appearance", ["Light", "Dark"])
def test_setup_wizard_tabs_and_scroll_viewports_follow_appearance(
    appearance, tmp_path
):
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QApplication, QLabel, QScrollArea
    from jobhub.desktop.styles import apply_appearance
    from jobhub.desktop.ui import SetupWizard

    app = QApplication.instance() or QApplication([])
    original_palette = QPalette(app.palette())
    original_stylesheet = app.styleSheet()
    wizard = None
    try:
        assert apply_appearance(app, appearance) == appearance
        _assert_appearance_palette(app, appearance)

        wizard = SetupWizard({}, {})
        wizard.resize(960, 760)
        wizard.show()
        app.processEvents()

        surfaces = []
        for index in range(wizard.tabs.count()):
            wizard.tabs.setCurrentIndex(index)
            app.processEvents()
            scroll = wizard.tabs.widget(index)
            assert isinstance(scroll, QScrollArea)
            # The right gutter inside each page's margins is always bare backdrop.
            viewport = scroll.viewport()
            surface = _rendered_surface(viewport, (viewport.width() - 5, viewport.height() // 2))
            surfaces.append(surface)
            if appearance == "Dark":
                assert _luminance(surface) < 0.24
            else:
                assert _luminance(surface) > 0.78

        # Every tab page is a scroll area and must paint the same base surface.
        assert max(_luminance(color) for color in surfaces) - min(
            _luminance(color) for color in surfaces
        ) < 0.08

        wizard.tabs.setCurrentIndex(0)
        app.processEvents()
        body = next(
            label
            for label in wizard.resume.findChildren(QLabel)
            if label.text().startswith("Choose a text-based PDF")
        )
        text_color = body.palette().color(QPalette.ColorRole.WindowText)
        assert _contrast(text_color, surfaces[0]) >= 4.5
        _save_snapshot(wizard, tmp_path, f"setup-{appearance.lower()}.png")
    finally:
        if wizard is not None:
            wizard.close()
        app.setStyleSheet(original_stylesheet)
        app.setPalette(original_palette)


@pytest.mark.parametrize("appearance", ["Light", "Dark"])
def test_dashboard_sidebar_and_navigation_follow_appearance(appearance, tmp_path):
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QApplication, QFrame
    from jobhub.desktop.styles import apply_appearance
    from jobhub.desktop.ui import JobHubWindow

    app = QApplication.instance() or QApplication([])
    original_palette = QPalette(app.palette())
    original_stylesheet = app.styleSheet()
    window = None
    try:
        assert apply_appearance(app, appearance) == appearance
        _assert_appearance_palette(app, appearance)

        window = JobHubWindow()
        window.show()
        app.processEvents()
        sidebar = window.findChild(QFrame, "sidebar")
        assert sidebar is not None
        # Sample the sidebar's own glass margin; the activity console beside it is a deliberately dark terminal.
        sidebar_surface = _rendered_surface(sidebar, (5, sidebar.height() // 2))
        dashboard_surface = _rendered_surface(window.dashboard)
        if appearance == "Dark":
            assert _luminance(sidebar_surface) < 0.28
            assert _luminance(dashboard_surface) < 0.28
        else:
            assert _luminance(sidebar_surface) > 0.72
            assert _luminance(dashboard_surface) > 0.78
        # Sidebar navigation text must stay legible on the translucent glass.
        text = QPalette(app.palette()).color(QPalette.ColorRole.WindowText)
        assert _contrast(text, sidebar_surface) >= 7.0

        # Exercise navigation and a nested form page while the same theme is active.
        for row in (1, 2, 3, 4, 0):
            window.nav.setCurrentRow(row)
            app.processEvents()
            assert window.stack.currentIndex() == row
            page_surface = _rendered_surface(window.stack.currentWidget())
            if appearance == "Dark":
                assert _luminance(page_surface) < 0.24
            else:
                assert _luminance(page_surface) > 0.78
        _save_snapshot(window, tmp_path, f"dashboard-{appearance.lower()}.png")
    finally:
        if window is not None:
            window.close()
        app.setStyleSheet(original_stylesheet)
        app.setPalette(original_palette)


def test_system_appearance_tracks_scheme_changes_on_existing_widgets(monkeypatch, tmp_path):
    """System mode follows Qt's scheme hint without rebuilding open dialogs."""
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QApplication, QLabel
    from jobhub.desktop import theme
    from jobhub.desktop.styles import apply_appearance
    from jobhub.desktop.ui import SetupWizard

    app = QApplication.instance() or QApplication([])
    original_palette = QPalette(app.palette())
    original_stylesheet = app.styleSheet()
    wizard = None
    try:
        monkeypatch.setattr(theme, "system_scheme", lambda _app=None: "Light")
        assert apply_appearance(app, "System") == "Light"
        _assert_appearance_palette(app, "Light")
        wizard = SetupWizard({}, {})
        wizard.resize(960, 760)
        wizard.show()
        app.processEvents()
        light_surface = _rendered_surface(wizard.tabs.widget(0).viewport())
        assert _luminance(light_surface) > 0.78
        _save_snapshot(wizard, tmp_path, "system-switch-light.png")

        monkeypatch.setattr(theme, "system_scheme", lambda _app=None: "Dark")
        assert app._jobhub_theme_manager.appearance == "System"
        app._jobhub_theme_manager._system_appearance_changed()
        assert app.property("jobhubResolvedAppearance") == "Dark"
        _assert_appearance_palette(app, "Dark")
        app.processEvents()
        dark_surface = _rendered_surface(wizard.tabs.widget(0).viewport())
        assert _luminance(dark_surface) < 0.24
        body = next(
            label
            for label in wizard.resume.findChildren(QLabel)
            if label.text().startswith("Choose a text-based PDF")
        )
        assert _contrast(
            body.palette().color(QPalette.ColorRole.WindowText), dark_surface
        ) >= 4.5
        _save_snapshot(wizard, tmp_path, "system-switch-dark.png")
    finally:
        if wizard is not None:
            wizard.close()
        app.setStyleSheet(original_stylesheet)
        app.setPalette(original_palette)
