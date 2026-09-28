"""Profile page: grouped, System Settings-style candidate facts."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QLineEdit, QScrollArea, QTextEdit, QVBoxLayout, QWidget

from ..widgets import DirtyTracker, FormGroup, PageHeader, button

CONTACT_FIELDS = (
    ("first_name", "First name"),
    ("last_name", "Last name"),
    ("email", "Email"),
    ("phone", "Phone"),
)
ADDRESS_FIELDS = (
    ("address_line1", "Address"),
    ("address_line2", "Address line 2"),
    ("city", "City"),
    ("state", "State / region"),
    ("region", "Region / province"),
    ("postal_code", "Postal code"),
    ("country", "Country"),
)
LINK_FIELDS = (
    ("linkedin", "LinkedIn"),
    ("github", "GitHub"),
    ("website", "Website"),
)


def parse_facts(text: str) -> dict[str, str]:
    """``label | value`` lines into a dict; lines without a label are ignored."""
    facts: dict[str, str] = {}
    for line in text.splitlines():
        if "|" in line:
            key, value = line.split("|", 1)
            if key.strip():
                facts[key.strip()] = value.strip()
    return facts


class ProfileView(QWidget):
    save_requested = Signal(dict)
    resume_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.tracker = DirtyTracker(self)
        column = QVBoxLayout(self)
        column.setContentsMargins(30, 26, 30, 0)
        column.setSpacing(14)
        self.header = PageHeader("Profile", "The facts JobHub uses to complete applications.")
        self.save_button = button("Save changes")
        self.save_button.setEnabled(False)
        self.save_button.setToolTip("Save profile changes (⌘S)")
        self.save_button.clicked.connect(self._save)
        self.header.add_action(self.save_button)
        column.addWidget(self.header)
        self.tracker.changed.connect(self._dirty_changed)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        column.addWidget(scroll, 1)
        page = QWidget()
        page.setObjectName("page")
        scroll.setWidget(page)
        groups = QVBoxLayout(page)
        groups.setContentsMargins(0, 2, 6, 26)
        groups.setSpacing(22)

        self.fields: dict[str, QLineEdit] = {}
        contact = FormGroup("Contact", "Used for employer contact fields.")
        address = FormGroup("Location", "Many forms require a current city and country.")
        professional = FormGroup("Professional")
        links = FormGroup("Online presence", "Links you want to share with employers.")
        for group, spec in ((contact, CONTACT_FIELDS), (address, ADDRESS_FIELDS), (links, LINK_FIELDS)):
            for key, title in spec:
                edit = QLineEdit()
                edit.setPlaceholderText(title)
                edit.setAccessibleName(title)
                self.fields[key] = edit
                group.add_row(title, edit)
        headline = QLineEdit()
        headline.setPlaceholderText("For example, Software engineer focused on data systems")
        self.fields["headline"] = headline
        professional.add_row("Headline", headline)
        self.summary = QTextEdit()
        self.summary.setFixedHeight(104)
        self.summary.setAcceptRichText(False)
        professional.add_row("Summary", self.summary)
        self.skills = QLineEdit()
        self.skills.setPlaceholderText("Comma-separated skills")
        professional.add_row("Skills", self.skills, "Used for ranking and tailoring.")
        self.custom_facts = QTextEdit()
        self.custom_facts.setFixedHeight(96)
        self.custom_facts.setAcceptRichText(False)
        self.custom_facts.setPlaceholderText("One fact per line: label | value")
        professional.add_row("Additional facts", self.custom_facts, "label | value per line")
        for group in (contact, address, professional, links):
            groups.addWidget(group)
        groups.addStretch(1)
        self.tracker.watch(*self.fields.values(), self.summary, self.skills, self.custom_facts)

    @property
    def dirty(self) -> bool:
        return self.tracker.dirty

    def _dirty_changed(self, dirty: bool) -> None:
        self.save_button.setEnabled(dirty)
        self.header.set_subtitle("You have unsaved changes." if dirty
                                 else "The facts JobHub uses to complete applications.")

    def set_profile(self, profile: Any) -> None:
        data = profile.model_dump() if hasattr(profile, "model_dump") else (profile or {})
        with self.tracker.suspended():
            for key, edit in self.fields.items():
                edit.setText(str(data.get(key, "") or ""))
            self.summary.setPlainText(str(data.get("summary", "") or ""))
            self.skills.setText(", ".join(data.get("skills", []) or []))
            self.custom_facts.setPlainText(
                "\n".join(f"{k} | {v}" for k, v in (data.get("custom_facts", {}) or {}).items())
            )
        self.tracker.reset()

    def _save(self) -> None:
        self.save_requested.emit({
            **{key: edit.text().strip() for key, edit in self.fields.items()},
            "summary": self.summary.toPlainText().strip(),
            "skills": [x.strip() for x in self.skills.text().split(",") if x.strip()],
            "custom_facts": parse_facts(self.custom_facts.toPlainText()),
        })
