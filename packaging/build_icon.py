"""Build Starployment's platform icon assets from the canonical SVG artwork."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "packaging" / "assets"
SVG = ASSETS / "starployment-icon.svg"
PNG = ASSETS / "starployment-icon-1024.png"
PNG_256 = ASSETS / "starployment-icon-256.png"
ICO = ASSETS / "starployment.ico"
ICNS = ASSETS / "starployment.icns"


def render_png(size: int) -> QImage:
    renderer = QSvgRenderer(str(SVG))
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    renderer.render(painter)
    painter.end()
    return image


def image_bytes(image: QImage, fmt: str = "PNG") -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, fmt)
    return bytes(buffer.data())


def make_icns() -> bool:
    iconutil = shutil.which("iconutil")
    if not iconutil:
        return False
    with tempfile.TemporaryDirectory(prefix="starployment.iconset.") as temp:
        iconset = Path(temp) / "Starployment.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            image = Image.open(PNG).convert("RGBA")
            image.resize((size, size), Image.Resampling.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
            image.resize((size * 2, size * 2), Image.Resampling.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
        subprocess.run([iconutil, "--convert", "icns", "--output", str(ICNS), str(iconset)], check=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    if not SVG.is_file():
        raise SystemExit(f"Missing icon source: {SVG}")
    app = QGuiApplication.instance() or QGuiApplication([])
    PNG.write_bytes(image_bytes(render_png(1024)))
    PNG_256.write_bytes(image_bytes(render_png(256)))
    with Image.open(PNG) as image:
        image.convert("RGBA").save(ICO, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    icns_created = make_icns()
    if not args.quiet:
        print(f"Generated {PNG.name}, {PNG_256.name}, {ICO.name}" + (f", {ICNS.name}" if icns_created else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
