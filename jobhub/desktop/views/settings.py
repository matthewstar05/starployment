"""Settings page: appearance, connections, automation, search, resume, and sources.

Appearance and the resume choice apply instantly. Everything else waits for
Save, tracked by ``DirtyTracker`` so a refresh never overwrites edits.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QLineEdit, QScrollArea, QSpinBox, QVBoxLayout, QWidget

from ...config import ASHBY_COMPANIES, GREENHOUSE_COMPANIES, LEVER_COMPANIES, PRIORITY_COMPANIES, WORKDAY_COMPANIES
from .. import fonts
from ..editors import TagEditor
from ..widgets import DirtyTracker, FormGroup, PageHeader, SegmentedControl, Stepper, ToggleSwitch, button
from .connections import EmailConnection, LunaConnection

DEFAULT_SUBTITLE = "Connections, automation, priority companies, search preferences, and appearance."
COMPANY_SUGGESTIONS = sorted(set(PRIORITY_COMPANIES) | {
    "Adobe", "AMD", "Bloomberg", "Coinbase", "DoorDash", "Figma", "Intel", "Lyft", "Notion", "Oracle", "Pinterest",
    "Ramp", "Reddit", "Robinhood", "Roblox", "Salesforce", "Scale AI", "Shopify", "Snap", "Snowflake", "Spotify",
    "TikTok", "ByteDance", "Waymo", "xAI", "Millennium", "Point72", "Akuna Capital", "DRW", "Virtu",
})
TYPEFACE_NOTE = "Claude uses Anthropic Sans and Serif from the Claude app on this computer."
TYPEFACE_MISSING = "Claude's fonts appear when the Claude desktop app is installed; until then the system font is used."
DISCOVER_NOTE = "Also read the public Greenhouse, Lever, and Ashby boards of companies found in other listings."
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
    typeface_changed = Signal(str)
    resume_strategy_changed = Signal(str)
    passwords_requested = Signal()

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
        self.typeface = SegmentedControl()
        for text, value in (("Claude", "Claude"), ("System", "System")):
            self.typeface.addItem(text, value)
        self.typeface.activated.connect(lambda _index: self.typeface_changed.emit(str(self.typeface.currentData())))
        appearance.add_row("Font", self.typeface, TYPEFACE_NOTE if fonts.claude_available()
                           else TYPEFACE_MISSING)
        groups.addWidget(appearance)

        # Connections -----------------------------------------------------------
        self.luna = LunaConnection()
        self.email = EmailConnection()
        self.connections_anchor = self.luna
        groups.addWidget(self.luna)
        groups.addWidget(self.email)

        security = FormGroup("Security")
        self.keychain_protection = ToggleSwitch()
        self.keychain_protection.setAccessibleName("Keychain protection")
        security.add_row("Keychain protection", self.keychain_protection,
                         "Keep the mailbox password and API keys in your system Keychain. Off keeps them in a "
                         "file only your account can read, so updates never stop to ask for Keychain access.")
        groups.addWidget(security)

        workday = FormGroup("Workday", "Banks, insurers, and asset managers apply through Workday, which needs an "
                                       "account at each employer.")
        self.workday_accounts = ToggleSwitch()
        self.workday_accounts.setAccessibleName("Manage Workday sign-ins")
        workday.add_row("Manage Workday sign-ins", self.workday_accounts,
                        "Create each employer's Workday account with your email and a generated password, accept "
                        "its account terms, and sign in with it later. Passwords you type are saved too. They stay "
                        "on this Mac, in Keychain when Keychain protection is on.")
        self.workday_send = ToggleSwitch()
        self.workday_send.setAccessibleName("Send Workday applications")
        workday.add_row("Send Workday applications", self.workday_send,
                        "Approve & send, Send ready, and automatic runs fill each Workday page, save it, and submit. "
                        "A page asking something you have no saved answer for comes back to you.")
        passwords = button("Show saved sign-ins", "secondary")
        passwords.clicked.connect(self.passwords_requested)
        workday.add_row("Saved sign-ins", passwords, "Showing a password asks for Touch ID.")
        groups.addWidget(workday)

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
        self.parallel_sends = QSpinBox()
        self.parallel_sends.setRange(1, 8)
        self.parallel_sends.setSuffix(" at once")
        self.show_browser = ToggleSwitch()
        self.show_browser.setAccessibleName("Show the browser while sending")
        automation.add_row("Practice mode", self.dry_run, "Fill forms but never click Submit, even when you send.")
        automation.add_row("Send applications", Stepper(self.parallel_sends, width=112),
                           "Each runs in its own background browser, one per employer at a time.")
        automation.add_row("Show the browser while sending", self.show_browser,
                           "Off sends quietly in the background. Turn on to watch a send.")
        automation.add_row("Automatic applications", self.auto_apply,
                           "Also apply to eligible matches after each scan, without review.")
        self.interval.setSingleStep(5)
        automation.add_row("Scan every", Stepper(self.interval, width=104))
        automation.add_row("Applications per run", Stepper(self.max_apply), "At most one per employer each run.")
        automation.add_row("Minimum match score", Stepper(self.min_score), "Automatic runs only apply at or above this.")
        groups.addWidget(automation)

        # Priority companies ---------------------------------------------------
        priority = FormGroup("Priority companies",
                             "Applications to these employers are starred and prepared like any other, then wait "
                             "for you: automatic runs and Send ready skip them, so you approve each one yourself.")
        self.priority_companies = TagEditor("Add a company", COMPANY_SUGGESTIONS, lowercase_unique=True)
        priority.add_row("Companies", self.priority_companies)
        restore = button("Restore FAANG+ list", "quiet")
        restore.setToolTip("Replace this list with the default FAANG+ companies")
        restore.clicked.connect(self._restore_priority)
        priority.add_row("", restore)
        groups.addWidget(priority)

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
        self.strategy.addItem("Tailor from profile", "tailored")
        self.strategy.addItem("Resume bank", "library")
        self.strategy.addItem("Main resume", "original")
        self.strategy.activated.connect(lambda _i: self.resume_strategy_changed.emit(str(self.strategy.currentData())))
        self.max_skills = QSpinBox()
        self.max_skills.setRange(5, 80)
        resume.add_row("Resume to send", self.strategy,
                       "Choose between generating a tailored resume per role from your profile, or selecting the most relevant untouched resume from your uploaded resume bank.")
        resume.add_row("Skills listed per resume", Stepper(self.max_skills))
        groups.addWidget(resume)

        # Sources -------------------------------------------------------------
        sources = FormGroup("Company job boards", "Public Greenhouse, Lever, Ashby, and Workday boards to scan. Use the "
                                                  "name from the board's web address, e.g. stripe for "
                                                  "job-boards.greenhouse.io/stripe.")
        self.greenhouse = TagEditor("Add a Greenhouse board", GREENHOUSE_COMPANIES)
        self.lever = TagEditor("Add a Lever board", LEVER_COMPANIES)
        self.ashby = TagEditor("Add an Ashby board", ASHBY_COMPANIES)
        self.workday = TagEditor("Add a Workday board", WORKDAY_COMPANIES)
        self.search_terms = TagEditor("Add a phrase")
        sources.add_row("Greenhouse", self.greenhouse)
        sources.add_row("Lever", self.lever)
        sources.add_row("Ashby", self.ashby)
        sources.add_row("Workday", self.workday, "Written company.wdN/site, from the careers address: citi.wd5/2 for "
                                                 "citi.wd5.myworkdayjobs.com/2. See Workday above for "
                                                 "how these applications are sent.")
        sources.add_row("Job-site searches", self.search_terms, "Phrases used on Indeed, LinkedIn, and Google Jobs.")
        self.discover_boards = ToggleSwitch()
        self.discover_boards.setAccessibleName("Discover more company boards")
        self._discover_row = sources.add_row("Discover more boards", self.discover_boards, DISCOVER_NOTE)
        groups.addWidget(sources)
        groups.addStretch(1)

        self.tracker.watch(
            self.auto_apply, self.dry_run, self.interval, self.max_apply, self.min_score, self.luna, self.email,
            self.parallel_sends, self.show_browser, self.keychain_protection, self.workday_accounts, self.workday_send,
            self.target_titles, self.locations, self.exclude_words, self.remote_ok, self.include_internships,
            self.min_salary, self.years_experience, self.max_skills, self.greenhouse, self.lever,
            self.ashby, self.workday, self.search_terms, self.priority_companies, self.discover_boards,
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

    def _restore_priority(self) -> None:
        self.priority_companies.set_values(list(PRIORITY_COMPANIES))
        self.priority_companies.changed.emit()

    def set_board_count(self, count: int) -> None:
        """Show how many discovered boards scans are reading."""
        note = self._discover_row.findChild(QLabel, "rowNote")
        if note is not None:
            note.setText(DISCOVER_NOTE + (f" {count:,} found so far." if count else ""))

    def set_settings(self, s: dict) -> None:
        with self.tracker.suspended():
            appearance = str(s.get("appearance", "System")).title()
            self.appearance.setCurrentIndex(max(0, self.appearance.findData(appearance)))
            typeface = str(s.get("font", "Claude")).title()
            self.typeface.setCurrentIndex(max(0, self.typeface.findData(typeface)))
            self.auto_apply.setChecked(bool(s.get("auto_apply", False)))
            self.dry_run.setChecked(bool(s.get("dry_run", False)))
            self.interval.setValue(int(s.get("scan_interval_min", 60)))
            self.max_apply.setValue(int(s.get("max_apply_per_run", 3)))
            self.min_score.setValue(int(s.get("min_score_to_apply", 60)))
            self.parallel_sends.setValue(int(s.get("parallel_sends", 4)))
            self.show_browser.setChecked(bool(s.get("show_browser", False)))
            self.keychain_protection.setChecked(bool(s.get("keychain_protection", False)))
            workday = s.get("workday") or {}
            self.workday_accounts.setChecked(bool(workday.get("create_accounts", False)))
            self.workday_send.setChecked(bool(workday.get("send", False)))
            strat = str(s.get("resume_strategy", "tailored"))
            if strat == "bank":
                strat = "library"
            self.strategy.setCurrentIndex(max(0, self.strategy.findData(strat)))
            builder = s.get("resume_builder") or {}
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
            self.workday.set_values(list(companies.get("workday") or []))
            self.search_terms.set_values(list((s.get("jobspy") or {}).get("search_terms") or []))
            self.priority_companies.set_values(list(s.get("priority_companies") or []))
            self.discover_boards.setChecked(bool(s.get("discover_boards", True)))
        self.tracker.reset()

    def _save(self) -> None:
        luna = self.luna.values()
        patch = {
            "appearance": self.appearance.currentData(),
            "font": self.typeface.currentData(),
            "auto_apply": self.auto_apply.isChecked(),
            "dry_run": self.dry_run.isChecked(),
            "scan_interval_min": self.interval.value(),
            "max_apply_per_run": self.max_apply.value(),
            "min_score_to_apply": float(self.min_score.value()),
            "parallel_sends": self.parallel_sends.value(),
            "show_browser": self.show_browser.isChecked(),
            "keychain_protection": self.keychain_protection.isChecked(),
            "workday": {"create_accounts": self.workday_accounts.isChecked(), "send": self.workday_send.isChecked()},
            "resume_strategy": self.strategy.currentData(),
            "resume_builder": {"one_page": True, "max_skills": self.max_skills.value(),
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
                          "ashby": self.ashby.values(), "workday": self.workday.values()},
            "jobspy": {"search_terms": self.search_terms.values()[:20]},
            "priority_companies": self.priority_companies.values(),
            "discover_boards": self.discover_boards.isChecked(),
        }
        self.save_requested.emit(patch)
        with self.tracker.suspended():
            self.email.clear_secrets()
