"""Jobs page: searchable, filterable, paginated table of ranked postings."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QTimer, Qt, Signal
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

from ..delegates import (
    ACTION_ROLE,
    SCORE_ROLE,
    SEARCH_ROLE,
    STATUS_ROLE,
    SUBTITLE_ROLE,
    ActionDelegate,
    GlassTable,
    RoleDelegate,
    ScoreDelegate,
    StatusDelegate,
    TextDelegate,
    configure_columns,
)
from ..format import salary, status_label
from ..widgets import (
    EmptyState,
    GlassPanel,
    PageHeader,
    SearchField,
    SegmentedControl,
    button,
    label,
    paint_backdrop,
)


class AddPostingSheet(QDialog):
    """A small glass sheet for importing one Greenhouse, Lever, or Ashby URL."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Add a posting")
        self.setModal(True)
        self.setMinimumWidth(480)
        column = QVBoxLayout(self)
        column.setContentsMargins(24, 22, 24, 20)
        column.setSpacing(10)
        column.addWidget(label("Add a posting", "sheetTitle"))
        column.addWidget(label("Paste a public Greenhouse, Lever, or Ashby job link. "
                               "JobHub imports it, scores it, and adds it to Jobs.", "subtitle"))
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
            self.hint.setText("Supported: job-boards.greenhouse.io, jobs.lever.co, jobs.ashbyhq.com")
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


class JobsView(QWidget):
    apply_requested = Signal(str)
    status_requested = Signal(str, str)
    open_requested = Signal(str)
    import_requested = Signal(str)
    query_changed = Signal()
    page_size = 100

    ROLE, SALARY, MATCH, STATUS, ACTION = range(5)
    FILTERS = (("All", "all"), ("New", "new"), ("Applied", "applied"),
               ("Review", "needs_human"), ("Skipped", "skipped"))

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.page = 0
        self.total_jobs = 0
        self.jobs: list[Any] = []
        self._signature: object = None
        self._query_timer = QTimer(self)
        self._query_timer.setSingleShot(True)
        self._query_timer.setInterval(220)
        self._query_timer.timeout.connect(self.query_changed)

        column = QVBoxLayout(self)
        column.setContentsMargins(30, 26, 30, 22)
        column.setSpacing(14)
        self.header = PageHeader("Jobs", "Roles ranked against your profile.")
        add = button("Add posting", "secondary", icon="plus")
        add.setToolTip("Import a Greenhouse, Lever, or Ashby link (⌘N)")
        add.clicked.connect(self._import_dialog)
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

        card = GlassPanel(radius=18)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(8, 6, 8, 8)
        card_layout.setSpacing(4)
        self.table = GlassTable(0, 5)
        self.table.setHorizontalHeaderLabels(["ROLE", "SALARY", "MATCH", "STATUS", ""])
        self.table.setAccessibleName("Jobs")
        self.table.setItemDelegateForColumn(self.ROLE, RoleDelegate(self.table))
        self.table.setItemDelegateForColumn(self.SALARY, TextDelegate(self.table))
        self.table.setItemDelegateForColumn(self.MATCH, ScoreDelegate(self.table))
        self.table.setItemDelegateForColumn(self.STATUS, StatusDelegate(self.table))
        self.table.setItemDelegateForColumn(self.ACTION, ActionDelegate(self.table))
        configure_columns(self.table, self.ROLE, {self.SALARY: 116, self.MATCH: 70, self.STATUS: 122, self.ACTION: 100})
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

    # ----------------------------------------------------------------- query
    @property
    def query(self) -> str:
        return self.search.text().strip()

    @property
    def selected_status(self) -> str:
        return str(self.filter.currentData() or "all")

    def set_filter(self, status: str) -> None:
        index = self.filter.findData(status)
        if index >= 0:
            self.filter.setCurrentIndex(index)

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
        self.jobs = list(jobs)
        self.total_jobs = len(jobs) if total_count is None else total_count
        first = self.page * self.page_size + 1 if self.total_jobs else 0
        last = min((self.page + 1) * self.page_size, self.total_jobs)
        self.page_label.setText(f"{first}–{last} of {self.total_jobs:,} jobs")
        self.previous_page.setEnabled(self.page > 0)
        self.next_page.setEnabled(last < self.total_jobs)
        self.header.set_subtitle(
            f"{self.total_jobs:,} roles ranked against your profile." if self.total_jobs
            else "Roles ranked against your profile."
        )
        signature = (self.page, tuple(
            (j.id, j.status, round(j.score, 1), j.title, j.company, j.location,
             j.salary_min, j.salary_max, j.id in retryable_ids) for j in jobs
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
            role = QTableWidgetItem(job.title)
            role.setData(SUBTITLE_ROLE, f"{job.company} · {location}")
            role.setData(Qt.ItemDataRole.UserRole, job.id)
            role.setData(SEARCH_ROLE, f"{job.title} {job.company} {location}".casefold())
            role.setToolTip(f"{job.title}\n{job.company} · {location}")
            pay = QTableWidgetItem(salary(job.salary_min, job.salary_max))
            match = QTableWidgetItem(f"{job.score:.0f}")
            match.setData(SCORE_ROLE, float(job.score))
            match.setToolTip(self._score_tooltip(job))
            status = QTableWidgetItem(status_label(job.status))
            status.setData(STATUS_ROLE, job.status)
            status.setData(Qt.ItemDataRole.UserRole, job.status)
            can_apply = job.status == "new" or job.id in retryable_ids
            kind = "apply" if job.status == "new" else "retry" if can_apply else "view"
            action = QTableWidgetItem({"apply": "Apply", "retry": "Retry", "view": "View"}[kind])
            action.setData(ACTION_ROLE, kind)
            action.setToolTip({"apply": "Queue this application", "retry": "Try this application again",
                               "view": "Open the posting"}[kind])
            for column, item in enumerate((role, pay, match, status, action)):
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
        if item.data(ACTION_ROLE) in ("apply", "retry"):
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
        if action_item is not None and action_item.data(ACTION_ROLE) in ("apply", "retry"):
            menu.addAction(action_item.text(), lambda: self.apply_requested.emit(job.id))
        menu.addAction("Copy link", lambda: QGuiApplication.clipboard().setText(job.url))
        menu.addSeparator()
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
        for row in range(self.table.rowCount()):
            role = self.table.item(row, self.ROLE)
            status = self.table.item(row, self.STATUS)
            text = str(role.data(SEARCH_ROLE) or "") if role else ""
            value = status.data(STATUS_ROLE) if status else None
            self.table.setRowHidden(
                row, bool(query and query not in text) or (selected != "all" and value != selected)
            )
        self._update_empty_state()
