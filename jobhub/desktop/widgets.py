"""Apple-style presentation primitives for the Starployment desktop client.

The visual language is "liquid glass": an ambient color field
(``AmbientBackdrop``) sits behind translucent rounded materials
(``GlassPanel``) with a hairline outer edge and a specular top highlight.
Controls that Qt stylesheets cannot express are painted here: the sliding
sidebar pill, switches, segmented controls, count-up numbers, toasts, and
page transitions.

Rules for this module:

* Presentation only. Nothing here reads the database or touches the network.
* Every color comes from ``theme.current_colors()`` at paint time, so a theme
  change only needs ``update()`` (``ThemeManager.apply`` does that for all widgets).
* Every animation checks ``reduced_motion()`` and jumps to its end state when
  motion is reduced or the widget is not visible yet.
"""
from __future__ import annotations

import math
import os
import random
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from hashlib import sha1

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QParallelAnimationGroup,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QCheckBox,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionButton,
    QStyleOptionViewItem,
    QStylePainter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import fonts
from .icons import icon_pixmap, jobhub_icon
from .native_glass import GlassSurface, refresh_window_materials, window_has_native_glass
from .theme import BACKDROP_GLOWS, DARK, PALETTES, current_colors, current_scheme


# --------------------------------------------------------------------------- motion

class Motion:
    """Shared durations (ms). Apple motion is quick to start and soft to land."""

    FAST = 160
    BASE = 260
    PAGE = 300
    SLOW = 420
    COUNT = 750


