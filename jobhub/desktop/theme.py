"""Application-wide appearance handling for the Starployment Qt client.

Design tokens live here. ``PALETTES`` holds opaque colors used by Qt's palette,
the stylesheet, and painted widgets. Glass materials are ``rgba(...)`` strings
so the same value works in a stylesheet and in ``widgets.qcolor``. The ambient
backdrop behind every glass surface is described by ``BACKDROP_GLOWS``.
"""
from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication


LIGHT = "Light"
DARK = "Dark"
SYSTEM = "System"
APPEARANCES = (SYSTEM, LIGHT, DARK)


PALETTES: dict[str, dict[str, str]] = {
    LIGHT: {
        # Opaque surfaces. ``window`` is also the backdrop's base tone.
        "window": "#F0F2F6",
        "surface": "#FFFFFF",
        "surface_alt": "#F3F4F7",
        "surface_hover": "#E6E8EE",
        "text": "#1D1D1F",
        "text_muted": "#626267",
        "text_subtle": "#6E6E73",
        "border": "#D1D3DA",
        "border_soft": "#E2E4EA",
        # The highlight color is dark gold, after the star in the app icon: selection,
        # focus, switches, links, and buttons. White sits only on ``button_*``.
        "accent": "#9A7418",
        "accent_hover": "#A67E1C",
        "accent_pressed": "#86650F",
        "button_top": "#977219",
        "button_fill": "#8C6A14",
        "button_hover": "#977318",
        "button_pressed": "#7A5B0E",
        "accent_soft": "#F6EDD3",
        "accent_text": "#7A5B0E",
        # Blue stays the tone for applications in progress (queued, prepared, sending).
        "info": "#0062C4",
        "info_soft": "#E3EEFF",
        "green": "#0F766E",
        "green_soft": "#E3F6F0",
        "amber": "#9A5700",
        "amber_soft": "#FFF1D1",
        "red": "#B42318",
        "red_soft": "#FDE9E7",
        "indigo": "#4F46E5",
        "indigo_soft": "#ECEBFF",
        "star": "#FFC233",
        "star_soft": "#FFF4D6",
        "star_text": "#8A5A00",
        "hero": "#0B0B0F",
        "tooltip": "#1D1D1F",
        "shadow": "#B8B8BE",
        "disabled": "#8E8E93",
        # Glass materials: translucent fills over the ambient backdrop.
        "glass_top": "rgba(255,255,255,0.72)",
        "glass_bottom": "rgba(255,255,255,0.58)",
        "glass_sidebar_top": "rgba(255,255,255,0.60)",
        "glass_sidebar_bottom": "rgba(255,255,255,0.44)",
        "glass_thick": "rgba(255,255,255,0.86)",
        "glass_edge": "rgba(255,255,255,0.95)",
        "glass_line": "rgba(24,32,64,0.09)",
        "glass_shadow": "rgba(40,52,90,0.16)",
        "fill_control": "rgba(255,255,255,0.78)",
        "fill_control_hover": "rgba(255,255,255,0.95)",
        "fill_quiet": "rgba(118,118,128,0.12)",
        "fill_hover": "rgba(0,0,0,0.045)",
        "fill_selected": "rgba(154,116,24,0.13)",
        # Surfaces inside a glass panel: a soft, borderless well (no second glass layer).
        "fill_inset": "rgba(255,255,255,0.46)",
        "separator": "rgba(60,60,67,0.10)",
        "track_off": "rgba(120,120,128,0.24)",
        "backdrop": "#F0F2F6",
    },
    DARK: {
        "window": "#131316",
        "surface": "#232327",
        "surface_alt": "#2C2C31",
        "surface_hover": "#3A3A40",
        "text": "#F5F5F7",
        "text_muted": "#AEAEB2",
        "text_subtle": "#98989E",
        "border": "#4A4A50",
        "border_soft": "#34343A",
        "accent": "#D4A63E",
        "accent_hover": "#DDB250",
        "accent_pressed": "#B88C2C",
        "button_top": "#9C7720",
        "button_fill": "#8F6B17",
        "button_hover": "#9C7720",
        "button_pressed": "#7A5A10",
        "accent_soft": "#3A2F15",
        "accent_text": "#E3BC62",
        "info": "#4DA3FF",
        "info_soft": "#17365A",
        "green": "#4FD1A1",
        "green_soft": "#173D34",
        "amber": "#FFD166",
        "amber_soft": "#463518",
        "red": "#FF6961",
        "red_soft": "#4A2320",
        "indigo": "#A5A0FF",
        "indigo_soft": "#2B2A55",
        "star": "#FFC233",
        "star_soft": "#3D3013",
        "star_text": "#FFD36B",
        "hero": "#08080B",
        "tooltip": "#3A3A3C",
        "shadow": "#0A0A0C",
        "disabled": "#77777C",
        "glass_top": "rgba(56,56,63,0.60)",
        "glass_bottom": "rgba(46,46,52,0.56)",
        "glass_sidebar_top": "rgba(58,58,66,0.52)",
        "glass_sidebar_bottom": "rgba(38,38,44,0.46)",
        "glass_thick": "rgba(46,46,52,0.94)",
        "glass_edge": "rgba(255,255,255,0.10)",
        "glass_line": "rgba(0,0,0,0.30)",
        "glass_shadow": "rgba(0,0,0,0.50)",
        "fill_control": "rgba(255,255,255,0.06)",
        "fill_control_hover": "rgba(255,255,255,0.09)",
        "fill_quiet": "rgba(118,118,128,0.24)",
        "fill_hover": "rgba(255,255,255,0.055)",
        "fill_selected": "rgba(212,166,62,0.14)",
        "fill_inset": "rgba(255,255,255,0.035)",
        "separator": "rgba(255,255,255,0.07)",
        "track_off": "rgba(120,120,128,0.36)",
        "backdrop": "#121215",
    },
}

