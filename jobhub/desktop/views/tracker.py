"""Tracker page: a sortable spreadsheet of every application that went out.

Columns sort by clicking their header. The Stage cell is editable (click it),
notes edit on double-click, and stages also advance automatically when inbox
tracking recognizes an assessment, interview, offer, or rejection email.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QMenu,
    QSpinBox,
    QStackedWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..delegates import SCORE_ROLE, STATUS_ROLE, SUBTITLE_ROLE, GlassTable, RoleDelegate, ScoreDelegate, StatusDelegate, TextDelegate
from ..format import (ACTIVE_STAGES, CLOSED_STAGES, STAGE_ORDER, compact_money, local_time, plural, relative_time,
                      salary, status_label, tracker_stats)
from ..native_glass import prepare_window
from ..widgets import EmptyState, FormGroup, GlassPanel, PageHeader, SearchField, SegmentedControl, button, label, paint_backdrop

SORT_ROLE = int(Qt.ItemDataRole.UserRole) + 40
ID_ROLE = int(Qt.ItemDataRole.UserRole) + 41


class SortItem(QTableWidgetItem):
    """A cell that sorts by a typed key (dates, money, stage order) instead of its text."""

    def __lt__(self, other):  # noqa: D105
        mine, theirs = self.data(SORT_ROLE), other.data(SORT_ROLE)
        if mine is None or theirs is None:
            return super().__lt__(other)
        try:
            return mine < theirs
        except TypeError:
            return str(mine) < str(theirs)


class StatTile(GlassPanel):
    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent, radius=14)
        column = QVBoxLayout(self)
        column.setContentsMargins(14, 10, 14, 10)
        column.setSpacing(0)
        self.value = label("0", "statValue", wrap=False)
        self.caption = label(title, "statLabel", wrap=False)
        column.addWidget(self.value)
        column.addWidget(self.caption)


class AddApplicationSheet(QDialog):
    """Record an application sent outside Starployment."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Track an application")
        prepare_window(self)
        self.setModal(True)
        self.setMinimumWidth(520)
        column = QVBoxLayout(self)
        column.setContentsMargins(24, 22, 24, 20)
        column.setSpacing(12)
        column.addWidget(label("Track an application", "sheetTitle"))
        column.addWidget(label("Add one you sent yourself, so the Tracker shows everything in one place.", "subtitle"))
        group = FormGroup()
        self.company = QLineEdit()
        self.company.setPlaceholderText("Company")
        self.title = QLineEdit()
        self.title.setPlaceholderText("Role")
        self.location = QLineEdit()
        self.location.setPlaceholderText("City, state or Remote")
        self.url = QLineEdit()
        self.url.setPlaceholderText("https:// (optional)")
        self.salary_min = QSpinBox()
        self.salary_max = QSpinBox()
        for spin in (self.salary_min, self.salary_max):
            spin.setRange(0, 2_000_000)
            spin.setSingleStep(5000)
            spin.setPrefix("$ ")
            spin.setGroupSeparatorShown(True)
            spin.setFixedWidth(140)
        pay = QWidget()
        pay_row = QHBoxLayout(pay)
        pay_row.setContentsMargins(0, 0, 0, 0)
        pay_row.addWidget(self.salary_min)
        pay_row.addWidget(label("to", "rowNote", wrap=False))
        pay_row.addWidget(self.salary_max)
        pay_row.addStretch(1)
        self.applied = QDateEdit(QDate.currentDate())
        self.applied.setCalendarPopup(True)
        self.applied.setDisplayFormat("MMM d, yyyy")
        self.stage = QComboBox()
        for stage in STAGE_ORDER:
            self.stage.addItem(status_label(stage), stage)
        group.add_row("Company", self.company)
        group.add_row("Role", self.title)
        group.add_row("Location", self.location)
        group.add_row("Salary", pay)
        group.add_row("Posting link", self.url)
        group.add_row("Applied on", self.applied)
        group.add_row("Stage", self.stage)
        column.addWidget(group)
        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = button("Cancel", "secondary")
        cancel.clicked.connect(self.reject)
        self.add = button("Add to Tracker")
        self.add.setDefault(True)
        self.add.clicked.connect(self.accept)
        self.add.setEnabled(False)
        for edit in (self.company, self.title):
            edit.textChanged.connect(lambda _=None: self.add.setEnabled(bool(self.company.text().strip() and self.title.text().strip())))
        actions.addWidget(cancel)
        actions.addWidget(self.add)
        column.addLayout(actions)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        paint_backdrop(painter, self)
        painter.end()

    def value(self) -> dict:
        day = self.applied.date()
        stamp = datetime(day.year(), day.month(), day.day(), 12, tzinfo=timezone.utc).isoformat(timespec="seconds")
        return {"company": self.company.text().strip(), "title": self.title.text().strip(),
                "location": self.location.text().strip(), "url": self.url.text().strip(),
                "salary_min": self.salary_min.value() or None, "salary_max": self.salary_max.value() or None,
                "applied_at": stamp, "stage": self.stage.currentData()}


