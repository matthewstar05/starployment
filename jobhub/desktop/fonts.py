"""Typefaces for the desktop app.

The default typeface is Claude's: Anthropic Sans for interface text and
Anthropic Serif for page titles. Starployment does not ship these fonts. When
the Claude desktop app is installed, they are registered at runtime from its
bundle, and they are never copied, bundled, or embedded anywhere. Without the
Claude app, or with the System choice, the platform UI font is used.

On macOS the bundle holds TrueType files. On Windows the Claude app (an MSIX
package) holds only hash-named WOFF2 web fonts, which Qt cannot read, so they
are decompressed in memory with fontTools, their family names are set to the
macOS names, and the bytes are registered with Qt. Nothing is written to disk.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QFontDatabase

CLAUDE = "Claude"
SYSTEM = "System"
CHOICES = (CLAUDE, SYSTEM)

SANS = "Anthropic Sans"
SERIF = "Anthropic Serif"
_FONT_FILES = (
    "AnthropicSans-Romans-Variable-25x258.ttf",
    "AnthropicSans-Italics-Variable-25x258.ttf",
    "AnthropicSerif-Romans-Variable-25x258.ttf",
    "AnthropicSerif-Italics-Variable-25x258.ttf",
)

# Windows web fonts, by family: hashed file-name patterns, variable build first.
_WOFF2_FILES = (
    (SANS, ("AnthropicSans-Roman-Variable-*.woff2", "AnthropicSans-Roman-Web.*.woff2")),
    (SERIF, ("AnthropicSerif-Roman-Variable-*.woff2", "AnthropicSerif-Roman-Web.*.woff2")),
)
# Per-user registry of installed MSIX packages. The WindowsApps folder itself
# cannot be listed by a normal user, but each package's root can be read.
_PACKAGES_KEY = r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\Repository\Packages"
_WINDOWS_RUNTIME = Path("app") / "resources" / "ion-dist" / "_frame-rt" / "_runtime"

_state: dict[str, object] = {"choice": CLAUDE, "loaded": None}


def _windows_claude_roots() -> list[Path]:
    """Install folders of the Claude MSIX package, newest version first."""
    try:
        import winreg
    except ImportError:
        return []
    roots: list[tuple[str, Path]] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PACKAGES_KEY) as packages:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(packages, index)
                except OSError:
                    break
                index += 1
                if not name.startswith("Claude_"):
                    continue
                try:
                    with winreg.OpenKey(packages, name) as package:
                        root, _type = winreg.QueryValueEx(package, "PackageRootFolder")
                except OSError:
                    continue
                roots.append((name, Path(root)))
    except OSError:
        return []

    def version(entry: tuple[str, Path]) -> tuple[int, ...]:
        parts = entry[0].split("_")
        try:
            return tuple(int(part) for part in parts[1].split("."))
        except (IndexError, ValueError):
            return ()

    return [root for _name, root in sorted(roots, key=version, reverse=True)]


def claude_font_dirs() -> list[Path]:
    """Where an installed Claude desktop app keeps its fonts."""
    if sys.platform == "darwin":
        bundles = [Path("/Applications/Claude.app"), Path.home() / "Applications" / "Claude.app"]
        return [bundle / "Contents" / "Resources" / "fonts" for bundle in bundles]
    if sys.platform == "win32":
        return [root / _WINDOWS_RUNTIME for root in _windows_claude_roots()]
    return []


def _register_ttf_files(folder: Path) -> set[str]:
    families: set[str] = set()
    for name in _FONT_FILES:
        path = folder / name
        if path.is_file():
            font_id = QFontDatabase.addApplicationFont(str(path))
            if font_id >= 0:
                families.update(QFontDatabase.applicationFontFamilies(font_id))
    return families


def _woff2_as_truetype(path: Path, family: str) -> bytes | None:
    """Decompress a WOFF2 file in memory and give it the macOS family name."""
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        return None
    try:
        font = TTFont(str(path))
        font.flavor = None
        names = font["name"]
        # 1 = family, 16 = typographic family; the web build says "... Variable" or "... Web".
        for record in list(names.names):
            if record.nameID in (1, 16):
                names.setName(family, record.nameID, record.platformID, record.platEncID, record.langID)
        buffer = io.BytesIO()
        font.save(buffer)
        return buffer.getvalue()
    except Exception:  # noqa: BLE001 - a damaged or unexpected font must never stop the app
        return None


def _register_woff2_files(folder: Path) -> set[str]:
    families: set[str] = set()
    for family, patterns in _WOFF2_FILES:
        for pattern in patterns:
            matches = sorted(folder.glob(pattern))
            data = _woff2_as_truetype(matches[0], family) if matches else None
            if data is None:
                continue
            font_id = QFontDatabase.addApplicationFontFromData(QByteArray(data))
            if font_id >= 0:
                families.update(QFontDatabase.applicationFontFamilies(font_id))
                break
    return families


def _load() -> bool:
    """Register Claude's fonts once per process. Returns whether both families exist."""
    if _state["loaded"] is None:
        families: set[str] = set()
        for folder in claude_font_dirs():
            if not folder.is_dir():
                continue
            families |= _register_ttf_files(folder)
            if not {SANS, SERIF} <= families:
                families |= _register_woff2_files(folder)
            if {SANS, SERIF} <= families:
                break
        _state["loaded"] = {SANS, SERIF} <= families
    return bool(_state["loaded"])


def normalize(choice: object) -> str:
    text = str(choice or "").strip().title()
    return text if text in CHOICES else CLAUDE


def set_typeface(choice: object) -> str:
    """Select Claude or System type. Returns the choice that is actually in use."""
    _state["choice"] = normalize(choice)
    return active()


def active() -> str:
    """``Claude`` when Claude's fonts are selected and available, else ``System``."""
    return CLAUDE if _state["choice"] == CLAUDE and _load() else SYSTEM


def claude_available() -> bool:
    return _load()


def ui_family() -> str | None:
    """Family for interface text, or None for the platform default."""
    if active() == CLAUDE:
        return SANS
    return ".AppleSystemUIFont" if sys.platform == "darwin" else None


def display_family() -> str | None:
    """Family for page titles: Claude's serif, or the interface family."""
    return SERIF if active() == CLAUDE else ui_family()