# Soft color fields painted behind the glass, as (x, y, radius, rgba) where the
# position and radius are fractions of the window. Positions may sit outside
# 0..1 so only the tail of a glow enters the window.
BACKDROP_GLOWS: dict[str, tuple[tuple[float, float, float, str], ...]] = {
    # Glows avoid the upper-left, where page titles and supporting text sit
    # directly on the backdrop, so that text keeps at least 4.5:1 contrast.
    LIGHT: (
        (0.98, -0.18, 0.72, "rgba(140,184,255,0.50)"),
        (-0.12, 0.80, 0.55, "rgba(200,180,255,0.36)"),
        (0.55, 1.22, 0.78, "rgba(150,226,200,0.40)"),
        (1.08, 0.92, 0.42, "rgba(255,200,160,0.30)"),
    ),
    DARK: (
        (0.98, -0.18, 0.72, "rgba(40,72,180,0.55)"),
        (-0.12, 0.80, 0.55, "rgba(110,52,170,0.36)"),
        (0.55, 1.22, 0.78, "rgba(16,120,105,0.34)"),
        (1.08, 0.92, 0.42, "rgba(160,70,40,0.20)"),
    ),
}


def normalize_appearance(appearance: str | None) -> str:
    """Return a supported appearance name, defaulting invalid or missing values to System."""
    if not appearance:
        return SYSTEM
    normalized = appearance.strip().casefold()
    return next((value for value in APPEARANCES if value.casefold() == normalized), SYSTEM)


def system_scheme(app: QApplication | None = None) -> str:
    """Read Qt's current system appearance, with a safe light fallback."""
    app = app or QApplication.instance()
    if app is None:
        return LIGHT
    try:
        scheme = app.styleHints().colorScheme()
    except (AttributeError, RuntimeError):
        return LIGHT
    if scheme == Qt.ColorScheme.Dark:
        return DARK
    return LIGHT


def resolve_appearance(appearance: str | None, app: QApplication | None = None) -> str:
    selected = normalize_appearance(appearance)
    return system_scheme(app) if selected == SYSTEM else selected


def colors_for(appearance: str | None, app: QApplication | None = None) -> Mapping[str, str]:
    return PALETTES[resolve_appearance(appearance, app)]


def current_scheme() -> str:
    """Return the resolved Light/Dark scheme the running app is painted with."""
    app = QApplication.instance()
    value = app.property("jobhubResolvedAppearance") if app is not None else None
    return value if value in (LIGHT, DARK) else LIGHT


def current_colors() -> Mapping[str, str]:
    """Palette for the appearance currently applied to the application."""
    return PALETTES[current_scheme()]