class TrackerView(QWidget):
    stage_change_requested = Signal(int, str)
    note_save_requested = Signal(int, str)
    sync_email_requested = Signal()
    add_manual_requested = Signal(dict)
    open_requested = Signal(str)
    connections_requested = Signal()

    ROLE, LOCATION, SALARY, APPLIED, STAGE, UPDATED, SOURCE, MATCH, NOTE = range(9)
    HEADERS = ("ROLE", "LOCATION", "SALARY", "APPLIED", "STAGE", "UPDATED", "SOURCE", "MATCH", "NOTE")
    FILTERS = (("All", "all"), ("Active", "active"), ("Assessment", "assessment"), ("Interview", "interview"),
               ("Offer", "offer"), ("Closed", "closed"))

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.rows: list[dict] = []
        self._signature: object = None
        self._email_enabled = False
        column = QVBoxLayout(self)
        column.setContentsMargins(30, 26, 30, 22)
        column.setSpacing(14)
        self.header = PageHeader("Tracker", "Every application you sent, its pay and place, and where it stands.")
        self.sync_button = button("Check email", "secondary", icon="mail")
        self.sync_button.setToolTip("Look for assessment, interview, offer, and rejection emails now")
        self.sync_button.clicked.connect(self.sync_email_requested)
        add = button("Add", "secondary", icon="plus")
        add.setToolTip("Track an application you sent yourself")
        add.clicked.connect(self._add_dialog)
        export = button("Export", "quiet", icon="download")
        export.setToolTip("Save this table as a CSV spreadsheet")
        export.clicked.connect(self._export)
        for widget in (export, add, self.sync_button):
            self.header.add_action(widget)
        column.addWidget(self.header)

        stats = QHBoxLayout()
        stats.setSpacing(10)
        self.tiles: dict[str, StatTile] = {}
        for key, title in (("total", "Applied"), ("response_rate", "Response rate"), ("assessment", "Assessments"),
                           ("interview", "Interviews"), ("offer", "Offers"), ("median_salary", "Median salary")):
            tile = StatTile(title)
            self.tiles[key] = tile
            stats.addWidget(tile, 1)
        column.addLayout(stats)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.search = SearchField("Search company, role, or place")
        self.search.setMinimumWidth(240)
        self.search.setMaximumWidth(360)
        self.search.textChanged.connect(self._apply_filter)
        toolbar.addWidget(self.search, 1)
        toolbar.addStretch(0)
        self.filter = SegmentedControl(compact=True)
        for text, value in self.FILTERS:
            self.filter.addItem(text, value)
        self.filter.currentIndexChanged.connect(self._apply_filter)
        toolbar.addWidget(self.filter)
        column.addLayout(toolbar)

        card = GlassPanel(radius=18)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(8, 6, 8, 8)
        card_layout.setSpacing(4)
        self.table = GlassTable(0, len(self.HEADERS))
        self.table.setObjectName("glassTable")
        self.table.setAccessibleName("Application tracker")
        self.table.setHorizontalHeaderLabels(list(self.HEADERS))
        self.table.setItemDelegateForColumn(self.ROLE, RoleDelegate(self.table))
        for column_index in (self.LOCATION, self.SALARY, self.APPLIED, self.UPDATED, self.SOURCE, self.NOTE):
            self.table.setItemDelegateForColumn(column_index, TextDelegate(self.table))
        self.table.setItemDelegateForColumn(self.STAGE, StatusDelegate(self.table))
        self.table.setItemDelegateForColumn(self.MATCH, ScoreDelegate(self.table))
        header = self.table.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.setStretchLastSection(True)
        from PySide6.QtWidgets import QHeaderView
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(self.ROLE, QHeaderView.ResizeMode.Stretch)
        for index, width in ((self.LOCATION, 132), (self.SALARY, 104), (self.APPLIED, 78), (self.STAGE, 112),
                             (self.UPDATED, 84), (self.SOURCE, 92), (self.MATCH, 62), (self.NOTE, 120)):
            self.table.setColumnWidth(index, width)
        self.table.verticalHeader().setDefaultSectionSize(52)
        self.table.setSortingEnabled(True)
        self.table.sortItems(self.APPLIED, Qt.SortOrder.DescendingOrder)
        self.table.cellClicked.connect(self._cell_clicked)
        self.table.cellDoubleClicked.connect(self._cell_double_clicked)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.body = QStackedWidget()
        self.body.addWidget(self.table)
        self.empty = EmptyState("tracker", "Nothing sent yet",
                                "Applications appear here once they are submitted. Add ones you sent elsewhere with Add.")
        self.body.addWidget(self.empty)
        card_layout.addWidget(self.body, 1)
        footer = QHBoxLayout()
        footer.setContentsMargins(10, 2, 6, 0)
        self.footer = label("", "caption")
        footer.addWidget(self.footer, 1)
        self.connect_button = button("Connect your inbox", "quiet", icon="plug")
        self.connect_button.clicked.connect(self.connections_requested)
        footer.addWidget(self.connect_button)
        card_layout.addLayout(footer)
        column.addWidget(card, 1)

    # ------------------------------------------------------------------ data
    def set_email_state(self, enabled: bool, last_checked: str | None = None, unmatched: int = 0) -> None:
        self._email_enabled = enabled
        self.sync_button.setEnabled(enabled)
        self.connect_button.setVisible(not enabled)
        if enabled:
            text = "Stages update from your inbox" + (f" · checked {relative_time(last_checked).lower()}" if last_checked else "")
            if unmatched:
                text += f" · {plural(unmatched, 'recruiting email')} could not be matched"
        else:
            text = "Inbox tracking is off. Connect your mailbox to update stages automatically."
        self.footer.setText(text)

    def set_rows(self, rows: list[dict]) -> None:
        signature = tuple((r.get("id"), r.get("stage"), r.get("stage_updated_at"), r.get("candidate_note"),
                           r.get("title"), r.get("company"), r.get("status")) for r in rows)
        if signature == self._signature:
            return
        self._signature = signature
        self.rows = list(rows)
        self._update_stats()
        selected = self._selected_id()
        scroll = self.table.verticalScrollBar().value()
        sort_column = self.table.horizontalHeader().sortIndicatorSection()
        sort_order = self.table.horizontalHeader().sortIndicatorOrder()
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(0)
        self.table.setRowCount(len(rows))
        for row, record in enumerate(rows):
            for column_index, item in enumerate(self._items(record)):
                self.table.setItem(row, column_index, item)
        self.table.setSortingEnabled(True)
        self.table.sortItems(sort_column, sort_order)
        if selected is not None:
            for row in range(self.table.rowCount()):
                if self.table.item(row, self.ROLE).data(ID_ROLE) == selected:
                    self.table.selectRow(row)
                    break
        self.table.verticalScrollBar().setValue(scroll)
        self.table.setUpdatesEnabled(True)
        self._apply_filter()

    def _items(self, record: dict) -> list[QTableWidgetItem]:
        stage = str(record.get("stage") or ("applied" if record.get("status") == "applied" else "applied"))
        applied_at = str(record.get("finished_at") or record.get("started_at") or "")
        updated_at = str(record.get("stage_updated_at") or applied_at)
        location = record.get("location") or ("Remote" if record.get("remote") else "")
        role = SortItem(str(record.get("title") or "Role"))
        role.setData(SUBTITLE_ROLE, str(record.get("company") or "Employer"))
        role.setData(SORT_ROLE, f"{record.get('company', '')} {record.get('title', '')}".casefold())
        role.setData(ID_ROLE, record.get("id"))
        role.setToolTip(f"{record.get('title')}\n{record.get('company')}")
        place = SortItem(location or "—")
        place.setData(SORT_ROLE, str(location).casefold())
        place.setToolTip(location)
        pay_text = salary(record.get("salary_min"), record.get("salary_max"))
        pay = SortItem(pay_text)
        pay.setData(SORT_ROLE, float(record.get("salary_max") or record.get("salary_min") or 0))
        applied = SortItem(local_time(applied_at).split(",")[0] if applied_at else "—")
        applied.setData(SORT_ROLE, applied_at)
        applied.setToolTip(local_time(applied_at))
        stage_item = SortItem(status_label(stage))
        stage_item.setData(STATUS_ROLE, stage)
        stage_item.setData(SORT_ROLE, STAGE_ORDER.index(stage) if stage in STAGE_ORDER else 0)
        stage_item.setToolTip("Click to change the stage" + (" · updated from your inbox" if record.get("stage_source") == "email" else ""))
        updated = SortItem(relative_time(updated_at) if updated_at else "—")
        updated.setData(SORT_ROLE, updated_at)
        source_name = "Manual" if record.get("method") == "manual" else str(record.get("ats") or record.get("source") or "").title()
        source = SortItem(source_name or "—")
        source.setData(SORT_ROLE, source_name.casefold())
        match = SortItem(f"{float(record.get('score') or 0):.0f}")
        match.setData(SCORE_ROLE, float(record.get("score") or 0))
        match.setData(SORT_ROLE, float(record.get("score") or 0))
        note_text = str(record.get("candidate_note") or "")
        note = SortItem(note_text.splitlines()[0] if note_text else "—")
        note.setData(SORT_ROLE, note_text.casefold())
        note.setToolTip(note_text or "Double-click to add a note")
        return [role, place, pay, applied, stage_item, updated, source, match, note]

    def _update_stats(self) -> None:
        stats = tracker_stats(self.rows)
        self.tiles["total"].value.setText(f"{stats['total']:,}")
        self.tiles["response_rate"].value.setText(f"{stats['response_rate']}%")
        self.tiles["assessment"].value.setText(str(stats["assessment"]))
        self.tiles["interview"].value.setText(str(stats["interview"]))
        self.tiles["offer"].value.setText(str(stats["offer"]))
        self.tiles["median_salary"].value.setText(compact_money(stats["median_salary"]))
        places = "; ".join(f"{place} ({count})" for place, count in stats["top_locations"])
        self.tiles["median_salary"].setToolTip(f"Most common places: {places}" if places else "")
        top = stats["top_locations"][0] if stats["top_locations"] else None
        mostly = f" · most often {top[0]}" if top and top[1] >= 2 else ""
        self.header.set_subtitle(
            f"{plural(stats['total'], 'application')} sent · {stats['interview']} interviewing · "
            f"{stats['assessment']} with assessments" + mostly
            if stats["total"] else "Every application you sent, its pay and place, and where it stands.")

    def _apply_filter(self, *_args) -> None:
        wanted = self.filter.currentData() or "all"
        query = self.search.text().casefold().strip()
        visible = 0
        for row in range(self.table.rowCount()):
            stage = str(self.table.item(row, self.STAGE).data(STATUS_ROLE) or "applied")
            text = " ".join(str(self.table.item(row, c).text()) for c in (self.ROLE, self.LOCATION, self.NOTE))
            text += " " + str(self.table.item(row, self.ROLE).data(SUBTITLE_ROLE) or "")
            stage_ok = (wanted == "all" or (wanted == "active" and stage in ACTIVE_STAGES)
                        or (wanted == "closed" and stage in CLOSED_STAGES) or stage == wanted)
            hidden = not stage_ok or bool(query and query not in text.casefold())
            self.table.setRowHidden(row, hidden)
            visible += not hidden
        if not visible:
            if self.rows:
                self.empty.set_text("No matches", "Nothing in the Tracker matches this filter.")
            else:
                self.empty.set_text("Nothing sent yet", "Applications appear here once they are submitted. "
                                                        "Add ones you sent elsewhere with Add.")
        self.body.setCurrentWidget(self.table if visible else self.empty)

    def _selected_id(self):
        row = self.table.currentRow()
        item = self.table.item(row, self.ROLE) if row >= 0 else None
        return item.data(ID_ROLE) if item else None

    def _record_at(self, row: int) -> dict | None:
        item = self.table.item(row, self.ROLE) if row >= 0 else None
        app_id = item.data(ID_ROLE) if item else None
        return next((r for r in self.rows if r.get("id") == app_id), None)

    # --------------------------------------------------------------- actions
    def _stage_menu(self, record: dict, menu: QMenu | None = None) -> QMenu:
        menu = menu or QMenu(self)
        current = str(record.get("stage") or "applied")
        for stage in STAGE_ORDER:
            action = menu.addAction(status_label(stage), lambda s=stage: self.stage_change_requested.emit(int(record["id"]), s))
            action.setCheckable(True)
            action.setChecked(stage == current)
        return menu

    def _cell_clicked(self, row: int, column: int) -> None:
        record = self._record_at(row)
        if record is None or column != self.STAGE:
            return
        rect = self.table.visualItemRect(self.table.item(row, column))
        self._stage_menu(record).exec(self.table.viewport().mapToGlobal(rect.bottomLeft()))

    def _cell_double_clicked(self, row: int, column: int) -> None:
        record = self._record_at(row)
        if record is None:
            return
        if column == self.NOTE:
            self._edit_note(record)
        elif column != self.STAGE and record.get("url") and not str(record["url"]).startswith("manual://"):
            self.open_requested.emit(str(record["url"]))

    def _edit_note(self, record: dict) -> None:
        text, ok = QInputDialog.getMultiLineText(self, "Note", f"{record.get('company')} · {record.get('title')}",
                                                 str(record.get("candidate_note") or ""))
        if ok:
            self.note_save_requested.emit(int(record["id"]), text)

    def _context_menu(self, position) -> None:
        row = self.table.rowAt(position.y())
        record = self._record_at(row)
        if record is None:
            return
        self.table.selectRow(row)
        menu = QMenu(self)
        if record.get("url") and not str(record["url"]).startswith("manual://"):
            menu.addAction("Open posting", lambda: self.open_requested.emit(str(record["url"])))
        menu.addAction("Edit note…", lambda: self._edit_note(record))
        self._stage_menu(record, menu.addMenu("Set stage"))
        menu.exec(self.table.viewport().mapToGlobal(position))

    def _add_dialog(self) -> None:
        sheet = AddApplicationSheet(self.window())
        if sheet.exec() == QDialog.DialogCode.Accepted:
            self.add_manual_requested.emit(sheet.value())

    def export_csv(self, path: str) -> int:
        fields = ("company", "title", "location", "salary_min", "salary_max", "applied", "stage",
                  "stage_updated_at", "source", "score", "url", "note")
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(fields)
            for r in self.rows:
                writer.writerow([r.get("company"), r.get("title"), r.get("location"), r.get("salary_min") or "",
                                 r.get("salary_max") or "", r.get("finished_at") or r.get("started_at") or "",
                                 r.get("stage") or "applied", r.get("stage_updated_at") or "",
                                 "manual" if r.get("method") == "manual" else r.get("ats") or r.get("source") or "",
                                 f"{float(r.get('score') or 0):.0f}",
                                 "" if str(r.get("url") or "").startswith("manual://") else r.get("url") or "",
                                 r.get("candidate_note") or ""])
        return len(self.rows)

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export tracker", "starployment-applications.csv", "CSV (*.csv)")
        if path:
            self.export_csv(path)