@lru_cache(maxsize=1)
def _system_reduce_motion() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        result = subprocess.run(
            ["defaults", "read", "com.apple.universalaccess", "reduceMotion"],
            capture_output=True, text=True, timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.stdout.strip() == "1"


def reduced_motion() -> bool:
    """True when animations should jump to their end state.

    ``JOBHUB_REDUCED_MOTION=1`` forces it on and ``=0`` forces it off;
    otherwise macOS's Reduce Motion accessibility preference decides.
    """
    value = os.environ.get("JOBHUB_REDUCED_MOTION", "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return _system_reduce_motion()


def _animate(anim: QVariantAnimation, start, end, duration: int,
             curve: QEasingCurve.Type | QEasingCurve = QEasingCurve.Type.OutCubic) -> None:
    anim.stop()
    anim.setDuration(duration)
    anim.setStartValue(start)
    anim.setEndValue(end)
    anim.setEasingCurve(curve if isinstance(curve, QEasingCurve) else QEasingCurve(curve))
    anim.start()


def spring_curve(overshoot: float = 0.9) -> QEasingCurve:
    """A gentle overshoot that reads like a UIKit spring with high damping."""
    curve = QEasingCurve(QEasingCurve.Type.OutBack)
    curve.setOvershoot(overshoot)
    return curve


# --------------------------------------------------------------------------- color

def qcolor(value: str | QColor) -> QColor:
    """Parse ``#RRGGBB``/``#AARRGGBB``/named colors and CSS ``rgba(r,g,b,a)``."""
    if isinstance(value, QColor):
        return QColor(value)
    text = value.strip()
    if text.startswith("rgba(") and text.endswith(")"):
        r, g, b, a = (part.strip() for part in text[5:-1].split(","))
        color = QColor(int(r), int(g), int(b))
        color.setAlphaF(max(0.0, min(1.0, float(a))))
        return color
    return QColor(text)


def with_alpha(value: str | QColor, alpha: float) -> QColor:
    color = qcolor(value)
    color.setAlphaF(max(0.0, min(1.0, alpha)))
    return color


def blend(first: str | QColor, second: str | QColor, amount: float) -> QColor:
    """Linear blend in RGBA; ``amount`` 0 returns ``first`` and 1 returns ``second``."""
    a, b = qcolor(first), qcolor(second)
    t = max(0.0, min(1.0, amount))
    return QColor.fromRgbF(
        a.redF() + (b.redF() - a.redF()) * t,
        a.greenF() + (b.greenF() - a.greenF()) * t,
        a.blueF() + (b.blueF() - a.blueF()) * t,
        a.alphaF() + (b.alphaF() - a.alphaF()) * t,
    )


def ui_font(pixel_size: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    """Interface font at an exact pixel size (Anthropic Sans, or the system UI font)."""
    family = fonts.ui_family()
    font = QFont(family) if family else QFont()
    font.setPixelSize(pixel_size)
    font.setWeight(weight)
    return font


def display_font(pixel_size: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    """Title font at an exact pixel size: Claude's serif, or the interface font with System type."""
    family = fonts.display_family()
    font = QFont(family) if family else QFont()
    font.setPixelSize(pixel_size)
    font.setWeight(weight)
    return font


# --------------------------------------------------------------------------- painting

@lru_cache(maxsize=2)
def _grain_tile(dark: bool) -> QImage:
    """A small deterministic noise tile. It breaks gradient banding the way
    Apple's materials do, at an alpha too low to read as texture."""
    size = 96
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    rng = random.Random(7)
    level = 255 if dark else 0
    for y in range(size):
        for x in range(size):
            alpha = rng.randint(0, 9 if dark else 7)
            if alpha:
                image.setPixelColor(x, y, QColor(level, level, level, alpha))
    return image


# Faint noise over the backdrop prevents gradient banding on real displays.
# Documentation renders turn it off because noise defeats PNG compression.
BACKDROP_GRAIN = True


@lru_cache(maxsize=6)
def _backdrop_pixmap(width: int, height: int, dpr: float, scheme: str, grain: bool = True) -> QPixmap:
    colors = PALETTES[scheme]
    # The glows are smooth, so render at quarter size and upscale. This keeps
    # live window resizes cheap without visible quality loss.
    small_w, small_h = max(8, width // 4), max(8, height // 4)
    image = QImage(small_w, small_h, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(colors["backdrop"]))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    extent = max(small_w, small_h)
    for x, y, radius, spec in BACKDROP_GLOWS[scheme]:
        center = QPointF(x * small_w, y * small_h)
        gradient = QRadialGradient(center, radius * extent)
        color = qcolor(spec)
        gradient.setColorAt(0.0, color)
        gradient.setColorAt(0.45, with_alpha(color, color.alphaF() * 0.55))
        gradient.setColorAt(1.0, with_alpha(color, 0.0))
        painter.fillRect(image.rect(), gradient)
    painter.end()
    scaled = image.scaled(
        max(1, int(width * dpr)), max(1, int(height * dpr)),
        Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation,
    )
    if grain:
        painter = QPainter(scaled)
        painter.drawTiledPixmap(scaled.rect(), QPixmap.fromImage(_grain_tile(scheme == DARK)))
        painter.end()
    pixmap = QPixmap.fromImage(scaled)
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


def paint_backdrop(painter: QPainter, widget: QWidget) -> None:
    """Fill ``widget`` with the ambient color field for the current scheme."""
    if window_has_native_glass(widget):
        return
    size = widget.size()
    dpr = widget.devicePixelRatioF() or 1.0
    painter.drawPixmap(0, 0, _backdrop_pixmap(size.width(), size.height(), round(dpr, 2), current_scheme(),
                                              BACKDROP_GRAIN))


def rounded_path(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    radius = max(0.0, min(radius, rect.width() / 2, rect.height() / 2))
    path.addRoundedRect(rect, radius, radius)
    return path


def paint_soft_shadow(painter: QPainter, rect: QRectF, radius: float, color: QColor | str,
                      blur: float = 22.0, offset: QPointF | None = None, steps: int = 10) -> None:
    """Approximate a gaussian drop shadow with concentric translucent layers.

    The shadow is clipped out of ``rect`` itself so it never darkens the
    translucent glass painted on top of it.
    """
    color = qcolor(color)
    offset = offset if offset is not None else QPointF(0, blur * 0.35)
    painter.save()
    outer = QPainterPath()
    outer.addRect(rect.adjusted(-blur * 2, -blur * 2, blur * 2, blur * 2).translated(offset))
    painter.setClipPath(outer.subtracted(rounded_path(rect, radius)))
    painter.setPen(Qt.PenStyle.NoPen)
    base = color.alphaF()
    for index in range(steps):
        t = (index + 1) / steps
        spread = blur * t
        layer = QColor(color)
        layer.setAlphaF(base * 2.0 * (1.0 - t) / steps)
        painter.setBrush(layer)
        shape = rect.translated(offset).adjusted(-spread, -spread, spread, spread)
        painter.drawRoundedRect(shape, radius + spread, radius + spread)
    painter.restore()


_MATERIALS = {
    "regular": ("glass_top", "glass_bottom"),
    "sidebar": ("glass_sidebar_top", "glass_sidebar_bottom"),
    "thick": ("glass_thick", "glass_thick"),
}


def paint_glass(painter: QPainter, rect: QRectF, radius: float, *, material: str = "regular",
                tint: QColor | None = None, highlight: bool = True) -> None:
    """Paint a glass material: translucent fill, hairline edge, specular top rim."""
    colors = current_colors()
    top_key, bottom_key = _MATERIALS[material]
    rect = QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)
    path = rounded_path(rect, radius)
    fill = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    fill.setColorAt(0.0, qcolor(colors[top_key]))
    fill.setColorAt(1.0, qcolor(colors[bottom_key]))
    painter.fillPath(path, fill)
    if tint is not None:
        painter.fillPath(path, tint)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(qcolor(colors["glass_line"]), 1.0))
    painter.drawPath(path)
    if highlight and rect.height() > 4:
        inner = rect.adjusted(1, 1, -1, -1)
        edge = qcolor(colors["glass_edge"])
        rim = QLinearGradient(inner.topLeft(), inner.bottomLeft())
        rim.setColorAt(0.0, edge)
        rim.setColorAt(min(0.95, 30.0 / max(1.0, inner.height())), with_alpha(edge, 0.0))
        rim.setColorAt(1.0, with_alpha(edge, edge.alphaF() * 0.35))
        painter.setPen(QPen(rim, 1.0))
        painter.drawPath(rounded_path(inner, max(0.0, radius - 1)))


def paint_activity_indicator(painter: QPainter, center: QPointF, radius: float,
                             phase: int, color: QColor | str) -> None:
    """Apple's classic eight-spoke spinner; advance ``phase`` ~12 times a second."""
    base = qcolor(color)
    painter.save()
    painter.translate(center)
    for spoke in range(8):
        strength = ((spoke - phase) % 8) / 7.0
        pen = QPen(with_alpha(base, 0.18 + 0.82 * strength), max(1.4, radius * 0.24))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(0, -radius * 0.48), QPointF(0, -radius))
        painter.rotate(45)
    painter.restore()


_MONOGRAM_COLORS = ("#0A84FF", "#30B0C7", "#34A853", "#FF9F0A", "#FF375F",
                    "#BF5AF2", "#5E5CE6", "#FF6B3D", "#8E7CC3", "#2FB6A0")


def monogram_color(text: str) -> QColor:
    digest = sha1((text or "?").strip().casefold().encode("utf-8")).digest()
    return QColor(_MONOGRAM_COLORS[digest[0] % len(_MONOGRAM_COLORS)])


def paint_monogram(painter: QPainter, rect: QRectF, text: str) -> None:
    """App-icon-like rounded tile with the employer's initial."""
    base = monogram_color(text)
    path = rounded_path(rect, rect.width() * 0.28)
    gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    gradient.setColorAt(0.0, base.lighter(118))
    gradient.setColorAt(1.0, base.darker(108))
    painter.fillPath(path, gradient)
    painter.setPen(QPen(with_alpha("#FFFFFF", 0.28), 1.0))
    painter.drawPath(rounded_path(rect.adjusted(0.5, 0.5, -0.5, -0.5), rect.width() * 0.28))
    initial = next((ch for ch in (text or "") if ch.isalnum()), "?").upper()
    painter.setPen(QColor("#FFFFFF"))
    painter.setFont(ui_font(max(9, int(rect.height() * 0.46)), QFont.Weight.DemiBold))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, initial)


def paint_pill(painter: QPainter, rect: QRectF, text: str, fg: QColor | str, bg: QColor | str,
               font: QFont | None = None) -> None:
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(qcolor(bg))
    painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
    painter.setPen(qcolor(fg))
    painter.setFont(font or ui_font(11, QFont.Weight.DemiBold))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
    painter.restore()


# --------------------------------------------------------------------------- surfaces

class AmbientBackdrop(QWidget):
    """The window "wallpaper": a soft color field that glass surfaces float over.

    Widgets registered with ``add_shadow_target`` get a soft shadow painted on
    the backdrop beneath them, so floating chrome (the sidebar) reads as
    raised without a per-frame graphics effect.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._shadow_targets: list[tuple[QWidget, float]] = []

    def add_shadow_target(self, widget: QWidget, radius: float) -> None:
        self._shadow_targets.append((widget, radius))
        widget.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in {QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide}:
            self.update()
        return super().eventFilter(watched, event)

    def paintEvent(self, event) -> None:
        if window_has_native_glass(self):
            return
        painter = QPainter(self)
        paint_backdrop(painter, self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shadow = current_colors()["glass_shadow"]
        for widget, radius in self._shadow_targets:
            if widget.isVisible():
                rect = QRectF(widget.mapTo(self, QPoint(0, 0)), widget.size())
                paint_soft_shadow(painter, rect, radius, shadow, blur=26)
        painter.end()


class _NoNativeGlass:
    """Stand-in for ``GlassSurface`` on surfaces that are never native glass."""

    active = False

    def refresh(self) -> None:
        pass


class GlassPanel(QFrame):
    """A translucent rounded material. Children should be transparent.

    ``material`` is ``regular`` (content cards), ``sidebar`` (thinner, more of
    the backdrop shows through), ``thick`` (sheets and popovers), or ``inset``
    for a surface inside another panel: a soft borderless well, never a second
    glass layer (glass on glass reads as a harsh step in tone). ``tint`` washes
    the panel with a semantic color, for example ``"accent"``.
    """

    def __init__(self, parent: QWidget | None = None, *, radius: float = 16.0,
                 material: str = "regular", tint: str | None = None, tint_alpha: float = 0.10):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setObjectName("glass")
        self._radius = radius
        self._material = material
        self._tint = tint
        self._tint_alpha = tint_alpha
        if material == "inset":
            self._native_glass = _NoNativeGlass()
        else:
            self._native_glass = GlassSurface(
                self, radius, tint=lambda: (self._tint, self._tint_alpha) if self._tint else None,
            )

    @property
    def radius(self) -> float:
        return self._radius

    def set_tint(self, tint: str | None, alpha: float | None = None) -> None:
        self._tint = tint
        if alpha is not None:
            self._tint_alpha = alpha
        self._native_glass.refresh()
        self.update()

    def paintEvent(self, event) -> None:
        if self._native_glass.active:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        tint = with_alpha(current_colors()[self._tint], self._tint_alpha) if self._tint else None
        if self._material == "inset":
            path = rounded_path(QRectF(self.rect()), self._radius)
            painter.fillPath(path, qcolor(current_colors()["fill_inset"]))
            if tint is not None:
                painter.fillPath(path, tint)
        else:
            paint_glass(painter, QRectF(self.rect()), self._radius, material=self._material, tint=tint)
        painter.end()


class ClickableGlass(GlassPanel):
    """A glass tile that brightens on hover and dips on press, like a Liquid Glass button."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None, **kwargs):
        super().__init__(parent, **kwargs)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._hover = 0.0
        self._pressed = False
        self._keyboard_focus = False
        self._anim = QVariantAnimation(self)
        self._anim.valueChanged.connect(self._set_hover)

    def _set_hover(self, value) -> None:
        self._hover = float(value)
        self.update()

    def _fade(self, target: float) -> None:
        if reduced_motion():
            self._set_hover(target)
        else:
            _animate(self._anim, self._hover, target, Motion.FAST)

    def enterEvent(self, event) -> None:
        self._fade(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._pressed = False
        self._fade(0.0)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        was_pressed = self._pressed
        self._pressed = False
        self.update()
        if was_pressed and event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = current_colors()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self._hover > 0.01:
            painter.fillPath(rounded_path(rect, self.radius), with_alpha(colors["glass_edge"], 0.28 * self._hover))
        if self._pressed:
            painter.fillPath(rounded_path(rect, self.radius), qcolor(colors["fill_hover"]))
        if self.hasFocus() and self.focusPolicy() != Qt.FocusPolicy.NoFocus and self._keyboard_focus:
            painter.setPen(QPen(with_alpha(colors["accent"], 0.55), 2.0))
            painter.drawPath(rounded_path(rect.adjusted(1, 1, -1, -1), self.radius - 1))
        painter.end()

    def focusInEvent(self, event) -> None:
        self._keyboard_focus = event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:
        self._keyboard_focus = False
        super().focusOutEvent(event)
        self.update()


# --------------------------------------------------------------------------- navigation

NAV_ICON_ROLE = int(Qt.ItemDataRole.UserRole) + 1
NAV_BADGE_ROLE = int(Qt.ItemDataRole.UserRole) + 2
NAV_BADGE_TONE_ROLE = int(Qt.ItemDataRole.UserRole) + 3


class _NavDelegate(QStyledItemDelegate):
    ROW_HEIGHT = 38

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        return QSize(max(1, option.rect.width()), self.ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        view = option.widget
        colors = current_colors()
        selected = view is not None and index.row() == view.currentRow()
        if isinstance(view, GlassNavList):
            rect = view.row_rect(option.rect.top(), option.rect.height())
        else:
            rect = QRectF(option.rect).adjusted(GlassNavList.INSET_X, 3, -GlassNavList.INSET_X, -3)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        icon_name = index.data(NAV_ICON_ROLE)
        if icon_name:
            color = colors["accent"] if selected else colors["text_muted"]
            pixmap = icon_pixmap(str(icon_name), color, 17, 1.8)
            painter.drawPixmap(QPointF(rect.left() + 11, rect.center().y() - 8.5), pixmap)
        text_left = rect.left() + 38
        badge = str(index.data(NAV_BADGE_ROLE) or "")
        badge_width = 0.0
        if badge:
            font = ui_font(11, QFont.Weight.DemiBold)
            badge_width = max(20.0, QFontMetrics(font).horizontalAdvance(badge) + 12.0)
            badge_rect = QRectF(rect.right() - badge_width - 8, rect.center().y() - 9, badge_width, 18)
            tone = str(index.data(NAV_BADGE_TONE_ROLE) or "neutral")
            fg, bg = {
                "amber": (colors["amber"], colors["amber_soft"]),
                "accent": (colors["accent_text"], colors["accent_soft"]),
            }.get(tone, (colors["text_muted"], colors["fill_quiet"]))
            paint_pill(painter, badge_rect, badge, fg, bg, font)
        # Page names share the serif of the app title when Claude's type is in use.
        serif = fonts.active() == fonts.CLAUDE
        font = display_font(14 if serif else 13, QFont.Weight.DemiBold if selected else QFont.Weight.Medium)
        painter.setFont(font)
        painter.setPen(qcolor(colors["text"]))
        text_rect = QRectF(text_left, rect.top(), rect.right() - text_left - badge_width - 12, rect.height())
        text = QFontMetrics(font).elidedText(index.data(Qt.ItemDataRole.DisplayRole) or "",
                                             Qt.TextElideMode.ElideRight, int(text_rect.width()))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        painter.restore()


class GlassNavList(QListWidget):
    """Sidebar navigation with a glass selection pill that springs between rows.

    Keeps the ``QListWidget`` API (``setCurrentRow``, ``currentRowChanged``),
    adding ``add_destination`` and ``set_badge``.
    """

    INSET_X = 10

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("nav")
        self.setItemDelegate(_NavDelegate(self))
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setUniformItemSizes(True)
        self.setSpacing(0)
        self._hover_row = -1
        self._pill = QRectF()
        self._anim = QVariantAnimation(self)
        self._anim.valueChanged.connect(self._set_pill)
        self.currentRowChanged.connect(self._move_pill)
        self._native_selection = GlassSurface(self.viewport(), 10, rect=lambda: self._pill)

    def add_destination(self, label: str, icon_name: str) -> QListWidgetItem:
        item = QListWidgetItem(label)
        item.setData(NAV_ICON_ROLE, icon_name)
        item.setToolTip(label)
        self.addItem(item)
        return item

    def set_badge(self, row: int, text: str = "", tone: str = "neutral") -> None:
        item = self.item(row)
        if item is None:
            return
        if item.data(NAV_BADGE_ROLE) != text or item.data(NAV_BADGE_TONE_ROLE) != tone:
            item.setData(NAV_BADGE_ROLE, text)
            item.setData(NAV_BADGE_TONE_ROLE, tone)
            self.viewport().update()

    def row_rect(self, top: float, height: float) -> QRectF:
        """The pill-shaped area of a row, inset from the sidebar's edges."""
        return QRectF(self.INSET_X, top + 3, self.viewport().width() - 2 * self.INSET_X, height - 6)

    def _target(self, row: int) -> QRectF:
        item = self.item(row)
        if item is None:
            return QRectF()
        rect = self.visualItemRect(item)
        return self.row_rect(rect.top(), rect.height())

    def _set_pill(self, rect) -> None:
        self._pill = QRectF(rect)
        self._native_selection.refresh()
        self.viewport().update()

    def _move_pill(self, row: int) -> None:
        target = self._target(row)
        if self._pill.isNull() or reduced_motion() or not self.isVisible():
            self._anim.stop()
            self._set_pill(target)
            return
        _animate(self._anim, QRectF(self._pill), target, Motion.SLOW, spring_curve(0.8))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._anim.stop()
        self._set_pill(self._target(self.currentRow()))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._set_pill(self._target(self.currentRow()))

    def mouseMoveEvent(self, event) -> None:
        row = self.indexAt(event.position().toPoint()).row()
        if row != self._hover_row:
            self._hover_row = row
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover_row = -1
        self.viewport().update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        colors = current_colors()
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if 0 <= self._hover_row != self.currentRow():
            painter.fillPath(rounded_path(self._target(self._hover_row), 10), qcolor(colors["fill_hover"]))
        if not self._native_selection.active and not self._pill.isNull() and self.currentRow() >= 0:
            if current_scheme() == DARK:
                paint_glass(painter, self._pill, 10, material="regular",
                            tint=with_alpha("#FFFFFF", 0.06))
            else:
                paint_soft_shadow(painter, self._pill, 10, with_alpha("#1C2440", 0.10), blur=8,
                                  offset=QPointF(0, 2), steps=6)
                painter.fillPath(rounded_path(self._pill, 10), with_alpha("#FFFFFF", 0.92))
                painter.setPen(QPen(qcolor(colors["glass_line"]), 1))
                painter.drawPath(rounded_path(self._pill.adjusted(0.5, 0.5, -0.5, -0.5), 10))
        painter.end()
        super().paintEvent(event)


# --------------------------------------------------------------------------- controls

class ToggleSwitch(QCheckBox):
    """An Apple switch. It is a ``QCheckBox``, so ``isChecked``/``toggled`` work.

    With text, the label is drawn at the left and the switch at the right edge,
    matching a System Settings row.
    """

    TRACK = QSize(40, 24)

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setObjectName("toggle")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Preferred if not text else QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self._position = 0.0
        self._keyboard_focus = False
        self._anim = QVariantAnimation(self)
        self._anim.valueChanged.connect(self._set_position)
        self.toggled.connect(self._animate)

    def _set_position(self, value) -> None:
        self._position = float(value)
        self.update()

    def _animate(self, checked: bool) -> None:
        target = 1.0 if checked else 0.0
        if reduced_motion() or not self.isVisible():
            self._anim.stop()
            self._set_position(target)
        else:
            _animate(self._anim, self._position, target, Motion.BASE, QEasingCurve.Type.OutCubic)

    def sizeHint(self) -> QSize:
        text = self.text()
        metrics = QFontMetrics(ui_font(13))
        width = self.TRACK.width() + 4 + (metrics.horizontalAdvance(text) + 16 if text else 0)
        return QSize(width, max(self.TRACK.height() + 6, metrics.height() + 8))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint() if not self.text() else QSize(self.TRACK.width() + 60, self.sizeHint().height())

    def hitButton(self, pos: QPoint) -> bool:
        return self.rect().contains(pos)

    def focusInEvent(self, event) -> None:
        self._keyboard_focus = event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason)
        super().focusInEvent(event)

    def paintEvent(self, event) -> None:
        colors = current_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = self.isEnabled()
        track = QRectF(self.width() - self.TRACK.width() - 2, (self.height() - self.TRACK.height()) / 2,
                       self.TRACK.width(), self.TRACK.height())
        if self.text():
            painter.setFont(ui_font(13))
            painter.setPen(qcolor(colors["text"] if enabled else colors["disabled"]))
            text_rect = QRectF(0, 0, track.left() - 12, self.height())
            text = QFontMetrics(ui_font(13)).elidedText(self.text(), Qt.TextElideMode.ElideRight, int(text_rect.width()))
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        on = qcolor(colors["accent"])
        fill = blend(colors["track_off"], on, self._position)
        if not enabled:
            fill = with_alpha(fill, fill.alphaF() * 0.45)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(track, track.height() / 2, track.height() / 2)
        knob_d = track.height() - 4
        x = track.left() + 2 + (track.width() - knob_d - 4) * self._position
        knob = QRectF(x, track.top() + 2, knob_d, knob_d)
        painter.setBrush(with_alpha("#000000", 0.16))
        painter.drawEllipse(knob.translated(0, 1).adjusted(-0.5, -0.5, 0.5, 0.5))
        painter.setBrush(QColor("#FFFFFF") if enabled else QColor("#F2F2F4"))
        painter.drawEllipse(knob)
        if self.hasFocus() and self._keyboard_focus:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(with_alpha(colors["accent"], 0.5), 3))
            painter.drawRoundedRect(track.adjusted(-2.5, -2.5, 2.5, 2.5), track.height() / 2 + 2.5, track.height() / 2 + 2.5)
        painter.end()


class SegmentedControl(QWidget):
    """A segmented control with a sliding glass thumb.

    Mirrors the ``QComboBox`` API the views need: ``addItem``, ``findData``,
    ``currentIndex``/``setCurrentIndex``, ``currentData``, ``currentText``, and
    ``currentIndexChanged``. ``activated`` fires only for user interaction.
    """

    currentIndexChanged = Signal(int)
    activated = Signal(int)

    def __init__(self, parent: QWidget | None = None, *, compact: bool = False):
        super().__init__(parent)
        self.setObjectName("segmented")
        self._items: list[tuple[str, object]] = []
        self._index = -1
        self._hover = -1
        self._thumb = QRectF()
        self._compact = compact
        self._anim = QVariantAnimation(self)
        self._anim.valueChanged.connect(self._set_thumb)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._native_glass = GlassSurface(self, lambda: self.height() / 2)

    # QComboBox-compatible surface -------------------------------------------------
    def addItem(self, text: str, data: object = None) -> None:  # noqa: N802
        self._items.append((text, text if data is None else data))
        if self._index < 0:
            self._index = 0
        self.updateGeometry()
        self.update()

    def addItems(self, texts: list[str]) -> None:  # noqa: N802
        for text in texts:
            self.addItem(text)

    def count(self) -> int:
        return len(self._items)

    def itemText(self, index: int) -> str:  # noqa: N802
        return self._items[index][0]

    def itemData(self, index: int) -> object:  # noqa: N802
        return self._items[index][1]

    def findData(self, data: object) -> int:  # noqa: N802
        return next((i for i, (_, value) in enumerate(self._items) if value == data), -1)

    def findText(self, text: str) -> int:  # noqa: N802
        return next((i for i, (label, _) in enumerate(self._items) if label == text), -1)

    def currentIndex(self) -> int:  # noqa: N802
        return self._index

    def currentData(self) -> object:  # noqa: N802
        return self._items[self._index][1] if 0 <= self._index < len(self._items) else None

    def currentText(self) -> str:  # noqa: N802
        return self._items[self._index][0] if 0 <= self._index < len(self._items) else ""

    def setCurrentText(self, text: str) -> None:  # noqa: N802
        index = self.findText(text)
        if index < 0:
            index = self.findData(text)
        if index >= 0:
            self.setCurrentIndex(index)

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        if not 0 <= index < len(self._items) or index == self._index:
            return
        self._index = index
        target = self._segment_rect(index)
        if self._thumb.isNull() or reduced_motion() or not self.isVisible():
            self._anim.stop()
            self._set_thumb(target)
        else:
            _animate(self._anim, QRectF(self._thumb), target, Motion.BASE, spring_curve(0.6))
        self.currentIndexChanged.emit(index)

    # Geometry and painting -------------------------------------------------------
    def _metrics(self) -> QFontMetrics:
        return QFontMetrics(ui_font(12 if self._compact else 13, QFont.Weight.DemiBold))

    def sizeHint(self) -> QSize:
        widest = max((self._metrics().horizontalAdvance(text) for text, _ in self._items), default=40)
        segment = widest + (22 if self._compact else 30)
        return QSize(int(segment * max(1, len(self._items)) + 4), 28 if self._compact else 30)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def _segment_rect(self, index: int) -> QRectF:
        if not self._items:
            return QRectF()
        inner = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        width = inner.width() / len(self._items)
        return QRectF(inner.left() + width * index, inner.top(), width, inner.height())

    def _set_thumb(self, rect) -> None:
        self._thumb = QRectF(rect)
        self.update()

    def _index_at(self, pos: QPointF) -> int:
        for index in range(len(self._items)):
            if self._segment_rect(index).contains(pos):
                return index
        return -1

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._anim.stop()
        self._set_thumb(self._segment_rect(self._index))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._set_thumb(self._segment_rect(self._index))

    def mouseMoveEvent(self, event) -> None:
        index = self._index_at(event.position())
        if index != self._hover:
            self._hover = index
            self.update()

    def leaveEvent(self, event) -> None:
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        index = self._index_at(event.position())
        if index >= 0 and event.button() == Qt.MouseButton.LeftButton:
            changed = index != self._index
            self.setCurrentIndex(index)
            if changed:
                self.activated.emit(index)

    def keyPressEvent(self, event) -> None:
        step = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1}.get(event.key())
        if step:
            index = max(0, min(len(self._items) - 1, self._index + step))
            if index != self._index:
                self.setCurrentIndex(index)
                self.activated.emit(index)
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        colors = current_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = track.height() / 2
        if not self._native_glass.active:
            painter.fillPath(rounded_path(track, radius), qcolor(colors["fill_quiet"]))
        if not self._thumb.isNull() and self._index >= 0:
            thumb = self._thumb.adjusted(0.5, 0.5, -0.5, -0.5)
            if current_scheme() == DARK:
                painter.fillPath(rounded_path(thumb, thumb.height() / 2), QColor("#636366"))
                painter.setPen(QPen(with_alpha("#FFFFFF", 0.12), 1))
                painter.drawPath(rounded_path(thumb, thumb.height() / 2))
            else:
                if not self._native_glass.active:
                    paint_soft_shadow(painter, thumb, thumb.height() / 2, with_alpha("#000000", 0.14), blur=5,
                                      offset=QPointF(0, 1.5), steps=5)
                painter.fillPath(rounded_path(thumb, thumb.height() / 2), QColor("#FFFFFF"))
        metrics = self._metrics()
        for index, (text, _) in enumerate(self._items):
            rect = self._segment_rect(index)
            selected = index == self._index
            font = ui_font(12 if self._compact else 13, QFont.Weight.DemiBold if selected else QFont.Weight.Medium)
            painter.setFont(font)
            color = colors["text"] if selected or index == self._hover else colors["text_muted"]
            painter.setPen(qcolor(color if self.isEnabled() else colors["disabled"]))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(rect.width() - 8)))
        if self.hasFocus():
            painter.setPen(QPen(with_alpha(colors["accent"], 0.45), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(rounded_path(track.adjusted(1, 1, -1, -1), radius - 1))
        painter.end()


class SearchField(QLineEdit):
    """A capsule search field with a leading magnifier and a clear button."""

    def __init__(self, placeholder: str = "Search", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("search")
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        self._icon_action = self.addAction(QIcon(), QLineEdit.ActionPosition.LeadingPosition)
        self._refresh_icon()
        self._native_glass = GlassSurface(self, lambda: self.height() / 2)

    def _refresh_icon(self) -> None:
        self._icon_action.setIcon(jobhub_icon("search", current_colors()["text_subtle"], 15))

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            self._refresh_icon()


class ActivityIndicator(QWidget):
    """A small spinner shown while background work runs."""

    def __init__(self, diameter: int = 16, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(diameter, diameter)
        self._phase = 0
        self._timer = QTimer(self)
        self._timer.setInterval(83)
        self._timer.timeout.connect(self._tick)

    def _tick(self) -> None:
        self._phase = (self._phase + 1) % 8
        self.update()

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()
        self.show()

    def stop(self) -> None:
        self._timer.stop()
        self.hide()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_activity_indicator(painter, QPointF(self.width() / 2, self.height() / 2),
                                 self.width() / 2 - 1, self._phase, current_colors()["text_muted"])
        painter.end()


class CountUpLabel(QLabel):
    """A metric number that counts to its new value, and up from zero on first show."""

    def __init__(self, value: int = 0, parent: QWidget | None = None):
        super().__init__(f"{value:,}", parent)
        self._value = value
        self._intro_played = False
        self._anim = QVariantAnimation(self)
        self._anim.valueChanged.connect(lambda v: self.setText(f"{int(round(float(v))):,}"))

    def value(self) -> int:
        return self._value

    def set_value(self, value: int) -> None:
        value = int(value)
        if value == self._value:
            return
        start, self._value = self._value, value
        if reduced_motion() or not self.isVisible():
            self._anim.stop()
            self.setText(f"{value:,}")
            return
        self._intro_played = True  # a live count replaces the first-show intro
        _animate(self._anim, float(start), float(value), Motion.COUNT, QEasingCurve.Type.OutCubic)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._intro_played:
            self._intro_played = True
            running = self._anim.state() == QVariantAnimation.State.Running
            if self._value > 0 and not running and not reduced_motion():
                _animate(self._anim, 0.0, float(self._value), Motion.COUNT + 250, QEasingCurve.Type.OutCubic)


class Monogram(QWidget):
    """An employer initial on a colored, app-icon-like tile."""

    def __init__(self, size: int = 44, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._text = ""

    def set_text(self, text: str) -> None:
        self._text = text
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_monogram(painter, QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self._text)
        painter.end()


class StatusDot(QWidget):
    """A small semantic dot with a soft halo, for timelines and status lines."""

    def __init__(self, tone: str = "neutral", diameter: int = 8, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(diameter + 4, diameter + 4)
        self._tone = tone

    def set_tone(self, tone: str) -> None:
        if tone != self._tone:
            self._tone = tone
            self.update()

    def paintEvent(self, event) -> None:
        colors = current_colors()
        color = qcolor({"green": colors["green"], "amber": colors["amber"], "red": colors["red"],
                        "accent": colors["accent"]}.get(self._tone, colors["text_subtle"]))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(with_alpha(color, 0.20))
        painter.drawEllipse(QRectF(self.rect()))
        painter.setBrush(color)
        painter.drawEllipse(QRectF(self.rect()).adjusted(2, 2, -2, -2))
        painter.end()


class IconBadge(QWidget):
    """A tinted circle holding a symbol, used on metric tiles and empty states."""

    def __init__(self, icon_name: str, tone: str = "accent", size: int = 30, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._icon = icon_name
        self._tone = tone

    def paintEvent(self, event) -> None:
        fg, bg = tone_colors(self._tone)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(qcolor(bg))
        painter.drawEllipse(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))
        glyph = int(self.width() * 0.52)
        pixmap = icon_pixmap(self._icon, qcolor(fg).name(), glyph, 2.0)
        painter.drawPixmap(QPointF((self.width() - glyph) / 2, (self.height() - glyph) / 2), pixmap)
        painter.end()


def tone_colors(tone: str) -> tuple[str, str]:
    """Foreground/background colors for a semantic tone name."""
    c = current_colors()
    return {
        "accent": (c["accent_text"], c["accent_soft"]),
        "green": (c["green"], c["green_soft"]),
        "amber": (c["amber"], c["amber_soft"]),
        "red": (c["red"], c["red_soft"]),
        "indigo": (c["indigo"], c["indigo_soft"]),
    }.get(tone, (c["text_muted"], c["fill_quiet"]))


# --------------------------------------------------------------------------- layout helpers

def label(text: str, object_name: str = "", *, wrap: bool = True) -> QLabel:
    item = QLabel(text)
    if object_name:
        item.setObjectName(object_name)
    item.setWordWrap(wrap)
    return item


class CapsuleButton(QPushButton):
    """A stylesheet-drawn capsule button whose optional symbol follows the theme.

    The stylesheet paints the capsule; with a symbol, this class lays out the
    symbol and label itself so there is a comfortable gap, and ``trailing``
    places the symbol after the label (for "Continue ›" style actions).
    """

    ICON = 15
    GAP = 6

    def __init__(self, text: str = "", role: str = "primary", icon: str | None = None,
                 parent: QWidget | None = None, *, trailing: bool = False):
        super().__init__(text, parent)
        if role != "primary":
            self.setObjectName(role)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._icon_name = icon
        self._trailing = trailing
        self.refresh_icon()
        # Buttons are drawn by Qt, not as native glass. Small, closely packed
        # NSGlassEffectViews in clipping parents render clipped rectangular
        # shadows and washed-out tints; a painted capsule is crisp in every
        # container and greys out correctly when disabled.

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        if self._icon_name and self.text():
            hint.setWidth(hint.width() + self.GAP)
        return hint

    def paintEvent(self, event) -> None:
        if not self._icon_name:
            super().paintEvent(event)
            return
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""
        option.icon = QIcon()
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_PushButtonBevel, option)
        area = self.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, self)
        text = self.text()
        metrics = self.fontMetrics()
        text_width = metrics.horizontalAdvance(text) if text else 0
        gap = self.GAP if text else 0
        left = area.center().x() + 1 - (self.ICON + gap + text_width) / 2
        mode = QIcon.Mode.Normal if self.isEnabled() else QIcon.Mode.Disabled
        pixmap = self.icon().pixmap(QSize(self.ICON, self.ICON), mode)
        icon_x = left + text_width + gap if self._trailing else left
        text_x = left if self._trailing else left + self.ICON + gap
        painter.drawPixmap(QPointF(icon_x, area.center().y() + 1 - self.ICON / 2), pixmap)
        if text:
            color = self.palette().color(self.foregroundRole()) if self.isEnabled() else qcolor(current_colors()["disabled"])
            painter.setPen(color)
            painter.drawText(QRectF(text_x, area.top(), text_width + 2, area.height()),
                             int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
        painter.end()

    def set_icon_name(self, icon: str | None) -> None:
        self._icon_name = icon
        self.refresh_icon()

    def refresh_icon(self, color: str | None = None) -> None:
        if not self._icon_name:
            self.setIcon(QIcon())
            return
        colors = current_colors()
        role = self.objectName()
        if color is None:
            color = ("#FFFFFF" if role in ("", "primary") else
                     colors["accent_text"] if role == "quiet" else colors["text_muted"])
        self.setIcon(jobhub_icon(self._icon_name, color, 15))
        self.setIconSize(QSize(15, 15))

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            self.refresh_icon()


def button(text: str, role: str = "primary", *, icon: str | None = None, trailing: bool = False) -> CapsuleButton:
    """A capsule button. ``role`` is ``primary``, ``secondary``, ``quiet``, or ``danger``."""
    return CapsuleButton(text, role, icon, trailing=trailing)


class ChoiceButton(CapsuleButton):
    """A secondary capsule that opens a menu of exclusive choices (sort order, date range).

    Mirrors a small part of the ``QComboBox`` API. ``changed`` fires only when
    the user picks a different choice, never from ``set_value``.
    """

    changed = Signal(object)

    def __init__(self, icon: str, accessible_name: str, parent: QWidget | None = None):
        super().__init__("", "secondary", icon, parent)
        self.setAccessibleName(accessible_name)
        self._options: list[tuple[str, object]] = []
        self._value: object = None
        self.clicked.connect(self._open_menu)

    def add_option(self, text: str, value: object) -> None:
        self._options.append((text, value))
        if len(self._options) == 1:
            self.set_value(value)

    def value(self) -> object:
        return self._value

    def options(self) -> list[tuple[str, object]]:
        return list(self._options)

    def set_value(self, value: object) -> None:
        for text, option in self._options:
            if option == value:
                self._value = value
                self.setText(text)
                self.setToolTip(f"{self.accessibleName()}: {text}")
                self.updateGeometry()
                return

    def choose(self, value: object) -> None:
        """Select ``value`` as if the user picked it."""
        if value != self._value and any(option == value for _, option in self._options):
            self.set_value(value)
            self.changed.emit(value)

    def _open_menu(self) -> None:
        menu = QMenu(self)
        for text, value in self._options:
            action = menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(value == self._value)
            action.triggered.connect(lambda _=False, v=value: self.choose(v))
        menu.exec(self.mapToGlobal(QPoint(0, self.height() + 4)))


def paint_star_mark(painter: QPainter, center: QPointF, radius: float, color: QColor | str) -> None:
    """A filled five-point star (the "starred application" mark)."""
    path = QPainterPath()
    for index in range(10):
        angle = math.pi * index / 5 - math.pi / 2
        r = radius if index % 2 == 0 else radius * 0.45
        point = QPointF(center.x() + r * math.cos(angle), center.y() + r * math.sin(angle))
        path.moveTo(point) if index == 0 else path.lineTo(point)
    path.closeSubpath()
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(qcolor(color), max(1.0, radius * 0.22), Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(qcolor(color))
    painter.drawPath(path)
    painter.restore()


class _StepperButtons(QWidget):
    """The −/+ capsule of a ``Stepper``; press and hold repeats."""

    def __init__(self, spin: QSpinBox, parent: QWidget | None = None):
        super().__init__(parent)
        self._spin = spin
        self._hover = -1
        self._pressed = -1
        self.setFixedSize(66, 28)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._repeat = QTimer(self)
        self._repeat.timeout.connect(self._step)

    def _side(self, x: float) -> int:
        return 0 if x < self.width() / 2 else 1

    def _step(self) -> None:
        if self._pressed == 0:
            self._spin.stepDown()
        elif self._pressed == 1:
            self._spin.stepUp()
        self._repeat.setInterval(70)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self._pressed = self._side(event.position().x())
            self._step()
            self._repeat.start(420)
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        self._repeat.stop()
        self._pressed = -1
        self.update()

    def mouseMoveEvent(self, event) -> None:
        side = self._side(event.position().x())
        if side != self._hover:
            self._hover = side
            self.update()

    def leaveEvent(self, event) -> None:
        self._hover = -1
        self.update()

    def paintEvent(self, event) -> None:
        colors = current_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.fillPath(rounded_path(rect, rect.height() / 2), qcolor(colors["fill_quiet"]))
        half = rect.width() / 2
        for side in (0, 1):
            if side in (self._hover, self._pressed):
                part = QRectF(rect.left() + side * half, rect.top(), half, rect.height())
                painter.save()
                painter.setClipRect(part)
                painter.fillPath(rounded_path(rect, rect.height() / 2),
                                 qcolor(colors["fill_quiet"] if side == self._pressed else colors["fill_hover"]))
                painter.restore()
        painter.setPen(QPen(qcolor(colors["separator"]), 1))
        painter.drawLine(QPointF(rect.center().x(), rect.top() + 7), QPointF(rect.center().x(), rect.bottom() - 7))
        color = colors["text"] if self.isEnabled() else colors["disabled"]
        for side, name in ((0, "minus"), (1, "plus")):
            center = QPointF(rect.left() + half * side + half / 2, rect.center().y())
            painter.drawPixmap(center - QPointF(6.5, 6.5), icon_pixmap(name, color, 13, 2.2))
        painter.end()


class Stepper(QWidget):
    """A numeric field with an Apple-style −/+ capsule instead of spin arrows.

    The wrapped ``QSpinBox`` stays the source of truth (``value``/``setValue``).
    """

    def __init__(self, spin: QSpinBox, parent: QWidget | None = None, *, width: int = 96):
        super().__init__(parent)
        self.spin = spin
        spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        spin.setProperty("stepper", True)
        spin.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        spin.setFixedWidth(width)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(spin)
        self.buttons = _StepperButtons(spin)
        row.addWidget(self.buttons)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)


class StatusPill(QWidget):
    """A status capsule whose colors are resolved at paint time (theme-safe)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._status = ""
        self._text = ""
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_status(self, status: str, text: str) -> None:
        self._status, self._text = status, text
        self.setAccessibleName(text)
        self.updateGeometry()
        self.update()

    def text(self) -> str:
        return self._text

    def sizeHint(self) -> QSize:
        return QSize(QFontMetrics(ui_font(11, QFont.Weight.DemiBold)).horizontalAdvance(self._text) + 22, 22)

    def paintEvent(self, event) -> None:
        from .styles import color_for_status

        fg, bg = color_for_status(self._status)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_pill(painter, QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self._text, fg, bg)
        painter.end()


class ElidedLabel(QLabel):
    """A single-line label that truncates with an ellipsis instead of clipping."""

    def __init__(self, text: str = "", object_name: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        if object_name:
            self.setObjectName(object_name)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def minimumSizeHint(self) -> QSize:
        return QSize(12, super().minimumSizeHint().height())

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()))
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, int(self.alignment()), text)
        painter.end()


class PageHeader(QWidget):
    """Large title, supporting line, and trailing actions for a page."""

    def __init__(self, title: str, subtitle: str = "", eyebrow: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.eyebrow_label = label(eyebrow.upper(), "eyebrow")
        self.eyebrow_label.setVisible(bool(eyebrow))
        self.title_label = label(title, "pageTitle")
        self.subtitle_label = label(subtitle, "subtitle")
        self.subtitle_label.setVisible(bool(subtitle))
        text.addWidget(self.eyebrow_label)
        text.addWidget(self.title_label)
        text.addWidget(self.subtitle_label)
        row.addLayout(text, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        row.addLayout(self.actions)
        row.setAlignment(self.actions, Qt.AlignmentFlag.AlignBottom)

    def add_action(self, widget: QWidget) -> QWidget:
        self.actions.addWidget(widget)
        return widget

    def set_subtitle(self, text: str) -> None:
        self.subtitle_label.setText(text)
        self.subtitle_label.setVisible(bool(text))

    def set_eyebrow(self, text: str) -> None:
        self.eyebrow_label.setText(text.upper())
        self.eyebrow_label.setVisible(bool(text))


class EmptyState(QWidget):
    """Centered symbol, title, and guidance for an empty collection."""

    def __init__(self, icon_name: str, title: str, message: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(24, 28, 24, 28)
        column.setSpacing(8)
        column.addStretch(1)
        self.badge = IconBadge(icon_name, "neutral", 52)
        column.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(6)
        self.title_label = label(title, "emptyTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.title_label)
        self.message_label = label(message, "subtitle")
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message_label.setVisible(bool(message))
        column.addWidget(self.message_label)
        self.actions = QHBoxLayout()
        self.actions.addStretch(1)
        self.actions.addStretch(1)
        column.addLayout(self.actions)
        column.addStretch(1)

    def set_text(self, title: str, message: str = "") -> None:
        self.title_label.setText(title)
        self.message_label.setText(message)
        self.message_label.setVisible(bool(message))

    def add_action(self, widget: QWidget) -> None:
        self.actions.insertWidget(1, widget)


class _GroupBody(GlassPanel):
    """Glass body of a ``FormGroup``; paints inset hairlines between rows."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, radius=14)
        self.rows: list[QWidget] = []

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        visible = [row for row in self.rows if row.isVisible()]
        if len(visible) < 2:
            return
        painter = QPainter(self)
        painter.setPen(QPen(qcolor(current_colors()["separator"]), 1))
        for row in visible[:-1]:
            y = row.geometry().bottom() + 0.5
            painter.drawLine(QPointF(16, y), QPointF(self.width() - 16, y))
        painter.end()


class FormGroup(QWidget):
    """A System Settings-style group: heading outside, rows inside one glass box."""

    LABEL_WIDTH = 190

    def __init__(self, title: str = "", note: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        if title:
            heading = label(title, "groupTitle")
            heading.setContentsMargins(4, 0, 0, 0)
            column.addWidget(heading)
        self.body = _GroupBody()
        self._rows = QVBoxLayout(self.body)
        self._rows.setContentsMargins(0, 4, 0, 4)
        self._rows.setSpacing(0)
        column.addWidget(self.body)
        self.note_label = label(note, "groupNote")
        self.note_label.setContentsMargins(4, 0, 4, 0)
        self.note_label.setVisible(bool(note))
        column.addWidget(self.note_label)

    def add_row(self, title: str, field: QWidget, description: str = "", *, expand: bool | None = None) -> QWidget:
        """Add ``title`` on the left and ``field`` on the right.

        Text inputs expand to fill the row. Switches, steppers, and segmented
        controls hug the trailing edge unless ``expand`` says otherwise.
        """
        row = QWidget()
        row.setObjectName("formRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(16, 9, 14, 9)
        layout.setSpacing(14)
        grows = bool(field.property("formExpand"))   # chip lists and row editors
        if field.metaObject().className() == "QComboBox" and getattr(field, "isEditable", lambda: False)():
            expand = True if expand is None else expand   # an editable choice reads like a text field
        multiline = isinstance(field, (QTextEdit, QPlainTextEdit)) or grows
        if expand is None:
            expand = isinstance(field, (QLineEdit, QTextEdit, QPlainTextEdit)) or grows or (
                isinstance(field, ToggleSwitch) and bool(field.text())
            )
        if title or description:
            text = QVBoxLayout()
            text.setContentsMargins(0, 7 if multiline else 0, 0, 0)
            text.setSpacing(1)
            text.addWidget(label(title, "rowTitle"))
            if description:
                text.addWidget(label(description, "rowNote"))
            if multiline:
                text.addStretch(1)
            holder = QWidget()
            holder.setObjectName("formRow")
            holder.setLayout(text)
            if expand:
                holder.setFixedWidth(self.LABEL_WIDTH)
            else:
                holder.setMinimumWidth(self.LABEL_WIDTH)
            layout.addWidget(holder, 0 if expand else 1,
                             Qt.AlignmentFlag.AlignTop if multiline else Qt.AlignmentFlag.AlignVCenter)
            row.setProperty("rowTitle", title)
        elif not expand:
            layout.addStretch(1)
        layout.addWidget(field, 1 if expand else 0)
        self._rows.addWidget(row)
        self.body.rows.append(row)
        return row

    def add_widget(self, widget: QWidget) -> QWidget:
        """Add a full-width row, for multi-line inputs and inline notes."""
        row = QWidget()
        row.setObjectName("formRow")
        layout = QVBoxLayout(row)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.addWidget(widget)
        self._rows.addWidget(row)
        self.body.rows.append(row)
        return row


class Card(GlassPanel):
    """A titled glass card whose rows are separated by inset hairlines.

    ``header`` holds the title and an optional trailing action; ``body`` is a
    ``QVBoxLayout`` for arbitrary content. Rows added with ``add_row`` get
    separators; call ``clear_rows`` before repopulating.
    """

    def __init__(self, title: str = "", parent: QWidget | None = None, *, radius: float = 16.0,
                 tint: str | None = None, padding: tuple[int, int, int, int] = (18, 16, 18, 14)):
        super().__init__(parent, radius=radius, tint=tint)
        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(*padding)
        self._column.setSpacing(8)
        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        self.title_label = label(title, "cardTitle")
        self.title_label.setVisible(bool(title))
        self.header.addWidget(self.title_label, 1)
        self._column.addLayout(self.header)
        self.body = QVBoxLayout()
        self.body.setSpacing(0)
        self._column.addLayout(self.body)
        self.rows: list[QWidget] = []

    def add_header_action(self, widget: QWidget) -> QWidget:
        self.header.addWidget(widget)
        return widget

    def add_row(self, widget: QWidget) -> QWidget:
        self.body.addWidget(widget)
        self.rows.append(widget)
        self.update()
        return widget

    def clear_rows(self) -> None:
        for widget in self.rows:
            self.body.removeWidget(widget)
            widget.hide()
            widget.deleteLater()
        self.rows = []
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        visible = [row for row in self.rows if row.isVisible()]
        if len(visible) < 2:
            return
        painter = QPainter(self)
        painter.setPen(QPen(qcolor(current_colors()["separator"]), 1))
        for row in visible[:-1]:
            geometry = row.geometry()
            y = geometry.bottom() + 0.5
            inset = int(row.property("separatorInset") or 0)
            painter.drawLine(QPointF(geometry.left() + inset, y), QPointF(geometry.right(), y))
        painter.end()


class ActionRow(QWidget):
    """A full-width clickable row with a rounded hover highlight and trailing chevron."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None, *, chevron: bool = True):
        super().__init__(parent)
        self.setObjectName("formRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.layout_ = QHBoxLayout(self)
        self.layout_.setContentsMargins(8, 8, 8, 8)
        self.layout_.setSpacing(12)
        self._chevron = chevron
        self._hover = False

    def finish(self) -> None:
        """Append the trailing chevron after content widgets are added."""
        if self._chevron:
            arrow = QLabel()
            arrow.setPixmap(icon_pixmap("chevron-right", current_colors()["text_subtle"], 14, 2.0))
            self.layout_.addWidget(arrow)

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        if not self._hover:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillPath(rounded_path(QRectF(self.rect()).adjusted(0, 1, 0, -1), 10),
                         qcolor(current_colors()["fill_hover"]))
        painter.end()


class DirtyTracker(QObject):
    """Tracks unsaved edits across inputs so a page can offer Save only when needed."""

    changed = Signal(bool)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._dirty = False
        self._suspended = 0

    @property
    def dirty(self) -> bool:
        return self._dirty

    def watch(self, *widgets: QWidget) -> None:
        for widget in widgets:
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._mark)
            elif isinstance(widget, (QTextEdit, QPlainTextEdit)):
                widget.textChanged.connect(self._mark)
            elif isinstance(widget, QAbstractButton):
                widget.toggled.connect(self._mark)
            elif isinstance(widget, QAbstractSpinBox):
                widget.valueChanged.connect(self._mark)
            elif isinstance(widget, SegmentedControl):
                widget.currentIndexChanged.connect(self._mark)
            elif hasattr(widget, "changed"):
                widget.changed.connect(self._mark)   # TagEditor, KeyValueEditor, BulletEditor

    def _mark(self, *_args) -> None:
        if self._suspended or self._dirty:
            return
        self._dirty = True
        self.changed.emit(True)

    def reset(self) -> None:
        if self._dirty:
            self._dirty = False
            self.changed.emit(False)

    @contextmanager
    def suspended(self) -> Iterator[None]:
        self._suspended += 1
        try:
            yield
        finally:
            self._suspended -= 1


# --------------------------------------------------------------------------- transitions

class FadeStack(QStackedWidget):
    """A ``QStackedWidget`` whose page changes fade and glide into place.

    ``mode="rise"`` lifts the new page a few points (sidebar navigation);
    ``mode="push"`` slides it horizontally in the direction of travel (wizard steps).
    """

    def __init__(self, parent: QWidget | None = None, *, mode: str = "rise", distance: int = 14):
        super().__init__(parent)
        self._mode = mode
        self._distance = distance
        self._group: QParallelAnimationGroup | None = None
        self._page: QWidget | None = None

    def addTab(self, widget: QWidget, _title: str = "") -> int:  # noqa: N802 - QTabWidget parity
        return self.addWidget(widget)

    def setCurrentWidget(self, widget: QWidget) -> None:  # noqa: N802
        self.setCurrentIndex(self.indexOf(widget))

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        previous = self.currentIndex()
        if index == previous or not 0 <= index < self.count():
            return
        self._finish()
        super().setCurrentIndex(index)
        page = self.currentWidget()
        if page is None or reduced_motion() or not self.isVisible():
            return
        effect = QGraphicsOpacityEffect(page)
        effect.setOpacity(0.0)
        page.setGraphicsEffect(effect)
        fade = QPropertyAnimation(effect, b"opacity", self)
        fade.setDuration(Motion.PAGE - 40)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        fade.valueChanged.connect(lambda: refresh_window_materials(self))
        end = self.contentsRect().topLeft()
        if self._mode == "push":
            direction = 1 if index > previous else -1
            start = end + QPoint(self._distance * direction, 0)
        else:
            start = end + QPoint(0, self._distance)
        glide = QPropertyAnimation(page, b"pos", self)
        glide.setDuration(Motion.PAGE + 60)
        glide.setStartValue(start)
        glide.setEndValue(end)
        glide.setEasingCurve(QEasingCurve.Type.OutQuart)
        group = QParallelAnimationGroup(self)
        group.addAnimation(fade)
        group.addAnimation(glide)
        group.finished.connect(self._finish)
        page.move(start)
        self._group, self._page = group, page
        group.start()

    def _finish(self) -> None:
        group, page = self._group, self._page
        self._group = self._page = None
        if group is not None:
            group.stop()
            group.deleteLater()
        if page is not None:
            page.setGraphicsEffect(None)
            page.move(self.contentsRect().topLeft())
            refresh_window_materials(self)


# --------------------------------------------------------------------------- feedback

class Toast(QWidget):
    """A glass capsule notice that rises from the bottom edge of ``host``.

    ``tone`` is ``info``, ``success``, ``warning``, ``error``, or ``busy``
    (spinner, stays until replaced). ``timeout`` 0 keeps the toast visible.
    Click to dismiss.
    """

    MARGIN = 20
    HEIGHT = 42

    def __init__(self, host: QWidget):
        super().__init__(host)
        self.setObjectName("toast")
        self.hide()
        self._host = host
        self._area: QWidget | None = None
        self._text = ""
        self._tone = "info"
        self._progress = 0.0
        self._native_glass = GlassSurface(
            self, self.HEIGHT / 2,
            rect=lambda: QRectF(self.rect()).adjusted(self.MARGIN, self.MARGIN, -self.MARGIN, -self.MARGIN)
                         .translated(0, (1.0 - self._progress) * 18),
            opacity=lambda: self._progress,
        )
        self._phase = 0
        self._anim = QVariantAnimation(self)
        self._anim.valueChanged.connect(self._set_progress)
        self._anim.finished.connect(self._animation_done)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        self._spin = QTimer(self)
        self._spin.setInterval(83)
        self._spin.timeout.connect(self._tick)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        host.installEventFilter(self)

    @property
    def text(self) -> str:
        return self._text

    def set_area(self, area: QWidget) -> None:
        """Center over ``area`` (the page column) rather than the whole window."""
        self._area = area
        area.installEventFilter(self)

    @property
    def tone(self) -> str:
        return self._tone

    def show_message(self, text: str, tone: str = "info", timeout: int = 4500) -> None:
        self._text = " ".join(str(text).split())
        self._tone = tone
        self._relayout()
        if tone == "busy":
            self._spin.start()
        else:
            self._spin.stop()
        if not self.isVisible() or self._anim.endValue() == 0.0:
            self.show()
            self.raise_()
            if reduced_motion():
                self._set_progress(1.0)
            else:
                _animate(self._anim, self._progress, 1.0, Motion.SLOW, spring_curve(0.7))
        self.update()
        if timeout > 0:
            self._timer.start(timeout)
        else:
            self._timer.stop()

    def dismiss(self) -> None:
        self._timer.stop()
        if reduced_motion():
            self._set_progress(0.0)
            self._animation_done()
        else:
            _animate(self._anim, self._progress, 0.0, Motion.BASE, QEasingCurve.Type.InCubic)

    def _animation_done(self) -> None:
        if self._progress <= 0.001:
            self._spin.stop()
            self.hide()

    def _set_progress(self, value) -> None:
        self._progress = float(value)
        self._native_glass.refresh()
        self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 1) % 8
        self.update()

    def _relayout(self) -> None:
        metrics = QFontMetrics(ui_font(13, QFont.Weight.Medium))
        left, span = 0, self._host.width()
        if self._area is not None and self._area.isVisible():
            left = self._area.mapTo(self._host, QPoint(0, 0)).x()
            span = self._area.width()
        available = max(200, span - 80)
        body_width = min(available, metrics.horizontalAdvance(self._text) + 18 + 12 + 40)
        width = int(body_width + self.MARGIN * 2)
        height = self.HEIGHT + self.MARGIN * 2
        self.setGeometry(left + (span - width) // 2, self._host.height() - height - 6, width, height)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched in (self._host, self._area) and event.type() in (QEvent.Type.Resize, QEvent.Type.Move) and self._text:
            self._relayout()
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event) -> None:
        self.dismiss()

    def paintEvent(self, event) -> None:
        if self._progress <= 0.001:
            return
        colors = current_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(min(1.0, self._progress))
        painter.translate(0, (1.0 - self._progress) * 18)
        body = QRectF(self.rect()).adjusted(self.MARGIN, self.MARGIN, -self.MARGIN, -self.MARGIN)
        # Always back the text with a near-opaque material: a toast floats over
        # busy lists, and translucent glass alone lets their text show through.
        if not self._native_glass.active:
            paint_soft_shadow(painter, body, body.height() / 2, colors["glass_shadow"], blur=18)
            paint_glass(painter, body, body.height() / 2, material="thick")
        painter.fillPath(rounded_path(body, body.height() / 2), qcolor(colors["glass_thick"]))
        painter.setPen(QPen(qcolor(colors["glass_line"]), 1))
        painter.drawPath(rounded_path(body.adjusted(0.5, 0.5, -0.5, -0.5), body.height() / 2))
        icon_center = QPointF(body.left() + 22, body.center().y())
        if self._tone == "busy":
            paint_activity_indicator(painter, icon_center, 8, self._phase, colors["text_muted"])
        else:
            name, color = {
                "success": ("success", colors["green"]),
                "warning": ("alert", colors["amber"]),
                "error": ("alert", colors["red"]),
            }.get(self._tone, ("info", colors["accent"]))
            painter.drawPixmap(icon_center - QPointF(8, 8), icon_pixmap(name, color, 16, 2.0))
        font = ui_font(13, QFont.Weight.Medium)
        painter.setFont(font)
        painter.setPen(qcolor(colors["text"]))
        text_rect = QRectF(body.left() + 40, body.top(), body.width() - 58, body.height())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         QFontMetrics(font).elidedText(self._text, Qt.TextElideMode.ElideRight, int(text_rect.width())))
        painter.end()


def item_state_selected(option: QStyleOptionViewItem) -> bool:
    return bool(option.state & QStyle.StateFlag.State_Selected)


def item_state_hover(option: QStyleOptionViewItem) -> bool:
    return bool(option.state & QStyle.StateFlag.State_MouseOver)
