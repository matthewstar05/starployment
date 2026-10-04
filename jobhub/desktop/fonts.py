"""Typefaces for the desktop app.

The default typeface is Claude's: Anthropic Sans for interface text and
Anthropic Serif for page titles. Starployment does not ship these fonts. When
the Claude desktop app is installed, they are registered at runtime from its
bundle, and they are never copied, bundled, or embedded anywhere. Without the
Claude app, or with the System choice, the platform UI font is used.
"""
from __future__ import annotations

import sys
from pathlib import Path

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

_state: dict[str, object] = {"choice": CLAUDE, "loaded": None}


def claude_font_dirs() -> list[Path]:
    """Where an installed Claude desktop app keeps its fonts."""
    if sys.platform != "darwin":
        return []
    bundles = [Path("/Applications/Claude.app"), Path.home() / "Applications" / "Claude.app"]
    return [bundle / "Contents" / "Resources" / "fonts" for bundle in bundles]


def _load() -> bool:
    """Register Claude's fonts once per process. Returns whether both families exist."""
    if _state["loaded"] is None:
        families: set[str] = set()
        for folder in claude_font_dirs():
            if not folder.is_dir():
                continue
            for name in _FONT_FILES:
                path = folder / name
                if path.is_file():
                    font_id = QFontDatabase.addApplicationFont(str(path))
                    if font_id >= 0:
                        families.update(QFontDatabase.applicationFontFamilies(font_id))
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
