"""The glass sheet that asks before JARVIS ALL or ALL+ sends applications without review."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLayout, QVBoxLayout, QWidget

from ..native_glass import prepare_window
from ..widgets import GlassPanel, IconBadge, button, label, paint_backdrop

# (icon, tone, title, note) per row; ALL+ adds its wider net and pace.
_SHARED = (
    ("luna", "indigo", "Prepares and answers",
     "Builds each application and fills open questions with AI Autofill. Assumptions are labeled "
     "and stay with that application, never in your saved answers."),
    ("applications", "accent", "Sends without asking",
     "Submits straight away. Only an employer confirmation counts as applied, and nothing is retried."),
    ("star", "amber", "Still waits for you",
     "Starred roles, priority companies, Workday, and uploads it cannot make, such as a transcript."),
)
_PACE = {
    "all": (("clock", "green", "Gentle on your Mac",
             "Background priority, half your saved concurrency, and a pause while the Mac is busy."),),
    "all_plus": (("trending", "green", "A wider net",
                  "Also older postings still listed, twice your per-employer limit, and on Workday it "
                  "guesses whatever your resume and saved answers leave open (logged on each application)."),
                 ("zap", "red", "Full speed, best overnight",
                  "Your full saved concurrency and scans twice as often. It slows down only when the Mac "
                  "nears its memory or CPU limits, so it never freezes it.")),
}
_TITLES = {"all": ("Turn on JARVIS ALL?", "Turn on ALL"), "all_plus": ("Turn on JARVIS ALL+?", "Turn on ALL+")}
_LEADS = {
    "all": "JARVIS works through new and older open postings by itself while you work or sleep.",
    "all_plus": "Everything ALL does, faster and wider. Meant for nights when you are away from the Mac.",
}


SHEET_WIDTH = 500
_SIDES = 28
# Text width inside a row: sheet sides, row padding, badge, and spacing. Wrapped labels
# need a definite width to report their full height.
_TEXT_WIDTH = SHEET_WIDTH - 2 * _SIDES - 14 - 16 - 30 - 12


class _Row(QWidget):
    def __init__(self, icon: str, tone: str, title: str, note: str):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 11, 16, 11)
        row.setSpacing(12)
        row.addWidget(IconBadge(icon, tone, 30), 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(2)
        for line in (label(title, "rowPrimary"), label(note, "rowSecondary")):
            line.setFixedWidth(_TEXT_WIDTH)
            text.addWidget(line)
        row.addLayout(text, 1)


class JarvisConfirmSheet(QDialog):
    """Asks once before a mode that sends without review. ``exec()`` is Accepted to turn it on."""

    def __init__(self, mode: str, parent: QWidget | None = None):
        super().__init__(parent)
        mode = mode if mode in _TITLES else "all"
        self.mode = mode
        title, confirm = _TITLES[mode]
        self.setWindowTitle(title)
        prepare_window(self)
        self.setModal(True)
        column = QVBoxLayout(self)
        column.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        column.setContentsMargins(_SIDES, 26, _SIDES, 22)
        column.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(14)
        self.badge = IconBadge("zap", "red" if mode == "all_plus" else "accent", 48)
        head.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        heading = QVBoxLayout()
        heading.setSpacing(4)
        heading.addWidget(label("JARVIS · " + ("ALL+" if mode == "all_plus" else "ALL"), "eyebrow"))
        self.title = label(title, "sheetTitle")
        heading.addWidget(self.title)
        lead = label(_LEADS[mode], "subtitle")
        lead.setFixedWidth(SHEET_WIDTH - 2 * _SIDES - 48 - 14)
        heading.addWidget(lead)
        head.addLayout(heading, 1)
        column.addLayout(head)
        column.addSpacing(18)

        self.panel = GlassPanel(radius=14)
        rows = QVBoxLayout(self.panel)
        rows.setContentsMargins(0, 4, 0, 4)
        rows.setSpacing(0)
        self.rows = [_Row(*spec) for spec in (*_SHARED, *_PACE[mode])]
        for row in self.rows:
            rows.addWidget(row)
        column.addWidget(self.panel)
        column.addSpacing(12)
        note = label("Turning it on is standing permission to send these answers. Switch JARVIS off from "
                     "the sidebar badge at any time; an application already sending finishes.", "caption")
        note.setFixedWidth(SHEET_WIDTH - 2 * _SIDES)
        column.addWidget(note)
        column.addSpacing(18)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch(1)
        self.cancel = button("Cancel", "secondary")
        self.cancel.clicked.connect(self.reject)
        self.confirm = button(confirm, "primary", icon="zap")
        self.confirm.clicked.connect(self.accept)
        # Return should not send applications by accident: Cancel is the default.
        self.confirm.setAutoDefault(False)
        self.cancel.setDefault(True)
        actions.addWidget(self.cancel)
        actions.addWidget(self.confirm)
        column.addLayout(actions)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        paint_backdrop(painter, self)
