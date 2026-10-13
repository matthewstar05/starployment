"""Jobs page: searchable, filterable, sortable, paginated table of ranked postings.

Sorting and the posting-date window run in SQL (``db.list_jobs``) so they
apply across every page, not only the rows on screen. Click a column header
or use the Sort menu; the Posted menu keeps roles published in a window such
as the last 24 hours.
"""
from __future__ import annotations

import time
from typing import Any

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence, QPainter
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QStackedWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...fortune import fortune_rank
from ...models import posted_epoch
from ...priority import held_from_automation, is_starred
from ..delegates import (
    ACTION_ROLE,
    POSTED_AT_ROLE,
    SCORE_ROLE,
    SEARCH_ROLE,
    STAR_ROLE,
    STATUS_ROLE,
    SUBTITLE_ROLE,
    ActionDelegate,
    GlassTable,
    PostedDelegate,
    RoleDelegate,
    ScoreDelegate,
    StatusDelegate,
    TextDelegate,
    configure_columns,
)
from ..format import local_time, posted_label, salary, status_label
from ..native_glass import prepare_window
from ..widgets import (
    ChoiceButton,
    EmptyState,
    GlassPanel,
    PageHeader,
    SearchField,
    SegmentedControl,
    ToggleSwitch,
    button,
    label,
    paint_backdrop,
)

# (menu text, value): a posting window in hours; 0 is any time.
POSTED_WINDOWS = (("Any time", 0), ("Last 24 hours", 24), ("Last 3 days", 72), ("Last week", 168),
                  ("Last 30 days", 720))
POSTED_PHRASES = {24: "in the last 24 hours", 72: "in the last 3 days", 168: "in the last week",
                  720: "in the last 30 days"}
# (menu text, sort key, descending)
JOB_SORTS = (("Best match", "match", True), ("Newest posted", "posted", True), ("Recently found", "found", True),
             ("Highest salary", "salary", True), ("Company A–Z", "company", False), ("Title A–Z", "title", False),
             ("Status", "status", False))


