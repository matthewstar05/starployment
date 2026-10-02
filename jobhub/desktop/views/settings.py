"""Settings page: appearance, connections, automation, search, resume, and sources.

Appearance and the resume choice apply instantly. Everything else waits for
Save, tracked by ``DirtyTracker`` so a refresh never overwrites edits.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QLineEdit, QScrollArea, QSpinBox, QVBoxLayout, QWidget

from ...config import ASHBY_COMPANIES, GREENHOUSE_COMPANIES, LEVER_COMPANIES
from ..editors import TagEditor
from ..widgets import DirtyTracker, FormGroup, PageHeader, SegmentedControl, Stepper, ToggleSwitch, button
from .connections import EmailConnection, LunaConnection

DEFAULT_SUBTITLE = "Connections, automation, search preferences, and appearance."
TITLE_SUGGESTIONS = ["Software Engineer", "Software Developer", "Backend Engineer", "Frontend Engineer",
                     "Full Stack Engineer", "Data Scientist", "Data Analyst", "Data Engineer", "Machine Learning Engineer",
                     "Quantitative Developer", "Quantitative Researcher", "Research Engineer", "Product Manager",
                     "New Grad", "Entry Level", "Site Reliability Engineer", "Security Engineer", "Mobile Engineer"]
PLACE_SUGGESTIONS = ["Remote", "San Francisco", "New York", "Seattle", "Los Angeles", "Chicago", "Boston", "Austin",
                     "Denver", "Washington, DC", "Atlanta", "San Diego", "Toronto", "London"]


class SettingsView(QWidget):
    save_requested = Signal(dict)
    scan_requested = Signal()
    appearance_changed = Signal(str)
    resume_strategy_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.tracker = DirtyTracker(self)
        column = QVBoxLayout(self)
        column.setContentsMargins(30, 26, 30, 0)
        column.setSpacing(14)
        self.header = PageHeader("Settings", DEFAULT_SUBTITLE)
        self.save_button = button("Save settings")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save)
        self.header.add_action(self.save_button)
        column.addWidget(self.header)
        self.tracker.changed.connect(self._dirty_changed)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        column.addWidget(scroll, 1)
        self.scroll = scroll
        page = QWidget()
        page.setObjectName("page")
        scroll.setWidget(page)
        groups = QVBoxLayout(page)
        groups.setContentsMargins(0, 2, 6, 26)
        groups.setSpacing(22)

        appearance = FormGroup("Appearance")
        self.appearance = SegmentedControl()
        for text, value in (("Automatic", "System"), ("Light", "Light"), ("Dark", "Dark")):
            self.appearance.addItem(text, value)
        self.appearance.activated.connect(lambda _index: self.appearance_changed.emit(str(self.appearance.currentData())))
        appearance.add_row("Theme", self.appearance, "Automatic follows your system.")
        groups.addWidget(appearance)

        # Connections -----------------------------------------------------------
        self.luna = LunaConnection()
        self.email = EmailConnection()
        self.connections_anchor = self.luna
        groups.addWidget(self.luna)
        groups.addWidget(self.email)

        # Automation -------------------------------------------------------------
        automation = FormGroup("Automation", "Starployment scans while the app is open and the computer is awake.")
        self.auto_apply = ToggleSwitch()
        self.auto_apply.setAccessibleName("Automatic applications")
        self.dry_run = ToggleSwitch()
        self.dry_run.setAccessibleName("Practice mode")
        self.interval = QSpinBox()
        self.interval.setRange(5, 10080)
        self.interval.setSuffix(" min")
        self.max_apply = QSpinBox()
        self.max_apply.setRange(1, 25)
        self.min_score = QSpinBox()
        self.min_score.setRange(0, 100)
        automation.add_row("Practice mode", self.dry_run, "Fill forms but never click Submit, even when you send.")
        automation.add_row("Automatic applications", self.auto_apply,
                           "Also apply to eligible matches after each scan, without review.")
        self.interval.setSingleStep(5)
        automation.add_row("Scan every", Stepper(self.interval, width=104))
        automation.add_row("Applications per run", Stepper(self.max_apply), "At most one per employer each run.")
        automation.add_row("Minimum match score", Stepper(self.min_score), "Automatic runs only apply at or above this.")
        groups.addWidget(automation)

        # Search ---------------------------------------------------------------
        search = FormGroup("What to look for", "Changes re-rank every saved job.")
        self.target_titles = TagEditor("Add a title", TITLE_SUGGESTIONS)
        self.locations = TagEditor("Add a place", PLACE_SUGGESTIONS)
        self.exclude_words = TagEditor("Add a word")
        self.remote_ok = ToggleSwitch()
        self.include_internships = ToggleSwitch()
        self.min_salary = QSpinBox()
        self.min_salary.setRange(0, 10_000_000)
        self.min_salary.setSingleStep(5_000)
        self.min_salary.setPrefix("$ ")
        self.min_salary.setGroupSeparatorShown(True)
        self.years_experience = QSpinBox()
        self.years_experience.setRange(0, 80)
        self.years_experience.setSuffix(" years")
        search.add_row("Job titles", self.target_titles)
        search.add_row("Places", self.locations)
        search.add_row("Leave out titles with", self.exclude_words)
        search.add_row("Include remote roles", self.remote_ok)
        search.add_row("Include internships", self.include_internships)
        search.add_row("Minimum salary", Stepper(self.min_salary, width=132))
        search.add_row("Professional experience", Stepper(self.years_experience, width=104))
        groups.addWidget(search)

        # Resume ---------------------------------------------------------------
        resume = FormGroup("Resume")
        self.strategy = SegmentedControl()
        self.strategy.addItem("Generated", "tailored")
        self.strategy.addItem("Matched file", "library")
        self.strategy.addItem("Main resume", "original")
        self.strategy.activated.connect(lambda _i: self.resume_strategy_changed.emit(str(self.strategy.currentData())))
        self.one_page = ToggleSwitch()
        self.max_skills = QSpinBox()
        self.max_skills.setRange(5, 80)
        resume.add_row("Resume to send", self.strategy,
                       "Generated builds a one-page resume per role from Profile → Resume builder.")
        resume.add_row("Keep generated resumes to one page", self.one_page)
        resume.add_row("Skills listed per resume", Stepper(self.max_skills))
        groups.addWidget(resume)

        # Sources -------------------------------------------------------------
        sources = FormGroup("Company job boards", "Public Greenhouse, Lever, and Ashby boards to scan. Use the name "
                                                  "from the board's web address, e.g. stripe for job-boards.greenhouse.io/stripe.")
        self.greenhouse = TagEditor("Add a Greenhouse board", GREENHOUSE_COMPANIES)
        self.lever = TagEditor("Add a Lever board", LEVER_COMPANIES)
        self.ashby = TagEditor("Add an Ashby board", ASHBY_COMPANIES)
        self.search_terms = TagEditor("Add a phrase")
        sources.add_row("Greenhouse", self.greenhouse)
        sources.add_row("Lever", self.lever)
        sources.add_row("Ashby", self.ashby)
        sources.add_row("Job-site searches", self.search_terms, "Phrases used on Indeed, LinkedIn, and Google Jobs.")
        groups.addWidget(sources)
        groups.addStretch(1)

        self.tracker.watch(
            self.auto_apply, self.dry_run, self.interval, self.max_apply, self.min_score, self.luna, self.email,
            self.target_titles, self.locations, self.exclude_words, self.remote_ok, self.include_internships,
            self.min_salary, self.years_experience, self.one_page, self.max_skills, self.greenhouse, self.lever,
            self.ashby, self.search_terms,
        )

    # Compatibility aliases for the email fields.
    @property
    def verification_enabled(self):
        return self.email.codes

    @property
    def imap_host(self) -> QLineEdit:
        return self.email.host

    @property
    def imap_port(self) -> QSpinBox:
        return self.email.port

    @property
    def imap_username(self) -> QLineEdit:
        return self.email.address

    @property
    def imap_sender(self) -> QLineEdit:
        return self.email.sender

    @property
    def imap_password(self) -> QLineEdit:
        return self.email.password

    @property
    def forget_imap_password(self):
        return self.email.forget

    @property
    def dirty(self) -> bool:
        return self.tracker.dirty

    def _dirty_changed(self, dirty: bool) -> None:
        self.save_button.setEnabled(dirty)
        self.header.set_subtitle("You have unsaved changes." if dirty else DEFAULT_SUBTITLE)

    def show_connections(self) -> None:
        self.scroll.ensureWidgetVisible(self.luna, 0, 0)

    def set_settings(self, s: dict) -> None:
        with self.tracker.suspended():
            appearance = str(s.get("appearance", "System")).title()
            self.appearance.setCurrentIndex(max(0, self.appearance.findData(appearance)))
            self.auto_apply.setChecked(bool(s.get("auto_apply", False)))
            self.dry_run.setChecked(bool(s.get("dry_run", False)))
            self.interval.setValue(int(s.get("scan_interval_min", 60)))
            self.max_apply.setValue(int(s.get("max_apply_per_run", 3)))
            self.min_score.setValue(int(s.get("min_score_to_apply", 60)))
            self.strategy.setCurrentIndex(max(0, self.strategy.findData(str(s.get("resume_strategy", "tailored")))))
            builder = s.get("resume_builder") or {}
            self.one_page.setChecked(bool(builder.get("one_page", True)))
            self.max_skills.setValue(int(builder.get("max_skills", 28)))
            self.luna.set_values(s)
            self.email.set_values(s)
            self.target_titles.set_values(list(s.get("target_titles") or []))
            self.locations.set_values(list(s.get("preferred_locations") or []))
            self.exclude_words.set_values(list(s.get("exclude_title_words") or []))
            self.remote_ok.setChecked(bool(s.get("remote_ok", True)))
            self.include_internships.setChecked(bool(s.get("include_internships", False)))
            self.min_salary.setValue(int(s.get("min_salary", 0) or 0))
            self.years_experience.setValue(int(float(s.get("years_experience", 0) or 0)))
            companies = s.get("companies") or {}
            self.greenhouse.set_values(list(companies.get("greenhouse") or []))
            self.lever.set_values(list(companies.get("lever") or []))
            self.ashby.set_values(list(companies.get("ashby") or []))
            self.search_terms.set_values(list((s.get("jobspy") or {}).get("search_terms") or []))
        self.tracker.reset()

    def _save(self) -> None:
        luna = self.luna.values()
        patch = {
            "appearance": self.appearance.currentData(),
            "auto_apply": self.auto_apply.isChecked(),
            "dry_run": self.dry_run.isChecked(),
            "scan_interval_min": self.interval.value(),
            "max_apply_per_run": self.max_apply.value(),
            "min_score_to_apply": float(self.min_score.value()),
            "resume_strategy": self.strategy.currentData(),
            "resume_builder": {"one_page": self.one_page.isChecked(), "max_skills": self.max_skills.value(),
                               **luna.pop("resume_builder")},
            **luna,
            **self.email.values(),
            "target_titles": self.target_titles.values(),
            "preferred_locations": self.locations.values(),
            "exclude_title_words": self.exclude_words.values(),
            "remote_ok": self.remote_ok.isChecked(),
            "include_internships": self.include_internships.isChecked(),
            "min_salary": self.min_salary.value(),
            "years_experience": float(self.years_experience.value()),
            "companies": {"greenhouse": self.greenhouse.values(), "lever": self.lever.values(),
                          "ashby": self.ashby.values()},
            "jobspy": {"search_terms": self.search_terms.values()[:20]},
        }
        self.save_requested.emit(patch)
        with self.tracker.suspended():
            self.email.clear_secrets()
