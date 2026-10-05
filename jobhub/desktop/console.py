"""The sidebar activity console: a small terminal-style log of everything happening.

It replaces pop-up notices. Each line is ``HH:MM message`` colored by tone.
Progress messages (tone ``busy``) update their line in place instead of piling
up. Stored events from the database (scans, inbox updates, application
results) are merged in so the log also shows work from earlier sessions.
"""
from __future__ import annotations

import html
from collections import deque
from datetime import datetime

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFont, QFontDatabase, QTextCursor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPlainTextEdit, QVBoxLayout, QWidget

from .format import activity_label, activity_tone, parse_time
from .theme import current_colors
from .widgets import ActivityIndicator, StatusDot, label

MAX_LINES = 400


def tone_colors() -> dict[str, str]:
    """Line colors for the current theme: the palette's semantic tones, so the log reads
    on a dark terminal in Dark mode and on a light panel in Light mode."""
    c = current_colors()
    return {"success": c["green"], "green": c["green"], "warning": c["amber"], "amber": c["amber"],
            "error": c["red"], "red": c["red"], "busy": c["info"], "accent": c["info"],
            "info": c["text"], "neutral": c["text_muted"], "time": c["text_subtle"]}


class ActivityConsole(QFrame):
    """Status header plus a scrolling, read-only log."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("console")
        column = QVBoxLayout(self)
        column.setContentsMargins(10, 9, 8, 8)
        column.setSpacing(6)
        header = QHBoxLayout()
        header.setSpacing(8)
        self.spinner = ActivityIndicator(12)
        self.spinner.hide()
        self.status_dot = StatusDot("green", 8)
        header.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        text = QVBoxLayout()
        text.setSpacing(0)
        self.status_title = label("Ready", "consoleTitle", wrap=False)
        self.status_detail = label("", "consoleDetail", wrap=False)
        text.addWidget(self.status_title)
        text.addWidget(self.status_detail)
        header.addLayout(text, 1)
        column.addLayout(header)
        self.log = QPlainTextEdit()
        self.log.setObjectName("consoleLog")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(MAX_LINES)
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.log.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.log.setAccessibleName("Activity log")
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPixelSize(10)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.log.setFont(font)
        column.addWidget(self.log, 1)
        self._busy: tuple | None = None
        self._entries: deque[tuple[datetime, str, str]] = deque(maxlen=MAX_LINES)
        self._drawn_colors: dict[str, str] | None = None
        self._seen_events: set[object] = set()
        self.last_text = ""
        self.last_tone = "info"

    # -------------------------------------------------------------- status
    def set_status(self, title: str, detail: str, tone: str, busy: bool) -> None:
        self.status_title.setText(title)
        self.status_detail.setText(detail)
        self.status_detail.setVisible(bool(detail))
        if busy:
            self.status_dot.hide()
            self.spinner.start()
        else:
            self.spinner.stop()
            self.status_dot.set_tone(tone)
            self.status_dot.show()

    # ----------------------------------------------------------------- log
    def _line(self, when: datetime, text: str, tone: str) -> str:
        colors = tone_colors()
        color = colors.get(tone, colors["info"])
        return (f'<span style="color:{colors["time"]}">{when:%H:%M}</span>&nbsp;'
                f'<span style="color:{color}">{html.escape(text)}</span>')

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            self._rerender()

    def _rerender(self) -> None:
        """Redraw every line in the current theme's colors (only when they changed)."""
        if not getattr(self, "_entries", None):
            return
        colors = tone_colors()
        if colors == self._drawn_colors:
            return
        self._drawn_colors = colors
        bar = self.log.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        self.log.clear()
        for entry in self._entries:   # one block per line, as _append writes them
            self.log.appendHtml(self._line(*entry))
        if at_bottom:
            bar.setValue(bar.maximum())

    def _drop_last_line(self) -> None:
        if self._entries:
            self._entries.pop()
        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        if self.log.blockCount() > 1:
            cursor.deletePreviousChar()

    def _append(self, text: str, tone: str, when: datetime | None) -> None:
        bar = self.log.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        when = when or datetime.now()
        self._entries.append((when, text, tone))
        self._drawn_colors = tone_colors()
        self.log.appendHtml(self._line(when, text, tone))
        if at_bottom:
            bar.setValue(bar.maximum())

    def write(self, text: str, tone: str = "info", when: datetime | None = None) -> None:
        """Append a line. A ``busy`` progress line is replaced by the next message."""
        text = " ".join(str(text).split())
        if not text:
            return
        self.last_text, self.last_tone = text, tone
        if self._busy is not None:
            self._drop_last_line()
        self._append(text, tone, when)
        self._busy = (text, tone, when) if tone == "busy" else None

    def add_events(self, events: list[dict], applications: list[dict] | None = None) -> None:
        """Merge stored events (newest first, as the database returns them) in time order."""
        by_job = {str(a.get("job_id")): a for a in (applications or []) if a.get("job_id")}
        fresh = [e for e in events if e.get("id") not in self._seen_events]
        if not self._seen_events:
            fresh = fresh[:40]
        lines = []
        for event in reversed(fresh):
            message = str(event.get("msg") or "")
            text = activity_label(message, by_job)
            if text is None:
                continue
            when = parse_time(event.get("ts"))
            lines.append((text, activity_tone(message, str(event.get("level") or "")),
                          when.astimezone() if when else None))
        for event in events:
            self._seen_events.add(event.get("id"))
        if not lines:
            return
        busy = self._busy
        if busy is not None:
            self._drop_last_line()       # keep the live progress line at the bottom
        for text, tone, when in lines:
            self._append(text, tone, when)
        if busy is not None:
            self._append(*busy)
