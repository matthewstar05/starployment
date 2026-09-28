"""Applications page: attempt list on the left, evidence and next steps on the right."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..delegates import RECORD_ROLE, SEARCH_ROLE, SUBTITLE_ROLE, ApplicationDelegate
from ..format import local_time, plural, pretty_log_line, relative_time, status_label
from ..widgets import EmptyState, GlassPanel, Monogram, PageHeader, StatusPill, button, label

NOTE_AUTOSAVE_MS = 900


class ApplicationsView(QWidget):
    open_requested = Signal(str)
    resume_requested = Signal(str)
    note_save_requested = Signal(int, str)
    answer_questions_requested = Signal(list)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("applicationWorkspace")
        self.applications: list[dict] = []
        self.selected_application: dict | None = None
        self._questions: list[str] = []
        self._signature: object = None
        self._loading_note = False
        self._note_dirty = False

        column = QVBoxLayout(self)
        column.setContentsMargins(30, 26, 30, 22)
        column.setSpacing(14)
        self.header = PageHeader("Applications", "Status, evidence, and next steps for every attempt.")
        column.addWidget(self.header)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(14)

        left = GlassPanel(radius=18)
        left.setObjectName("applicationListPanel")
        left.setMinimumWidth(280)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(6, 6, 6, 6)
        self.list_stack = QStackedWidget()
        self.list = QListWidget()
        self.list.setObjectName("applicationList")
        self.list.setAccessibleName("Applications")
        self.list.setItemDelegate(ApplicationDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.currentRowChanged.connect(self._select_application)
        self.empty = EmptyState("applications", "No applications yet",
                                "Apply from Jobs, or turn on automatic applications in Settings.")
        self.empty.setObjectName("applicationEmpty")
        self.list_stack.addWidget(self.list)
        self.list_stack.addWidget(self.empty)
        left_layout.addWidget(self.list_stack)
        split.addWidget(left)

        self.detail = GlassPanel(radius=18)
        self.detail.setObjectName("applicationDetail")
        detail_outer = QVBoxLayout(self.detail)
        detail_outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        detail_outer.addWidget(scroll)
        body = QWidget()
        body.setObjectName("page")
        scroll.setWidget(body)
        detail = QVBoxLayout(body)
        detail.setContentsMargins(24, 22, 24, 20)
        detail.setSpacing(12)

        heading = QHBoxLayout()
        heading.setSpacing(14)
        self.monogram = Monogram(48)
        heading.addWidget(self.monogram, 0, Qt.AlignmentFlag.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.detail_title = label("Choose an application", "detailTitle")
        titles.addWidget(self.detail_title)
        self.detail_company = label("Details, evidence, and your notes appear here.", "subtitle")
        titles.addWidget(self.detail_company)
        heading.addLayout(titles, 1)
        self.status_label = StatusPill()
        heading.addWidget(self.status_label, 0, Qt.AlignmentFlag.AlignTop)
        detail.addLayout(heading)

        self.meta_label = label("", "caption")
        detail.addWidget(self.meta_label)
        self.next_step = label("", "callout")
        detail.addWidget(self.next_step)
        self.questions_label = label("", "questions")
        self.questions_label.hide()
        detail.addWidget(self.questions_label)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.answer_button = button("Answer questions", icon="chevron-right", trailing=True)
        self.answer_button.clicked.connect(self._answer_questions)
        self.posting_button = button("Open posting", "secondary", icon="external")
        self.posting_button.clicked.connect(self._open_posting)
        self.resume_button = button("Open resume", "secondary", icon="file")
        self.resume_button.clicked.connect(self._open_resume)
        actions.addWidget(self.answer_button)
        actions.addWidget(self.posting_button)
        actions.addWidget(self.resume_button)
        actions.addStretch(1)
        detail.addLayout(actions)

        detail.addSpacing(4)
        self.evidence_title = label("Activity and evidence", "groupTitle")
        detail.addWidget(self.evidence_title)
        self.evidence = QPlainTextEdit()
        self.evidence.setObjectName("evidence")
        self.evidence.setReadOnly(True)
        self.evidence.setMinimumHeight(150)
        self.evidence.setAccessibleName("Application status and evidence")
        self.evidence.setPlaceholderText("Status, timing, submission evidence, and questions appear here.")
        detail.addWidget(self.evidence, 1)

        note_header = QHBoxLayout()
        note_header.addWidget(label("Private note", "groupTitle"))
        note_header.addStretch(1)
        self.note_status = label("", "caption", wrap=False)
        note_header.addWidget(self.note_status)
        detail.addLayout(note_header)
        self.note = QPlainTextEdit()
        self.note.setObjectName("applicationNote")
        self.note.setAccessibleName("Private application note")
        self.note.setPlaceholderText("Add a private reminder. Notes save automatically.")
        self.note.setFixedHeight(92)
        self.note.textChanged.connect(self._note_edited)
        detail.addWidget(self.note)
        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self.save_note = button("Save note", "secondary")
        self.save_note.clicked.connect(self._save_note)
        save_row.addWidget(self.save_note)
        detail.addLayout(save_row)
        self._autosave = QTimer(self)
        self._autosave.setSingleShot(True)
        self._autosave.setInterval(NOTE_AUTOSAVE_MS)
        self._autosave.timeout.connect(self._save_note)

        split.addWidget(self.detail)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        split.setSizes([330, 640])
        column.addWidget(split, 1)
        self.setMinimumWidth(700)
        self._set_detail_enabled(False)
        self._show_placeholder()

    # ------------------------------------------------------------------ data
    def set_applications(self, applications: list[dict]) -> None:
        signature = tuple(
            (a.get("id"), a.get("status"), a.get("finished_at"), a.get("candidate_note"),
             tuple(a.get("unanswered") or ()), len(a.get("log") or ()), a.get("submission_attempted"))
            for a in applications
        )
        if signature == self._signature:
            return
        self._signature = signature
        previous_id = (self.selected_application or {}).get("id")
        self.applications = list(applications)
        self.list.blockSignals(True)
        self.list.clear()
        restore = 0 if self.applications else -1
        for row, app in enumerate(self.applications):
            questions = list(dict.fromkeys(app.get("unanswered") or []))
            note = str(app.get("candidate_note") or "").strip()
            hint = note.splitlines()[0] if note else (plural(len(questions), "question") if questions else "")
            item = QListWidgetItem(f"{app.get('company') or 'Employer'}\n{app.get('title') or 'Role'}")
            item.setData(RECORD_ROLE, app)
            item.setData(SUBTITLE_ROLE, hint)
            item.setData(SEARCH_ROLE, relative_time(app.get("finished_at") or app.get("started_at")))
            item.setToolTip(f"{app.get('company') or 'Employer'} · {app.get('title') or 'Role'}\n"
                            f"{status_label(app.get('status'))}" + (f"\n{hint}" if hint else ""))
            item.setSizeHint(QSize(0, ApplicationDelegate.HEIGHT))
            self.list.addItem(item)
            if previous_id is not None and app.get("id") == previous_id:
                restore = row
        self.list.blockSignals(False)
        self.list_stack.setCurrentWidget(self.list if self.applications else self.empty)
        self.empty.setVisible(not self.applications)
        if restore >= 0:
            self.list.setCurrentRow(restore)
        else:
            self.selected_application = None
            self._set_detail_enabled(False)
            self._show_placeholder()

    def select_application_id(self, app_id: object) -> None:
        for row, app in enumerate(self.applications):
            if app.get("id") == app_id:
                self.list.setCurrentRow(row)
                return

    def _show_placeholder(self) -> None:
        self.detail_title.setText("Choose an application")
        self.detail_company.setText("Details, evidence, and your notes appear here.")
        self.monogram.set_text("")
        self.status_label.hide()
        self.meta_label.clear()
        self.next_step.hide()
        self.questions_label.hide()
        for widget in (self.answer_button, self.posting_button, self.resume_button):
            widget.hide()

    def _set_detail_enabled(self, enabled: bool) -> None:
        for widget in (self.note, self.answer_button, self.posting_button, self.resume_button, self.save_note):
            widget.setEnabled(enabled)

    def _select_application(self, row: int) -> None:
        if row < 0 or row >= len(self.applications):
            self.selected_application = None
            self._set_detail_enabled(False)
            self._show_placeholder()
            return
        application = self.applications[row]
        same_selection = (self.selected_application or {}).get("id") == application.get("id")
        if not same_selection and self._note_dirty:
            self._save_note()
        self.selected_application = application
        self._questions = list(dict.fromkeys(application.get("unanswered") or []))
        status = str(application.get("status") or "unknown")
        company = str(application.get("company") or "Employer")
        self.detail_title.setText(str(application.get("title") or "Role"))
        self.detail_company.setText(company + (f" · {application['location']}" if application.get("location") else ""))
        self.monogram.set_text(company)
        self.status_label.set_status(status, status_label(status))
        self.status_label.show()
        time_parts = [f"Started {local_time(application.get('started_at'))}"]
        if application.get("finished_at"):
            time_parts.append(f"Updated {local_time(application.get('finished_at'))}")
        time_parts.append("Submission attempted" if application.get("submission_attempted") else "Submission not attempted")
        self.meta_label.setText("  ·  ".join(time_parts))
        if status == "applied":
            text = "Employer confirmation recorded. Your application is complete."
        elif application.get("submission_attempted"):
            text = "The result is unconfirmed. Check the employer site before any retry."
        elif self._questions:
            text = "Next step · Answer the required questions below, then JobHub can try again."
        elif application.get("error"):
            text = "Next step · " + str(application["error"])
        else:
            text = "No unanswered questions are recorded."
        self.next_step.setText(text)
        self.next_step.show()
        if self._questions:
            self.questions_label.setText("Required questions\n" + "\n".join(f"•  {q}" for q in self._questions))
        else:
            self.questions_label.clear()
        self.questions_label.setVisible(bool(self._questions))
        sections = []
        if application.get("log"):
            sections.append("Activity\n" + "\n".join(pretty_log_line(line) for line in application["log"]))
        if application.get("screenshots"):
            sections.append("Screenshots\n" + "\n".join(str(path) for path in application["screenshots"]))
        if application.get("error") and self._questions:
            sections.append("Result details\n" + str(application["error"]))
        self.evidence.setPlainText("\n\n".join(sections) or "No activity or captured evidence is available.")
        if not (same_selection and self._note_dirty):
            self._loading_note = True
            self.note.setPlainText(str(application.get("candidate_note") or ""))
            self._loading_note = False
            self._note_dirty = False
            self.note_status.setText("")
        self.answer_button.setVisible(bool(self._questions) and not application.get("submission_attempted"))
        self.posting_button.setVisible(bool(application.get("url")))
        self.resume_button.setVisible(bool(application.get("resume_path")))
        self._set_detail_enabled(True)

    # --------------------------------------------------------------- actions
    def _note_edited(self) -> None:
        if self._loading_note or self.selected_application is None:
            return
        self._note_dirty = True
        self.note_status.setText("Editing…")
        self._autosave.start()

    def _answer_questions(self) -> None:
        if self._questions:
            self.answer_questions_requested.emit(self._questions)

    def _open_posting(self) -> None:
        if self.selected_application and self.selected_application.get("url"):
            self.open_requested.emit(str(self.selected_application["url"]))

    def _open_resume(self) -> None:
        if self.selected_application and self.selected_application.get("resume_path"):
            self.resume_requested.emit(str(self.selected_application["resume_path"]))

    def _save_note(self) -> None:
        self._autosave.stop()
        if self.selected_application and self.selected_application.get("id") is not None:
            text = self.note.toPlainText()
            self.selected_application["candidate_note"] = text
            self._note_dirty = False
            self.note_status.setText("Saved")
            self.note_save_requested.emit(int(self.selected_application["id"]), text)
