"""Profile page: your details, resume builder, resume files, and saved answers.

Details, the resume builder, and answers share one Save button and one
``DirtyTracker``, so a background refresh never overwrites unsaved edits.
Resume files and the "resume to send" choice apply immediately.
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QFrame, QLineEdit, QScrollArea, QStackedWidget, QTextEdit, QVBoxLayout, QWidget

from ..editors import SUGGESTED_FACTS, SUGGESTED_QUESTIONS, KeyValueEditor, yes_no_control
from ..widgets import DirtyTracker, FormGroup, PageHeader, SegmentedControl, button
from .resume_builder import ResumeBuilder, ResumeFiles

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
YES_NO_ANSWERS = (
    ("work_authorized_us", "Authorized to work in the US"),
    ("requires_sponsorship", "Needs visa sponsorship"),
    ("us_citizen_or_permanent_resident", "US citizen or permanent resident"),
    ("willing_to_relocate", "Willing to relocate"),
)
TEXT_ANSWERS = (
    ("salary_expectation", "Salary expectation", "e.g. $120,000 or Negotiable", ()),
    ("available_start", "Available to start", "e.g. June 2026", ()),
    ("how_heard", "How you heard about roles", "", ("Company website", "LinkedIn", "Job board", "Referral",
                                                    "University career site", "Recruiter")),
    ("preferred_name", "Preferred name", "Only if different from your first name", ()),
    ("pronouns", "Pronouns", "Optional", ("she/her", "he/him", "they/them")),
)
TABS = (("Details", "details"), ("Resume builder", "builder"), ("Resume files", "files"), ("Answers", "answers"))
SUBTITLE = "The facts and material Starployment uses to apply for you."


def parse_facts(text: str) -> dict[str, str]:
    """``label | value`` lines into a dict (kept for importing older notes)."""
    facts: dict[str, str] = {}
    for line in text.splitlines():
        if "|" in line:
            key, value = line.split("|", 1)
            if key.strip():
                facts[key.strip()] = value.strip()
    return facts


def _scroll(widget: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    widget.setObjectName("page")
    scroll.setWidget(widget)
    return scroll


class ProfileView(QWidget):
    save_requested = Signal(dict)
    resume_requested = Signal()
    import_resume_requested = Signal(str)
    preview_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.tracker = DirtyTracker(self)
        self._answers: dict = {}
        column = QVBoxLayout(self)
        column.setContentsMargins(30, 26, 30, 0)
        column.setSpacing(14)
        self.header = PageHeader("Profile", SUBTITLE)
        self.save_button = button("Save changes")
        self.save_button.setEnabled(False)
        self.save_button.setToolTip("Save profile changes (⌘S)")
        self.save_button.clicked.connect(self._save)
        self.header.add_action(self.save_button)
        column.addWidget(self.header)
        self.tracker.changed.connect(self._dirty_changed)
        self.tabs = SegmentedControl()
        for title, key in TABS:
            self.tabs.addItem(title, key)
        column.addWidget(self.tabs, 0, Qt.AlignmentFlag.AlignLeft)
        self.pages = QStackedWidget()
        column.addWidget(self.pages, 1)
        self.tabs.currentIndexChanged.connect(self.pages.setCurrentIndex)

        # Details ------------------------------------------------------------
        details = QWidget()
        groups = QVBoxLayout(details)
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
        professional.add_row("Summary", self.summary, "Sentences most relevant to a role lead its resume.")
        facts = FormGroup("Other facts about you", "Anything an application might ask that is not above. "
                                                    "Pick a label or type your own.")
        self.custom_facts = KeyValueEditor(suggestions=SUGGESTED_FACTS, key_placeholder="Label",
                                           value_placeholder="Your answer", add_text="Add a fact")
        facts.add_widget(self.custom_facts)
        for group in (contact, address, professional, links, facts):
            groups.addWidget(group)
        groups.addStretch(1)
        self.pages.addWidget(_scroll(details))

        # Resume builder ----------------------------------------------------------
        builder_page = QWidget()
        builder_page.setObjectName("page")
        builder_layout = QVBoxLayout(builder_page)
        builder_layout.setContentsMargins(0, 2, 0, 18)
        self.builder = ResumeBuilder()
        self.builder.import_requested.connect(self.import_resume_requested)
        self.builder.preview_requested.connect(self.preview_requested)
        builder_layout.addWidget(self.builder)
        self.pages.addWidget(builder_page)

        # Resume files ------------------------------------------------------------
        files_page = QWidget()
        files_layout = QVBoxLayout(files_page)
        files_layout.setContentsMargins(0, 2, 6, 26)
        self.files = ResumeFiles()
        files_layout.addWidget(self.files)
        self.pages.addWidget(_scroll(files_page))

        # Answers -----------------------------------------------------------------
        answers_page = QWidget()
        answers_layout = QVBoxLayout(answers_page)
        answers_layout.setContentsMargins(0, 2, 6, 26)
        answers_layout.setSpacing(22)
        eligibility = FormGroup("Eligibility", "Only answers you set here are used. Leave “Not set” if unsure: "
                                               "the application pauses and asks you.")
        self.yes_no: dict[str, Any] = {}
        for key, title in YES_NO_ANSWERS:
            control = yes_no_control()
            control.setAccessibleName(title)
            self.yes_no[key] = control
            eligibility.add_row(title, control)
        common = FormGroup("Common questions")
        self.answer_fields: dict[str, QComboBox | QLineEdit] = {}
        for key, title, placeholder, suggestions in TEXT_ANSWERS:
            if suggestions:
                field = QComboBox()
                field.setEditable(True)
                field.addItems(["", *suggestions])
                field.lineEdit().setPlaceholderText(placeholder or "Choose or type")
            else:
                field = QLineEdit()
                field.setPlaceholderText(placeholder)
            field.setAccessibleName(title)
            self.answer_fields[key] = field
            common.add_row(title, field)
        bank = FormGroup("Question bank", "Answers to questions employers ask word for word. Luna adds short, "
                                          "general answers you approve; role-specific essays stay with their application.")
        self.question_bank = KeyValueEditor(suggestions=SUGGESTED_QUESTIONS, multiline=True,
                                            key_placeholder="Question", value_placeholder="Your answer",
                                            add_text="Add a question")
        bank.add_widget(self.question_bank)
        for group in (eligibility, common, bank):
            answers_layout.addWidget(group)
        answers_layout.addStretch(1)
        self.pages.addWidget(_scroll(answers_page))

        self.tracker.watch(*self.fields.values(), self.summary, self.custom_facts, self.builder,
                           self.question_bank, *self.yes_no.values())
        for field in self.answer_fields.values():
            if isinstance(field, QComboBox):
                field.currentTextChanged.connect(lambda _=None: self.tracker._mark())
            else:
                self.tracker.watch(field)

    # ------------------------------------------------------------------ state
    @property
    def dirty(self) -> bool:
        return self.tracker.dirty

    @property
    def skills(self):
        return self.builder.skills

    def show_tab(self, key: str) -> None:
        index = self.tabs.findData(key)
        if index >= 0:
            self.tabs.setCurrentIndex(index)

    def _dirty_changed(self, dirty: bool) -> None:
        self.save_button.setEnabled(dirty)
        self.header.set_subtitle("You have unsaved changes." if dirty else SUBTITLE)

    def set_profile(self, profile: Any) -> None:
        data = profile.model_dump() if hasattr(profile, "model_dump") else (profile or {})
        with self.tracker.suspended():
            for key, edit in self.fields.items():
                edit.setText(str(data.get(key, "") or ""))
            self.summary.setPlainText(str(data.get("summary", "") or ""))
            self.custom_facts.set_values(dict(data.get("custom_facts", {}) or {}))
            self.builder.set_profile(data)
        self.tracker.reset()

    def set_settings(self, settings: dict) -> None:
        answers = dict(settings.get("answers") or {})
        self._answers = answers
        with self.tracker.suspended():
            for key, control in self.yes_no.items():
                control.setCurrentIndex(max(0, control.findData(str(answers.get(key) or ""))))
            for key, field in self.answer_fields.items():
                value = str(answers.get(key) or "")
                field.setCurrentText(value) if isinstance(field, QComboBox) else field.setText(value)
            self.question_bank.set_values(dict(answers.get("field_answers") or {}))
        self.files.set_strategy(str(settings.get("resume_strategy", "tailored")))
        self.tracker.reset()

    def answers_patch(self) -> dict:
        answers = {key: str(control.currentData() or "") for key, control in self.yes_no.items()}
        for key, field in self.answer_fields.items():
            answers[key] = (field.currentText() if isinstance(field, QComboBox) else field.text()).strip()
        bank = {}
        for question, value in self.question_bank.values().items():
            lowered = value.strip().casefold()
            bank[question] = lowered == "true" if lowered in {"true", "false"} else value
        answers["field_answers"] = bank
        confirmed = [key for key, value in answers.items() if key != "field_answers" and isinstance(value, str) and value]
        previous = [key for key in self._answers.get("confirmed_fields") or [] if key == "years_experience"]
        answers["confirmed_fields"] = sorted(set(confirmed + previous))
        if self._answers.get("years_experience"):
            answers["years_experience"] = str(self._answers["years_experience"])
        return answers

    def _save(self) -> None:
        profile = {key: edit.text().strip() for key, edit in self.fields.items()}
        profile["summary"] = self.summary.toPlainText().strip()
        profile["custom_facts"] = self.custom_facts.values()
        self.save_requested.emit({"profile": profile, "library": self.builder.library(),
                                  "answers": self.answers_patch()})