class AddPostingSheet(QDialog):
    """A small glass sheet for importing one Greenhouse, Lever, or Ashby URL."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Add a posting")
        prepare_window(self)
        self.setModal(True)
        self.setMinimumWidth(480)
        column = QVBoxLayout(self)
        column.setContentsMargins(24, 22, 24, 20)
        column.setSpacing(10)
        column.addWidget(label("Add a posting", "sheetTitle"))
        column.addWidget(label("Paste a public Greenhouse, Lever, or Ashby job link. "
                               "Starployment imports it, scores it, and adds it to Jobs.", "subtitle"))
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://job-boards.greenhouse.io/company/jobs/123")
        self.url.textChanged.connect(self._validate)
        column.addSpacing(4)
        column.addWidget(self.url)
        self.hint = label("", "caption")
        column.addWidget(self.hint)
        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = button("Cancel", "secondary")
        cancel.clicked.connect(self.reject)
        self.add = button("Add posting")
        self.add.setDefault(True)
        self.add.clicked.connect(self.accept)
        actions.addWidget(cancel)
        actions.addWidget(self.add)
        column.addSpacing(6)
        column.addLayout(actions)
        self._validate("")

    def _validate(self, text: str) -> None:
        from ...sources.import_job import JobImportError, parse_job_url

        text = text.strip()
        if not text:
            self.hint.setText("Supported: Greenhouse, Lever, Ashby, Workable, and Rippling job links")
            self.add.setEnabled(False)
            return
        try:
            parsed = parse_job_url(text)
        except JobImportError as error:
            self.hint.setText(str(error))
            self.add.setEnabled(False)
            return
        self.hint.setText(f"{parsed.ats.title()} posting · {parsed.slug}")
        self.add.setEnabled(True)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        paint_backdrop(painter, self)
        painter.end()

    def value(self) -> str:
        return self.url.text().strip()


ATS_NAMES = {"workday": "Workday", "linkedin": "LinkedIn", "indeed": "Indeed", "smartrecruiters": "SmartRecruiters",
             "icims": "iCIMS", "workable": "Workable"}


class JobsView(QWidget):
    apply_requested = Signal(str)
    prepare_requested = Signal(str)
    bulk_prepare_requested = Signal()
    review_requested = Signal(str)
    status_requested = Signal(str, str)
    open_requested = Signal(str)
    import_requested = Signal(str)
    star_requested = Signal(str, bool)
    mark_sent_requested = Signal(str)
    query_changed = Signal()
    page_size = 100

    ROLE, POSTED, SALARY, MATCH, STATUS, ACTION = range(6)
    # Header click -> sort key; the second click on the same column reverses it.
    COLUMN_SORTS = {0: "title", 1: "posted", 2: "salary", 3: "match", 4: "status"}
    FILTERS = (("All", "all"), ("New", "new"), ("Prepared", "prepared"), ("Applied", "applied"),
               ("Review", "needs_human"), ("Skipped", "skipped"))

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.page = 0
        self.total_jobs = 0
        self.jobs: list[Any] = []
        self.priority: list[str] = []
        self.sort_key = "match"
        self.sort_descending = True
        self._signature: object = None
        self._query_timer = QTimer(self)
        self._query_timer.setSingleShot(True)
        self._query_timer.setInterval(220)
        self._query_timer.timeout.connect(self.query_changed)

        column = QVBoxLayout(self)
        column.setContentsMargins(30, 26, 30, 22)
        column.setSpacing(14)
        self.header = PageHeader("Jobs", "Roles ranked against your profile.")
        self.posted = ChoiceButton("clock", "Posted")
        for text, hours in POSTED_WINDOWS:
            self.posted.add_option(text, hours)
        self.posted.changed.connect(self._reset_query)
        self.sort = ChoiceButton("sort", "Sort")
        for text, key, _descending in JOB_SORTS:
            self.sort.add_option(text, key)
        self.sort.changed.connect(self._sort_chosen)
        add = button("Add posting", "secondary", icon="plus")
        add.setToolTip("Import a Greenhouse, Lever, Ashby, Workable, or Rippling link (⌘N)")
        add.clicked.connect(self._import_dialog)
        self.bulk_prepare = button("Prepare bulk", icon="layers")
        self.bulk_prepare.setToolTip("Choose how many new applications to prepare for review")
        self.bulk_prepare.clicked.connect(self.bulk_prepare_requested)
        self.header.add_action(self.posted)
        self.header.add_action(self.sort)
        self.header.add_action(self.bulk_prepare)
        self.header.add_action(add)
        column.addWidget(self.header)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.search = SearchField("Search title, company, or location")
        self.search.setMinimumWidth(260)
        self.search.setMaximumWidth(380)
        toolbar.addWidget(self.search, 1)
        toolbar.addStretch(0)
        self.filter = SegmentedControl(compact=True)
        for text, value in self.FILTERS:
            self.filter.addItem(text, value)
        toolbar.addWidget(self.filter)
        column.addLayout(toolbar)

        options = QHBoxLayout()
        options.setSpacing(12)

        self.unpreparable_toggle = ToggleSwitch()
        self.unpreparable_toggle.setChecked(True)
        self.unpreparable_toggle.toggled.connect(self._filter_rows)
        self.unpreparable_toggle.setToolTip("Show applications that cannot be immediately prepared with Starployment (external 'View' links). Turn off to hide them.")

        toggle_box = QHBoxLayout()
        toggle_box.setSpacing(6)
        self.toggle_label = label("Can't prepare", "caption", wrap=False)
        self.toggle_label.setToolTip(self.unpreparable_toggle.toolTip())
        toggle_box.addWidget(self.toggle_label)
        toggle_box.addWidget(self.unpreparable_toggle)
        options.addLayout(toggle_box)
        options.addStretch(1)
        self.exclude_starred_toggle = ToggleSwitch()
        self.exclude_starred_toggle.setAccessibleName("Exclude starred jobs")
        self.exclude_starred_toggle.setToolTip("Hide every starred role, including automatic company stars.")
        self.exclude_starred_toggle.toggled.connect(self._filter_rows)
        self.exclude_starred_toggle.toggled.connect(self._reset_query)
        star_box = QHBoxLayout()
        star_box.setSpacing(6)
        star_box.addWidget(label("Exclude starred", "caption", wrap=False))
        star_box.addWidget(self.exclude_starred_toggle)
        options.addLayout(star_box)
        column.addLayout(options)

        card = GlassPanel(radius=18)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(8, 6, 8, 8)
        card_layout.setSpacing(4)
        self.table = GlassTable(0, 6)
        self.table.setHorizontalHeaderLabels(["ROLE", "POSTED", "SALARY", "MATCH", "STATUS", ""])
        self.table.horizontalHeaderItem(self.ACTION).setToolTip("Toggle on/off applications that cannot be immediately prepared with Starployment")
        self.table.setAccessibleName("Jobs")
        self.table.setItemDelegateForColumn(self.ROLE, RoleDelegate(self.table))
        self.table.setItemDelegateForColumn(self.POSTED, PostedDelegate(self.table))
        self.table.setItemDelegateForColumn(self.SALARY, TextDelegate(self.table))
        self.table.setItemDelegateForColumn(self.MATCH, ScoreDelegate(self.table))
        self.table.setItemDelegateForColumn(self.STATUS, StatusDelegate(self.table))
        self.table.setItemDelegateForColumn(self.ACTION, ActionDelegate(self.table))
        configure_columns(self.table, self.ROLE, {self.POSTED: 136, self.SALARY: 110, self.MATCH: 68,
                                                   self.STATUS: 118, self.ACTION: 96})
        header = self.table.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.setSortIndicator(self.MATCH, Qt.SortOrder.DescendingOrder)
        header.sectionClicked.connect(self._header_clicked)
        self.table.installEventFilter(self)
        self.table.verticalHeader().setDefaultSectionSize(56)
        self.table.cellClicked.connect(self._cell_clicked)
        self.table.cellDoubleClicked.connect(lambda row, _column: self._open_row(row))
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.setToolTip("")
        open_action = QAction("Open posting", self.table)
        open_action.setShortcuts([QKeySequence(Qt.Key.Key_Return), QKeySequence(Qt.Key.Key_Enter)])
        open_action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        open_action.triggered.connect(lambda: self._open_row(self.table.currentRow()))
        self.table.addAction(open_action)

        self.body = QStackedWidget()
        self.body.addWidget(self.table)
        self.empty = EmptyState("jobs", "No jobs yet", "Run a scan from the sidebar or add a posting link.")
        self.body.addWidget(self.empty)
        card_layout.addWidget(self.body, 1)

        pagination = QHBoxLayout()
        pagination.setContentsMargins(10, 2, 4, 0)
        self.page_label = label("", "caption", wrap=False)
        pagination.addWidget(self.page_label)
        pagination.addStretch(1)
        self.previous_page = button("", "secondary", icon="chevron-left")
        self.next_page = button("", "secondary", icon="chevron-right")
        self.previous_page.setToolTip("Previous page")
        self.next_page.setToolTip("Next page")
        self.previous_page.setAccessibleName("Previous page")
        self.next_page.setAccessibleName("Next page")
        for control in (self.previous_page, self.next_page):
            control.setFixedWidth(40)
            pagination.addWidget(control)
        self.previous_page.clicked.connect(lambda: self._change_page(-1))
        self.next_page.clicked.connect(lambda: self._change_page(1))
        card_layout.addLayout(pagination)
        column.addWidget(card, 1)

        self.search.textChanged.connect(self._filter_rows)
        self.search.textChanged.connect(self._reset_query)
        self.filter.currentIndexChanged.connect(self._filter_rows)
        self.filter.currentIndexChanged.connect(self._reset_query)
        self.posted.changed.connect(self._filter_rows)
        self._retryable: set[str] = set()
        self._preparing_ids: set[str] = set()

    def set_preparing(self, job_ids: set[str]) -> None:
        self._preparing_ids = set(job_ids)
        self._filter_rows()

    # ----------------------------------------------------------------- query
    @property
    def query(self) -> str:
        return self.search.text().strip()

    @property
    def selected_status(self) -> str:
        return str(self.filter.currentData() or "all")

    @property
    def posted_hours(self) -> int:
        return int(self.posted.value() or 0)

    @property
    def exclude_starred(self) -> bool:
        return self.exclude_starred_toggle.isChecked()

    def set_filter(self, status: str) -> None:
        index = self.filter.findData(status)
        if index >= 0:
            self.filter.setCurrentIndex(index)

    def set_posted_window(self, hours: int) -> None:
        if hours != self.posted_hours:
            self.posted.set_value(hours)
            self._filter_rows()
            self._reset_query()

    def set_priority(self, companies: list[str]) -> None:
        """The priority-company list decides which roles are starred automatically."""
        companies = list(companies or [])
        if companies != self.priority:
            self.priority = companies
            self._signature = None
            if self.jobs:
                self.set_jobs(self.jobs, self._retryable, self.total_jobs)

    def set_sort(self, key: str, descending: bool | None = None) -> None:
        defaults = {k: d for _text, k, d in JOB_SORTS}
        self.sort_key = key
        self.sort_descending = defaults.get(key, True) if descending is None else descending
        self.sort.set_value(key)
        column = next((c for c, k in self.COLUMN_SORTS.items() if k == key), -1)
        header = self.table.horizontalHeader()
        if column >= 0:
            header.setSortIndicator(column, Qt.SortOrder.DescendingOrder if self.sort_descending
                                    else Qt.SortOrder.AscendingOrder)
        else:
            header.setSortIndicator(-1, Qt.SortOrder.DescendingOrder)
        self._reset_query()

    def _sort_chosen(self, key: object) -> None:
        self.set_sort(str(key))

    def _header_clicked(self, column: int) -> None:
        if column == self.ACTION:
            self.unpreparable_toggle.setChecked(not self.unpreparable_toggle.isChecked())
            return
        key = self.COLUMN_SORTS.get(column)
        if key is None:
            # Keep the indicator on the active column when an unsortable header is clicked.
            self.set_sort(self.sort_key, self.sort_descending)
            return
        self.set_sort(key, not self.sort_descending if key == self.sort_key else None)

    def _reset_query(self, *_args) -> None:
        self.page = 0
        self._query_timer.start()

    def _change_page(self, delta: int) -> None:
        target = self.page + delta
        if 0 <= target < max(1, (self.total_jobs + self.page_size - 1) // self.page_size):
            self.page = target
            self._query_timer.stop()
            self.query_changed.emit()
            self.table.scrollToTop()

    # ------------------------------------------------------------------ data
    def set_jobs(self, jobs: list[Any], retryable_ids: set[str] | None = None,
                 total_count: int | None = None) -> None:
        retryable_ids = retryable_ids or set()
        self._retryable = retryable_ids
        self.jobs = list(jobs)
        self.total_jobs = len(jobs) if total_count is None else total_count
        first = self.page * self.page_size + 1 if self.total_jobs else 0
        last = min((self.page + 1) * self.page_size, self.total_jobs)
        self.page_label.setText(f"{first}–{last} of {self.total_jobs:,} jobs")
        self.previous_page.setEnabled(self.page > 0)
        self.next_page.setEnabled(last < self.total_jobs)
        phrase = POSTED_PHRASES.get(self.posted_hours)
        if phrase:
            self.header.set_subtitle(f"{self.total_jobs:,} role{'s' if self.total_jobs != 1 else ''} posted {phrase}.")
        else:
            self.header.set_subtitle(
                f"{self.total_jobs:,} roles ranked against your profile." if self.total_jobs
                else "Roles ranked against your profile."
            )
        signature = (self.page, tuple(
            (j.id, j.status, round(j.score, 1), j.title, j.company, j.location, j.posted_at,
             j.salary_min, j.salary_max, j.id in retryable_ids, is_starred(j, self.priority)) for j in jobs
        ))
        if signature != self._signature:
            self._signature = signature
            self._rebuild(jobs, retryable_ids)
        self._update_empty_state()

    def _rebuild(self, jobs: list[Any], retryable_ids: set[str]) -> None:
        selected = self._selected_job_id()
        scroll = self.table.verticalScrollBar().value()
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(0)
        self.table.setRowCount(len(jobs))
        restore_row = -1
        for row, job in enumerate(jobs):
            location = job.location or ("Remote" if job.remote else "Location not listed")
            starred = is_starred(job, self.priority)
            role = QTableWidgetItem(job.title)
            role.setData(SUBTITLE_ROLE, f"{job.company} · {location}")
            role.setData(Qt.ItemDataRole.UserRole, job.id)
            role.setData(SEARCH_ROLE, f"{job.title} {job.company} {location}".casefold())
            role.setData(STAR_ROLE, starred)
            rank = fortune_rank(job.company)
            if starred == 'red':
                star_tip = '\nFortune 500' + (f' (#{rank})' if rank else '') + ' · Starred (held for review)'
            elif starred:
                star_tip = (f'\nFortune 1000 (#{rank}) · Starred label' if rank else '\nStarred')
            else:
                star_tip = ''
            role.setToolTip(f'{job.title}\n{job.company} · {location}' + star_tip)
            posted = QTableWidgetItem(" · ".join(x for x in posted_label(job.posted_at) if x))
            posted.setData(POSTED_AT_ROLE, job.posted_at)
            posted.setToolTip(("Posted " + local_time(job.posted_at) if job.posted_at else "Posting date not published")
                              + (f"\nFound by Starployment {local_time(job.discovered_at)}" if job.discovered_at else ""))
            pay = QTableWidgetItem(salary(job.salary_min, job.salary_max))
            match = QTableWidgetItem(f"{job.score:.0f}")
            match.setData(SCORE_ROLE, float(job.score))
            match.setToolTip(self._score_tooltip(job))
            status = QTableWidgetItem(status_label(job.status))
            status.setData(STATUS_ROLE, job.status)
            status.setData(Qt.ItemDataRole.UserRole, job.status)
            can_apply = job.status == "new" or job.id in retryable_ids
            if job.id in self._preparing_ids or job.status == "preparing":
                kind = "preparing"
                action = QTableWidgetItem("Preparing…")
                action.setData(ACTION_ROLE, "preparing")
                action.setData(Qt.ItemDataRole.UserRole, "preparing")
                action.setToolTip("Starployment is reading the employer's form and building your resume…")
            elif job.status == "prepared":
                kind = "review"
                action = QTableWidgetItem("Review")
                action.setData(ACTION_ROLE, "apply")
                action.setData(Qt.ItemDataRole.UserRole, kind)
                action.setToolTip("Review the prepared application")
            elif can_apply and job.status in {"new", "dry_run"}:
                kind = "apply" if job.ats in ("greenhouse", "lever", "ashby", "workable", "rippling",
                                              "workday") else "view"
                action = QTableWidgetItem("Prepare" if kind == "apply" else "View")
                action.setData(ACTION_ROLE, kind)
                action.setData(Qt.ItemDataRole.UserRole, kind)
                if kind == "view":
                    site = ATS_NAMES.get(job.ats, "This employer's")
                    action.setToolTip(f"{site} applications are filled on the employer's site. Open the posting to apply there.")
                elif job.ats == "workday":
                    action.setToolTip("Prepare this Workday application. You finish it in a browser window after "
                                      "signing in; Starployment fills each page.")
                else:
                    action.setToolTip("Prepare this application for your review")
            else:
                kind = "retry" if can_apply else "view"
                action = QTableWidgetItem("Retry" if kind == "retry" else "View")
                action.setData(ACTION_ROLE, kind)
                action.setData(Qt.ItemDataRole.UserRole, kind)
                action.setToolTip("Try this application again" if kind == "retry" else "Open the posting")
            for column, item in enumerate((role, posted, pay, match, status, action)):
                self.table.setItem(row, column, item)
            if job.id == selected:
                restore_row = row
        self._filter_rows()
        if restore_row >= 0:
            self.table.selectRow(restore_row)
        self.table.verticalScrollBar().setValue(scroll)
        self.table.setUpdatesEnabled(True)

    def _update_empty_state(self) -> None:
        visible = any(not self.table.isRowHidden(r) for r in range(self.table.rowCount()))
        if not visible:
            if self.query:
                self.empty.set_text("No results", f"Nothing matches “{self.query}”. Try a shorter search.")
            elif self.exclude_starred:
                self.empty.set_text("No unstarred jobs", "No unstarred roles match these filters. Turn off Exclude starred to see starred roles.")
            elif self.posted_hours:
                self.empty.set_text("Nothing posted yet", f"No roles here were posted {POSTED_PHRASES.get(self.posted_hours, 'recently')}. "
                                    "Refresh jobs, or widen the Posted range.")
            elif self.selected_status != "all":
                self.empty.set_text("Nothing here yet", f"No jobs are marked {self.filter.currentText().lower()}.")
            else:
                self.empty.set_text("No jobs yet", "Run a scan from the sidebar or add a posting link.")
        self.body.setCurrentWidget(self.table if visible else self.empty)

    def _selected_job_id(self) -> str | None:
        row = self.table.currentRow()
        item = self.table.item(row, self.ROLE) if row >= 0 else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def job_at(self, row: int) -> Any | None:
        item = self.table.item(row, self.ROLE) if row >= 0 else None
        job_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        return next((job for job in self.jobs if job.id == job_id), None)

    @staticmethod
    def _salary(minimum: int | None, maximum: int | None) -> str:
        return salary(minimum, maximum)

    @staticmethod
    def _score_tooltip(job: Any) -> str:
        detail = job.score_json or {}
        names = (
            ("title_fit", "Title fit"), ("skills_fit", "Skills"),
            ("seniority", "Experience"), ("location", "Location"),
            ("salary", "Salary"), ("freshness", "Freshness"),
            ("ease", "Application support"),
        )
        lines = [f"Match score: {job.score:.0f} / 100"]
        lines.extend(f"{label_}: {detail[key]:g}" for key, label_ in names
                     if isinstance(detail.get(key), (int, float)))
        if detail.get("review_reasons"):
            lines.append("Review: " + "; ".join(detail["review_reasons"]))
        return "\n".join(lines)

    # --------------------------------------------------------------- actions
    def _cell_clicked(self, row: int, column: int) -> None:
        if column != self.ACTION:
            return
        item = self.table.item(row, self.ACTION)
        job = self.job_at(row)
        if item is None or job is None:
            return
        kind = item.data(Qt.ItemDataRole.UserRole) or item.data(ACTION_ROLE)
        if kind == "preparing":
            return
        if kind == "apply":
            self._preparing_ids.add(job.id)
            item.setText("Preparing…")
            item.setData(ACTION_ROLE, "preparing")
            item.setData(Qt.ItemDataRole.UserRole, "preparing")
            self.table.viewport().update()
            self.prepare_requested.emit(job.id)
        elif kind == "review":
            self.review_requested.emit(job.id)
        elif kind == "retry":
            self.apply_requested.emit(job.id)
        else:
            self.open_requested.emit(job.url)

    def _action(self, jid: str, url: str, allowed: bool) -> None:
        self.apply_requested.emit(jid) if allowed else self.open_requested.emit(url)

    def _open_row(self, row: int) -> None:
        job = self.job_at(row)
        if job is not None:
            self.open_requested.emit(job.url)

    def _context_menu(self, position) -> None:
        row = self.table.rowAt(position.y())
        job = self.job_at(row)
        if job is None:
            return
        self.table.selectRow(row)
        menu = QMenu(self)
        menu.addAction("Open posting", lambda: self.open_requested.emit(job.url))
        action_item = self.table.item(row, self.ACTION)
        kind = action_item.data(Qt.ItemDataRole.UserRole) if action_item is not None else None
        starred = is_starred(job, self.priority)
        if kind == "apply":
            menu.addAction("Prepare application", lambda: self.prepare_requested.emit(job.id))
            if not held_from_automation(job, self.priority):   # starred roles always get a prepared, reviewed application
                menu.addAction("Apply now, without review", lambda: self.apply_requested.emit(job.id))
        elif kind == "review":
            menu.addAction("Review prepared application", lambda: self.review_requested.emit(job.id))
        elif kind == "retry":
            menu.addAction("Retry", lambda: self.apply_requested.emit(job.id))
        menu.addAction("Copy link", lambda: QGuiApplication.clipboard().setText(job.url))
        menu.addSeparator()
        menu.addAction("Remove star" if starred else "Star", lambda: self.star_requested.emit(job.id, not starred))
        if job.status not in ("applied", "queued", "applying"):
            menu.addAction("Mark as sent…", lambda: self.mark_sent_requested.emit(job.id))
        if job.status == "skipped":
            menu.addAction("Restore to New", lambda: self.status_requested.emit(job.id, "new"))
        elif job.status in ("new", "failed"):
            menu.addAction("Skip", lambda: self.status_requested.emit(job.id, "skipped"))
        menu.exec(self.table.viewport().mapToGlobal(position))

    def _import_dialog(self) -> None:
        sheet = AddPostingSheet(self.window())
        if sheet.exec() == QDialog.DialogCode.Accepted and sheet.value():
            self.import_requested.emit(sheet.value())

    def focus_search(self) -> None:
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    def _filter_rows(self, *_args) -> None:
        query = self.search.text().casefold().strip()
        selected = self.filter.currentData()
        cutoff = time.time() - self.posted_hours * 3600 if self.posted_hours else None
        hide_unpreparable = not self.unpreparable_toggle.isChecked()
        for row in range(self.table.rowCount()):
            role = self.table.item(row, self.ROLE)
            status = self.table.item(row, self.STATUS)
            posted = self.table.item(row, self.POSTED)
            action = self.table.item(row, self.ACTION)
            text = str(role.data(SEARCH_ROLE) or "") if role else ""
            value = status.data(STATUS_ROLE) if status else None
            stamp = posted_epoch(posted.data(POSTED_AT_ROLE)) if posted else None
            outside = cutoff is not None and (stamp is None or stamp < cutoff)
            kind = action.data(Qt.ItemDataRole.UserRole) if action else None
            is_unpreparable = (kind == "view")
            self.table.setRowHidden(
                row, bool(query and query not in text)
                or (selected != "all" and value != selected)
                or outside
                or (hide_unpreparable and is_unpreparable)
                or (self.exclude_starred and bool(role.data(STAR_ROLE) if role else False))
            )
        self._update_empty_state()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.table and event.type() == QEvent.Type.Resize:
            # Salary yields its column to role titles in a narrow window.
            self.table.setColumnHidden(self.SALARY, event.size().width() < 760)
        return super().eventFilter(watched, event)
