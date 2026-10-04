"""The activity console follows Light/Dark, and the Claude/System font choice."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from jobhub.desktop import fonts


def test_console_lines_recolor_when_the_theme_changes():
    from jobhub.desktop.console import ActivityConsole
    from jobhub.desktop.styles import apply_appearance, stylesheet
    from jobhub.desktop.theme import DARK, LIGHT, PALETTES

    app = QApplication.instance() or QApplication([])
    try:
        apply_appearance(app, "Dark")
        console = ActivityConsole()
        console.write("Application sent", "success")
        console.write("Preparing applications", "busy")
        assert PALETTES[DARK]["green"].lower() in console.log.document().toHtml().lower()

        apply_appearance(app, "Light")
        app.processEvents()
        html = console.log.document().toHtml().lower()
        assert PALETTES[LIGHT]["green"].lower() in html
        assert PALETTES[DARK]["green"].lower() not in html
        assert "#0E0E12" not in stylesheet("Light")   # the dark terminal fill is Dark mode only
        assert "#0E0E12" in stylesheet("Dark")

        # After a recolor the busy line is still replaced by the next message.
        console.write("Done", "info")
        lines = console.log.toPlainText().splitlines()
        assert len(lines) == 2 and lines[0].endswith("Application sent") and lines[1].endswith("Done")
    finally:
        apply_appearance(app, "System")


def test_system_font_is_used_when_claude_is_not_installed(monkeypatch):
    from jobhub.desktop.styles import app_font, apply_typeface, stylesheet

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(fonts, "_state", {"choice": fonts.CLAUDE, "loaded": None})
    monkeypatch.setattr(fonts, "claude_font_dirs", lambda: [])
    try:
        assert apply_typeface(app, "Claude") == fonts.SYSTEM
        assert "Anthropic" not in stylesheet("Light")
        assert not app_font().family().startswith("Anthropic")
    finally:
        monkeypatch.undo()
        apply_typeface(app, "Claude")


@pytest.mark.skipif(not any((d / "AnthropicSans-Romans-Variable-25x258.ttf").is_file() for d in fonts.claude_font_dirs()),
                    reason="the Claude desktop app is not installed here")
def test_claude_font_choice_uses_anthropic_sans_and_serif_titles():
    from jobhub.desktop.styles import app_font, apply_typeface, stylesheet

    app = QApplication.instance() or QApplication([])
    try:
        assert apply_typeface(app, "Claude") == fonts.CLAUDE
        assert app_font().family() == fonts.SANS
        sheet = stylesheet("Light")
        assert f"font-family: '{fonts.SANS}'" in sheet
        title_rule = next(line for line in sheet.splitlines() if "QLabel#pageTitle" in line)
        assert fonts.SERIF in title_rule

        assert apply_typeface(app, "System") == fonts.SYSTEM
        assert "Anthropic" not in stylesheet("Light")
    finally:
        apply_typeface(app, "Claude")
