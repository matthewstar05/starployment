"""Load the bundled Lucide SVG icons as color-aware Qt icons and pixmaps."""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


_ICON_FILES = {
    "dashboard": "layout-dashboard.svg",
    "jobs": "briefcase-business.svg",
    "applications": "send.svg",
    "profile": "user-round.svg",
    "settings": "settings.svg",
    "refresh": "refresh-cw.svg",
    "file": "file-text.svg",
    "file-up": "file-up.svg",
    "check": "check.svg",
    "search": "search.svg",
    "chevron-left": "chevron-left.svg",
    "chevron-right": "chevron-right.svg",
    "chevron-up": "chevron-up.svg",
    "chevron-down": "chevron-down.svg",
    "minus": "minus.svg",
    "plus": "plus.svg",
    "external": "external-link.svg",
    "success": "circle-check.svg",
    "alert": "circle-alert.svg",
    "info": "info.svg",
    "clock": "clock.svg",
    "trending": "trending-up.svg",
    "layers": "layers.svg",
    "zap": "zap.svg",
    "close": "x.svg",
    "tracker": "sheet.svg",
    "luna": "sparkles.svg",
    "mail": "mail.svg",
    "star": "star.svg",
    "trash": "trash-2.svg",
    "folder": "folder-open.svg",
    "plug": "plug.svg",
    "reset": "rotate-ccw.svg",
    "download": "download.svg",
    "eye": "eye.svg",
    "pin": "pin.svg",
    "education": "graduation-cap.svg",
    "code": "code.svg",
    "award": "award.svg",
    "sort": "arrow-up-down.svg",
    "rows": "rows-3.svg",
}
_DEFAULT_COLOR = "#5b6573"


def icon_path(name: str) -> Path:
    """Return the path for a named bundled icon."""
    try:
        filename = _ICON_FILES[name]
    except KeyError as error:
        raise ValueError(f"Unknown Starployment icon: {name}") from error

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "assets" / "lucide" / filename
    return Path(__file__).resolve().parents[2] / "packaging" / "assets" / "lucide" / filename


@lru_cache(maxsize=512)
def icon_pixmap(name: str, color: str = _DEFAULT_COLOR, size: int = 20,
                stroke: float = 2.0, scale: int = 2) -> QPixmap:
    """Render a Lucide SVG into a crisp, color-tinted, cached pixmap.

    ``stroke`` overrides Lucide's default 2px line weight; Apple's SF Symbols
    read lighter, so navigation uses ~1.8. Pixmaps are cached by arguments, so
    delegates can call this from ``paint`` without re-parsing SVG each frame.
    """
    if size < 1:
        raise ValueError("Icon size must be a positive integer")
    tint = QColor(color)
    if not tint.isValid():
        raise ValueError(f"Invalid icon color: {color}")

    source = icon_path(name).read_bytes().replace(
        b"currentColor", tint.name(QColor.NameFormat.HexRgb).encode("ascii")
    )
    if stroke != 2.0:
        source = source.replace(b'stroke-width="2"', f'stroke-width="{stroke:g}"'.encode("ascii"))
    renderer = QSvgRenderer(QByteArray(source))
    if not renderer.isValid():
        raise ValueError(f"Unable to render Starployment icon: {name}")

    pixmap = QPixmap(QSize(size * scale, size * scale))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setOpacity(tint.alphaF())
    renderer.render(painter, QRectF(0, 0, size * scale, size * scale))
    painter.end()
    pixmap.setDevicePixelRatio(scale)
    return pixmap


def jobhub_icon(name: str, color: str = _DEFAULT_COLOR, size: int = 20, stroke: float = 2.0) -> QIcon:
    """Return a color-tinted QIcon. Color is explicit so callers follow the active palette."""
    return QIcon(icon_pixmap(name, color, size, stroke))