def _set_palette(app: QApplication, colors: Mapping[str, str]) -> None:
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: colors["window"],
        QPalette.ColorRole.WindowText: colors["text"],
        QPalette.ColorRole.Base: colors["surface"],
        QPalette.ColorRole.AlternateBase: colors["surface_alt"],
        QPalette.ColorRole.ToolTipBase: colors["tooltip"],
        QPalette.ColorRole.ToolTipText: "#FFFFFF",
        QPalette.ColorRole.Text: colors["text"],
        QPalette.ColorRole.Button: colors["surface_alt"],
        QPalette.ColorRole.ButtonText: colors["text"],
        QPalette.ColorRole.BrightText: "#FFFFFF",
        QPalette.ColorRole.Highlight: colors["accent"],
        QPalette.ColorRole.HighlightedText: "#FFFFFF",
        QPalette.ColorRole.Link: colors["accent_text"],
        QPalette.ColorRole.PlaceholderText: colors["text_subtle"],
        QPalette.ColorRole.Light: colors["surface_hover"],
        QPalette.ColorRole.Mid: colors["border"],
        QPalette.ColorRole.Dark: colors["shadow"],
        QPalette.ColorRole.Shadow: colors["shadow"],
    }
    for role, value in roles.items():
        palette.setColor(role, value)
    disabled = {
        QPalette.ColorRole.WindowText: colors["disabled"],
        QPalette.ColorRole.Text: colors["disabled"],
        QPalette.ColorRole.ButtonText: colors["disabled"],
        QPalette.ColorRole.PlaceholderText: colors["disabled"],
    }
    for role, value in disabled.items():
        palette.setColor(QPalette.ColorGroup.Disabled, role, value)
    app.setPalette(palette)


class ThemeManager:
    """Keeps the application stylesheet and Qt palette in sync with appearance."""

    def __init__(self, app: QApplication, appearance: str = SYSTEM):
        self.app = app
        self.appearance = normalize_appearance(appearance)
        self._requested_hint = Qt.ColorScheme.Unknown
        self._applied_scheme: str | None = None
        self._applied_appearance: str | None = None
        self._applied_typeface: str | None = None
        app._jobhub_theme_manager = self
        # Connect once and retain this manager on QApplication so the callback remains alive.
        try:
            app.styleHints().colorSchemeChanged.connect(self._system_appearance_changed)
        except (AttributeError, RuntimeError):
            pass
        self.apply()

    @property
    def resolved(self) -> str:
        return resolve_appearance(self.appearance, self.app)

    def set_appearance(self, appearance: str, force: bool = False) -> str:
        normalized = normalize_appearance(appearance)
        if not force and normalized == self.appearance and self.resolved == self._applied_scheme:
            return self.resolved
        self.appearance = normalized
        self.apply(force=force)
        return self.resolved

    def apply(self, force: bool = False) -> str:
        from . import fonts

        current_typeface = getattr(fonts, "active", lambda: None)()
        scheme = self.resolved
        if (
            not force
            and scheme == self._applied_scheme
            and self.appearance == self._applied_appearance
            and current_typeface == self._applied_typeface
            and self.app.property("jobhubResolvedAppearance") == scheme
            and self.app.property("jobhubAppearance") == self.appearance
            and bool(self.app.styleSheet())
        ):
            return scheme

        desired_hint = {
            LIGHT: Qt.ColorScheme.Light,
            DARK: Qt.ColorScheme.Dark,
            SYSTEM: Qt.ColorScheme.Unknown,
        }[self.appearance]
        if desired_hint != self._requested_hint:
            self._requested_hint = desired_hint
            try:
                self.app.styleHints().setColorScheme(desired_hint)
            except (AttributeError, RuntimeError):
                pass
        colors = PALETTES[scheme]
        self.app.setProperty("jobhubAppearance", self.appearance)
        self.app.setProperty("jobhubResolvedAppearance", scheme)
        _set_palette(self.app, colors)
        # Imported lazily to avoid a styles/theme import cycle.
        from .styles import stylesheet

        self.app.setStyleSheet(stylesheet(scheme))
        # Windows draws the title bar itself; match it to the painted scheme.
        from .native_windows import install_title_bar_sync, sync_title_bars

        install_title_bar_sync(self.app)
        sync_title_bars(self.app)
        # Custom-painted widgets cache scheme-dependent pixmaps; repaint them.
        for widget in self.app.allWidgets():
            widget.update()
        self._applied_scheme = scheme
        self._applied_appearance = self.appearance
        self._applied_typeface = current_typeface
        return scheme

    def _system_appearance_changed(self, *_args) -> None:
        if self.appearance == SYSTEM and self.resolved != self._applied_scheme:
            self.apply()


def apply_appearance(app: QApplication, appearance: str = SYSTEM, force: bool = False) -> str:
    """Apply appearance and retain a manager that can follow later OS changes."""
    manager = getattr(app, "_jobhub_theme_manager", None)
    if manager is None:
        manager = ThemeManager(app, appearance)
        app._jobhub_theme_manager = manager
    else:
        manager.set_appearance(appearance, force=force)
    return manager.resolved
