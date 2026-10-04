"""Applications page: prepared applications to review and send, plus every attempt.

The list on the left mixes two kinds of records:

* drafts, prepared by the Apply flow: a resume and the employer's questions,
  each with a proposed answer. Review, edit, and "Approve & send" here.
* attempts, from the browser engine: status, evidence, and a private note.

Filters narrow the list to starred applications, what needs you (To review),
what is queued (Approved), what went out (Sent), and what stopped (Issues).
The Sort menu orders the list; right-click a row to star it, mark it as sent
(applied outside Starployment), or remove it from the list.

Starred applications (priority companies are starred automatically) are
prepared like any other but never sent by "Send ready" or automatic runs:
each one waits for its own Approve & send.
"""
from __future__ import annotations

import re

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...answers import missing_answer
from ...models import posted_epoch
from ...priority import is_starred
from ..delegates import RECORD_ROLE, SEARCH_ROLE, SUBTITLE_ROLE, ApplicationDelegate
from ..editors import FlowLayout, _AutoText
from ..format import (local_time, parse_time, plural, posted_label, pretty_log_line, relative_time,
                      retryable_job_ids, salary, status_label)
from ..widgets import (
    ChoiceButton,
    EmptyState,
    GlassPanel,
    Monogram,
    PageHeader,
    SearchField,
    SegmentedControl,
    StatusPill,
    ToggleSwitch,
    button,
    label,
)

NOTE_AUTOSAVE_MS = 900
SOURCE_TEXT = {"saved": "Saved answer", "profile": "From your profile", "resume": "From your resume",
               "luna": "Drafted by Luna · review", "autofill": "AI Autofill · check", "you": "Your answer", "none": "Needs your answer",
               "declined": "Declined", "file": "Attached automatically"}
FILTERS = (("All", "all"), ("Starred", "starred"), ("To review", "review"), ("Approved", "approved"),
           ("Sent", "sent"), ("Issues", "issues"))
SORTS = (("Recent activity", "recent"), ("Newest posted", "posted"), ("Best match", "match"),
         ("Company A–Z", "company"), ("Status", "status"))
BUCKET_ORDER = {"review": 0, "approved": 1, "issues": 2, "sent": 3}
OPEN_DRAFT = {"preparing", "ready", "needs_answers", "approved", "sending", "practice", "error"}


def activity_time(record: dict) -> float:
    """When a record last changed: a draft's update, or an attempt's finish (else start)."""
    if record.get("kind") == "draft":
        value = record.get("updated_at") or record.get("created_at")
    else:
        value = record.get("finished_at") or record.get("started_at")
    when = parse_time(value)
    return when.timestamp() if when else 0.0


def latest_attempts(applications: list[dict]) -> list[dict]:
    """One attempt per posting: its confirmed application if it has one, else the newest attempt.

    Earlier tries of a posting (for example two stopped attempts before a
    successful send) are history, not separate applications to act on.
    """
    best: dict[object, dict] = {}
    for record in applications:
        if record.get("hidden"):
            continue
        key = record.get("canonical_key") or record.get("job_id") or ("id", record.get("id"))
        current = best.get(key)
        rank = (record.get("status") == "applied", int(record.get("id") or 0))
        if current is None or rank > (current.get("status") == "applied", int(current.get("id") or 0)):
            best[key] = record
    return [record for record in applications if any(record is kept for kept in best.values())]


def sort_records(records: list[dict], key: str) -> list[dict]:
    """Order Applications rows. Unknown posting dates sort last."""
    if key == "posted":
        return sorted(records, key=lambda r: (posted_epoch(r.get("posted_at")) is None,
                                              -(posted_epoch(r.get("posted_at")) or 0), -activity_time(r)))
    if key == "match":
        return sorted(records, key=lambda r: (-float(r.get("score") or 0), -activity_time(r)))
    if key == "company":
        return sorted(records, key=lambda r: (str(r.get("company") or "").casefold(), -activity_time(r)))
    if key == "status":
        return sorted(records, key=lambda r: (BUCKET_ORDER.get(bucket(r), 9), -activity_time(r)))
    return sorted(records, key=lambda r: -activity_time(r))


def bucket(record: dict) -> str:
    status = str(record.get("status") or "")
    if record.get("kind") == "draft":
        return "approved" if status in ("approved", "sending") else "review"
    if status == "applied" or record.get("submission_attempted"):
        return "sent"
    return "issues"


class QuestionRow(QWidget):
    """One employer question: its text, where the answer came from, and an editor."""

    def __init__(self, question: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("formRow")
        self.question = question
        self.original = question.get("answer")
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 8, 0, 8)
        column.setSpacing(5)
        top = QHBoxLayout()
        top.setSpacing(8)
        title = label(question["label"] + ("  *" if question.get("required") else ""), "questionLabel")
        title.setToolTip("Required" if question.get("required") else "Optional")
        top.addWidget(title, 1)
        source = question.get("source", "none")
        badge = label(SOURCE_TEXT.get(source, source), "sourceBadge", wrap=False)
        if source in ("luna", "autofill"):
            badge.setObjectName("lunaNote")
        top.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        column.addLayout(top)
        self.editor = self._editor(question)
        column.addWidget(self.editor)
        if question.get("note") and source == "luna":
            column.addWidget(label("Luna used: " + str(question["note"]), "lunaNote"))
        elif question.get("note") and source == "autofill":
            column.addWidget(label(str(question["note"]), "lunaNote"))
        elif question.get("note") and source == "you":
            column.addWidget(label(str(question["note"]), "rowNote"))

    def _editor(self, question: dict) -> QWidget:
        kind, value = question.get("kind"), question.get("answer")
        options = list(question.get("options") or [])
        if kind == "file":
            if question.get("source") == "file":
                attached = ("A cover letter written from your resume is attached." if re.search(r"cover", question["label"], re.I)
                            else "Your resume for this role is attached.")
                return label(attached, "rowNote")
            return label("Starployment can't attach this file. Apply on the employer's site, then right-click "
                         "the application and choose Mark as sent." if question.get("required") else
                         "Optional: Starployment leaves this upload empty.", "rowNote")
        if kind == "checkbox" and options:
            chosen = {str(v).casefold() for v in (value if isinstance(value, list) else [])}
            holder = QWidget()
            holder.setObjectName("formRow")
            flow = FlowLayout(holder, spacing=14)
            holder.boxes = []
            for option in options:
                box = QCheckBox(option)
                box.setChecked(option.casefold() in chosen)
                flow.addWidget(box)
                holder.boxes.append(box)
            return holder
        if kind == "checkbox":
            toggle = ToggleSwitch("Yes")
            toggle.setChecked(value is True or str(value).casefold() in {"true", "yes"})
            return toggle
        if options:
            combo = QComboBox()
            combo.addItem("Choose…", "")
            for option in options:
                combo.addItem(option, option)
            index = combo.findData(value) if value else 0
            combo.setCurrentIndex(max(0, index))
            return combo
        if kind == "textarea" or (isinstance(value, str) and len(value) > 90):
            edit = _AutoText(str(value or ""))
            edit.setPlaceholderText("Type your answer")
            return edit
        edit = QLineEdit(str(value or ""))
        edit.setPlaceholderText("Type your answer")
        return edit

    def value(self):
        editor = self.editor
        if hasattr(editor, "boxes"):
            return [box.text() for box in editor.boxes if box.isChecked()]
        if isinstance(editor, ToggleSwitch):
            return editor.isChecked()
        if isinstance(editor, QComboBox):
            return editor.currentData() or ""
        if isinstance(editor, QPlainTextEdit):
            return editor.toPlainText().strip()
        if isinstance(editor, QLineEdit):
            return editor.text().strip()
        return self.original

    def edited(self) -> bool:
        current, original = self.value(), self.original
        if hasattr(self.editor, "boxes"):
            return sorted(current) != sorted(original if isinstance(original, list) else [])
        if isinstance(self.editor, ToggleSwitch):
            return bool(current) != (original is True or str(original).casefold() in {"true", "yes"})
        return str(current or "") != str(original if original is not None else "")


class DraftDetail(QWidget):
    """Review panel for a prepared application."""

    approve_requested = Signal(int, dict)
    save_requested = Signal(int, dict)
    luna_requested = Signal(int)
    remove_requested = Signal(int)
    open_requested = Signal(str)
    resume_requested = Signal(str)
    star_requested = Signal(str, bool)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.draft: dict | None = None
        self.rows: list[QuestionRow] = []
        self._show_all = False
        self._practice = False
        self._luna_connected = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        body = QWidget()
        body.setObjectName("page")
        scroll.setWidget(body)
        column = QVBoxLayout(body)
        column.setContentsMargins(24, 22, 24, 20)
        column.setSpacing(12)
        heading = QHBoxLayout()
        heading.setSpacing(14)
        self.monogram = Monogram(48)
        heading.addWidget(self.monogram, 0, Qt.AlignmentFlag.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title = label("", "detailTitle")
        self.company = label("", "subtitle")
        titles.addWidget(self.title)
        titles.addWidget(self.company)
        heading.addLayout(titles, 1)
        self.status = StatusPill()
        heading.addWidget(self.status, 0, Qt.AlignmentFlag.AlignTop)
        column.addLayout(heading)
        self.meta = label("", "caption")
        column.addWidget(self.meta)
        self.next_step = label("", "callout")
        column.addWidget(self.next_step)
        self.practice_note = label("Practice mode is on in Settings. Sending fills the employer's form but does not "
                                   "click Submit.", "questions")
        self.practice_note.hide()
        column.addWidget(self.practice_note)
        self.starred_note = label("Starred · This application waits for you. Refine it, then use Approve & send. "
                                  "Send ready and automatic runs skip starred applications.", "questions")
        self.starred_note.hide()
        column.addWidget(self.starred_note)

        actions_holder = QWidget()
        actions_holder.setObjectName("formRow")
        actions = FlowLayout(actions_holder, spacing=8)
        self.approve = button("Approve & send", icon="applications")
        self.approve.setToolTip("Approve every answer shown and send this application")
        self.approve.clicked.connect(self._approve)
        self.save = button("Save answers", "secondary")
        self.save.clicked.connect(self._save)
        self.luna = button("AI Autofill", "secondary", icon="luna")
        self.luna.setToolTip("Fill every open question with a realistic answer from your profile. "
                             "Assumptions are labeled; check them before you send.")
        self.luna.clicked.connect(lambda: self.draft and self.luna_requested.emit(int(self.draft["id"])))
        self.resume = button("Resume", "quiet", icon="file")
        self.resume.setToolTip("Open the resume prepared for this role")
        self.resume.clicked.connect(lambda: self.draft and self.resume_requested.emit(str(self.draft.get("resume_path") or "")))
        self.posting = button("Posting", "quiet", icon="external")
        self.posting.setToolTip("Open the job posting")
        self.posting.clicked.connect(lambda: self.draft and self.open_requested.emit(str(self.draft.get("url") or "")))
        self.remove = button("Remove", "quiet", icon="trash")
        self.remove.setToolTip("Remove this prepared application")
        self.remove.clicked.connect(self._remove)
        self.star = button("Star", "quiet", icon="star")
        self.star.clicked.connect(self._toggle_star)
        for widget in (self.approve, self.luna, self.save, self.star, self.resume, self.posting, self.remove):
            actions.addWidget(widget)
        column.addWidget(actions_holder)

        questions_head = QHBoxLayout()
        self.questions_title = label("Questions", "groupTitle")
        questions_head.addWidget(self.questions_title)
        questions_head.addStretch(1)
        self.show_all = button("Show all fields", "quiet")
        self.show_all.clicked.connect(self._toggle_all)
        questions_head.addWidget(self.show_all)
        column.addLayout(questions_head)
        self.questions_panel = GlassPanel(radius=14, material="inset")
        self.questions_layout = QVBoxLayout(self.questions_panel)
        self.questions_layout.setContentsMargins(16, 6, 16, 6)
        self.questions_layout.setSpacing(0)
        column.addWidget(self.questions_panel)
        self.no_questions = label("", "rowNote")
        column.addWidget(self.no_questions)
        column.addStretch(1)

    # -------------------------------------------------------------- data
    def set_context(self, *, practice: bool, luna_connected: bool) -> None:
        self._practice, self._luna_connected = practice, luna_connected
        if self.draft is not None:
            self.practice_note.setVisible(practice)
            self.luna.setEnabled(self.draft.get("status") in ("ready", "needs_answers", "practice", "error"))

    def dirty(self) -> bool:
        return any(row.edited() for row in self.rows)

    def edits(self) -> dict:
        return {row.question["id"]: row.value() for row in self.rows if row.edited()}

    def set_draft(self, draft: dict) -> None:
        self.draft = draft
        status = str(draft.get("status") or "")
        company = str(draft.get("company") or "Employer")
        self.title.setText(str(draft.get("title") or "Role"))
        place = draft.get("location") or ("Remote" if draft.get("remote") else "")
        pay = salary(draft.get("salary_min"), draft.get("salary_max"))
        self.company.setText(" · ".join(x for x in (company, place, pay if pay != "—" else "") if x))
        self.monogram.set_text(company)
        self.status.set_status(status, status_label(status))
        questions = list(draft.get("questions") or [])
        missing = [q for q in questions if missing_answer(q)]
        luna = [q for q in questions if q.get("source") in ("luna", "autofill") and not q.get("approved")]
        source = {"greenhouse_form": "Greenhouse", "lever_form": "Lever", "ashby_form": "Ashby"}.get(
            str(draft.get("question_source") or ""), "")
        meta = [f"Match {float(draft.get('score') or 0):.0f}"]
        if draft.get("posted_at"):
            meta.append(f"Posted {local_time(draft['posted_at'])}")
        if source:
            meta.append(f"{source} form read ahead")
        if draft.get("resume_note"):
            meta.append(str(draft["resume_note"]))
        self.meta.setText("  ·  ".join(meta))
        if status == "preparing":
            text = "Preparing: reading the employer's form and building your resume…"
        elif status == "sending":
            text = "Sending now. Starployment will confirm the employer's response."
        elif status == "approved":
            text = "Approved and waiting to send. Use Send approved, or send it now."
        elif status == "error":
            text = "This needs attention · " + (str(draft.get("error") or "") or "Prepare it again from Jobs.")
        elif status == "practice":
            text = "Practice run finished. Nothing was submitted. Review, then send when practice mode is off."
        elif missing:
            names = "; ".join(str(q["label"])[:60] for q in missing[:3]) + (" and more" if len(missing) > 3 else "")
            text = (f"{plural(len(missing), 'required question')} need{'s' if len(missing) == 1 else ''} your answer "
                    f"before sending: {names}" + (f" · Last attempt: {draft['error']}" if draft.get("error") else "") + ".")
        elif luna:
            text = (f"{plural(len(luna), 'answer')} came from Luna or AI Autofill, including any labeled assumptions. "
                    "Check them, edit anything, then Approve & send.")
        elif questions:
            text = "Every known question has an answer from your saved facts. Approve & send when ready."
        else:
            text = "The form's questions are checked when sending. Saved answers fill what they can; anything else comes back here."
        self.next_step.setText(text)
        self.practice_note.setVisible(self._practice)
        sendable = status in ("ready", "needs_answers", "approved", "practice", "error")
        self.approve.setEnabled(sendable)
        self.approve.setText("Send now" if status == "approved" else "Approve & send")
        self.save.setEnabled(sendable)
        self.luna.setEnabled(status in ("ready", "needs_answers", "practice", "error"))
        self.luna.setToolTip("Fill every open question with a realistic answer from your profile. Assumptions are "
                             "labeled; check them before you send." if self._luna_connected
                             else "Connect Luna in Settings → Connections to use AI Autofill")
        self.resume.setVisible(bool(draft.get("resume_path")))
        self.posting.setVisible(bool(draft.get("url")))
        self.remove.setEnabled(status not in ("sending",))
        starred = bool(draft.get("_starred"))
        self.star.setText("Starred" if starred else "Star")
        self.star.setToolTip("Remove the star. Send ready can then include this application." if starred else
                             "Star this application to refine it. Starred applications are only sent one by one.")
        self.starred_note.setVisible(starred and status not in ("sending",))
        self._build_rows()

    def _toggle_star(self) -> None:
        if self.draft is not None and self.draft.get("job_id"):
            self.star_requested.emit(str(self.draft["job_id"]), not bool(self.draft.get("_starred")))

    def _build_rows(self) -> None:
        while self.questions_layout.count():
            item = self.questions_layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self.rows = []
        questions = list((self.draft or {}).get("questions") or [])
        routine = [q for q in questions if q.get("source") in ("profile", "resume", "file", "declined")]
        shown = questions if self._show_all else [q for q in questions if q not in routine]
        # What still needs you comes first, then Luna's drafts to check, then the rest.
        def priority(q):
            if missing_answer(q):
                return 0
            if q.get("answer") in (None, "", []) and q.get("kind") != "file":
                return 2
            return 1 if q.get("source") in ("luna", "autofill") and not q.get("approved") else 3
        shown = sorted(shown, key=priority)
        self.show_all.setVisible(bool(routine))
        self.show_all.setText("Hide profile fields" if self._show_all else f"Show all fields (+{len(routine)})")
        for index, question in enumerate(shown):
            row = QuestionRow(question)
            self.rows.append(row)
            self.questions_layout.addWidget(row)
            if index < len(shown) - 1:
                line = QFrame()
                line.setFrameShape(QFrame.Shape.HLine)
                self.questions_layout.addWidget(line)
        self.questions_panel.setVisible(bool(shown))
        if not questions:
            self.no_questions.setText("No questions were published ahead of time for this form.")
        elif not shown:
            self.no_questions.setText(f"All {len(routine)} fields are answered from your profile and resume.")
        else:
            self.no_questions.setText("")
        self.no_questions.setVisible(bool(self.no_questions.text()))
        self.questions_title.setText(f"Questions ({len(shown)})" if shown else "Questions")

    def _toggle_all(self) -> None:
        edits = self.edits()
        self._show_all = not self._show_all
        self._build_rows()
        for row in self.rows:
            if row.question["id"] in edits:
                value = edits[row.question["id"]]
                editor = row.editor
                if isinstance(editor, QComboBox):
                    editor.setCurrentIndex(max(0, editor.findData(value)))
                elif isinstance(editor, QPlainTextEdit):
                    editor.setPlainText(str(value))
                elif isinstance(editor, QLineEdit):
                    editor.setText(str(value))
                elif isinstance(editor, ToggleSwitch):
                    editor.setChecked(bool(value))

    def _approve(self) -> None:
        if self.draft is not None:
            self.approve_requested.emit(int(self.draft["id"]), self.edits())

    def _save(self) -> None:
        if self.draft is not None:
            self.save_requested.emit(int(self.draft["id"]), self.edits())

    def _remove(self) -> None:
        if self.draft is not None:
            self.remove_requested.emit(int(self.draft["id"]))


class ApplicationsView(QWidget):
    open_requested = Signal(str)
    resume_requested = Signal(str)
    apply_requested = Signal(str)
    note_save_requested = Signal(int, str)
    answer_questions_requested = Signal(list)
    # Prepared-application workflow
    approve_send_requested = Signal(int, dict)
    save_draft_requested = Signal(int, dict)
    luna_requested = Signal(int)
    remove_draft_requested = Signal(int)
    send_approved_requested = Signal()
    reset_requested = Signal(bool)
    prepare_requested = Signal(str)
    autofill_all_requested = Signal()
    reopen_requested = Signal(int)
    star_requested = Signal(str, bool)
    mark_sent_requested = Signal(str)
    hide_requested = Signal(int)
    discard_draft_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("applicationWorkspace")
        self.applications: list[dict] = []
        self.drafts: list[dict] = []
        self.records: list[dict] = []
        self.priority: list[str] = []
        self.selected_application: dict | None = None
        self._questions: list[str] = []
        self._signature: object = None
        self._loading_note = False
        self._note_dirty = False
        self._continuable_ids: set[object] = set()

        column = QVBoxLayout(self)
        column.setContentsMargins(30, 26, 30, 22)
        column.setSpacing(14)
        self.header = PageHeader("Applications", "Review prepared applications, send them, and see every attempt.")
        self.reset_button = button("Reset unfinished…", "secondary", icon="reset")
        self.reset_button.setToolTip("Remove prepared applications that were not sent, so you can prepare them again")
        self.reset_button.clicked.connect(self._confirm_reset)
        self.autofill_all_button = button("Autofill all", "secondary", icon="luna")
        self.autofill_all_button.setToolTip("Run AI Autofill on every prepared application that still has open questions")
        self.autofill_all_button.clicked.connect(self.autofill_all_requested)
        self.send_all_button = button("Send ready", icon="applications")
        self.send_all_button.setToolTip("Approve and send every application that is ready. "
                                        "Starred applications are left for you to send one by one.")
        self.send_all_button.setEnabled(False)
        self.send_all_button.clicked.connect(self.send_approved_requested)
        self.header.add_action(self.reset_button)
        self.header.add_action(self.autofill_all_button)
        self.header.add_action(self.send_all_button)
        column.addWidget(self.header)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.search = SearchField("Search company or role")
        self.search.setMinimumWidth(180)
        self.search.setMaximumWidth(340)
        self.search.textChanged.connect(self._rebuild_list)
        toolbar.addWidget(self.search, 1)
        toolbar.addStretch(0)
        self.filter = SegmentedControl(compact=True)
        for text, value in FILTERS:
            self.filter.addItem(text, value)
        self.filter.currentIndexChanged.connect(self._rebuild_list)
        toolbar.addWidget(self.filter)
        column.addLayout(toolbar)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(14)

        left = GlassPanel(radius=18)
        left.setObjectName("applicationListPanel")
        left.setMinimumWidth(280)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(6, 6, 6, 6)
        left_layout.setSpacing(2)
        list_head = QHBoxLayout()
        list_head.setContentsMargins(10, 2, 2, 0)
        self.list_count = label("", "caption", wrap=False)
        list_head.addWidget(self.list_count)
        list_head.addStretch(1)
        self.sort = ChoiceButton("sort", "Sort")
        for text, value in SORTS:
            self.sort.add_option(text, value)
        self.sort.changed.connect(self._rebuild_list)
        list_head.addWidget(self.sort)
        left_layout.addLayout(list_head)
        self.list_stack = QStackedWidget()
        self.list = QListWidget()
        self.list.setObjectName("applicationList")
        self.list.setAccessibleName("Applications")
        self.list.setItemDelegate(ApplicationDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.currentRowChanged.connect(self._select_row)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.empty = EmptyState("applications", "No applications yet",
                                "Press Apply on Overview to prepare applications for your best matches.")
        self.empty.setObjectName("applicationEmpty")
        self.list_stack.addWidget(self.list)
        self.list_stack.addWidget(self.empty)
        left_layout.addWidget(self.list_stack)
        split.addWidget(left)

        self.detail = GlassPanel(radius=18)
        self.detail.setObjectName("applicationDetail")
        detail_outer = QVBoxLayout(self.detail)
        detail_outer.setContentsMargins(0, 0, 0, 0)
        self.detail_stack = QStackedWidget()
        detail_outer.addWidget(self.detail_stack)
        self.attempt_page = self._build_attempt_detail()
        self.draft_detail = DraftDetail()
        self.draft_detail.approve_requested.connect(self.approve_send_requested)
        self.draft_detail.save_requested.connect(self.save_draft_requested)
        self.draft_detail.luna_requested.connect(self.luna_requested)
        self.draft_detail.remove_requested.connect(self._confirm_remove)
        self.draft_detail.open_requested.connect(self.open_requested)
        self.draft_detail.resume_requested.connect(self.resume_requested)
        self.draft_detail.star_requested.connect(self.star_requested)
        self.detail_stack.addWidget(self.attempt_page)
        self.detail_stack.addWidget(self.draft_detail)
        split.addWidget(self.detail)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        split.setSizes([330, 640])
        column.addWidget(split, 1)
        self.setMinimumWidth(700)
        self._set_detail_enabled(False)
        self._show_placeholder()

    def _build_attempt_detail(self) -> QWidget:
        page = QWidget()
        page.setObjectName("page")
        detail_outer = QVBoxLayout(page)
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
        self.answer_button = button("Review & answer", icon="chevron-right", trailing=True)
        self.answer_button.setToolTip("Prepare this application again with your answers to these questions")
        self.answer_button.clicked.connect(self._answer_questions)
        self.posting_button = button("Open posting", "secondary", icon="external")
        self.posting_button.clicked.connect(self._open_posting)
        self.resume_button = button("Open resume", "secondary", icon="file")
        self.resume_button.clicked.connect(self._open_resume)
        self.continue_button = button("Try again", icon="applications")
        self.continue_button.clicked.connect(self._continue_application)
        self.reopen_button = button("Prepare again", icon="reset")
        self.reopen_button.setToolTip("The employer refused this submission, so it was not received. "
                                      "Prepare it again to answer and send it.")
        self.reopen_button.clicked.connect(
            lambda: self.selected_application and self.reopen_requested.emit(int(self.selected_application["id"])))
        actions.addWidget(self.reopen_button)
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
        self.save_note = button("Save note", "secondary")
        self.save_note.clicked.connect(self._save_note)
        save_row.addWidget(self.save_note)
        save_row.addStretch(1)
        save_row.addWidget(self.continue_button)
        detail.addLayout(save_row)
        self._autosave = QTimer(self)
        self._autosave.setSingleShot(True)
        self._autosave.setInterval(NOTE_AUTOSAVE_MS)
        self._autosave.timeout.connect(self._save_note)
        return page

    # ------------------------------------------------------------------ data
    def set_context(self, *, practice: bool = False, luna_connected: bool = False,
                    priority: list[str] | None = None) -> None:
        self.draft_detail.set_context(practice=practice, luna_connected=luna_connected)
        if priority is not None and list(priority) != self.priority:
            self.priority = list(priority)
            self._signature = None
            if self.applications or self.drafts:
                self.set_applications(self.applications, self.drafts)

    def set_applications(self, applications: list[dict], drafts: list[dict] | None = None) -> None:
        drafts = self.drafts if drafts is None else drafts
        signature = (tuple(
            (a.get("id"), a.get("status"), a.get("finished_at"), a.get("candidate_note"),
             tuple(a.get("unanswered") or ()), len(a.get("log") or ()), a.get("submission_attempted"),
             a.get("job_id"), a.get("canonical_key"), a.get("title"), a.get("company"),
             a.get("url"), a.get("location"), a.get("resume_path"), a.get("hidden"), a.get("starred"),
             a.get("posted_at"), a.get("score"))
            for a in applications
        ), tuple((d.get("id"), d.get("status"), d.get("updated_at"), d.get("score"), d.get("starred"),
                  d.get("posted_at")) for d in drafts))
        if signature == self._signature:
            return
        self._signature = signature
        self.applications = list(applications)
        self.drafts = list(drafts)
        retryable = retryable_job_ids(self.applications)
        latest = {}
        for record in self.applications:
            job_id = record.get("job_id")
            if job_id and (job_id not in latest or int(record.get("id") or 0) > int(latest[job_id].get("id") or 0)):
                latest[job_id] = record
        self._continuable_ids = {record.get("id") for job_id, record in latest.items() if job_id in retryable}
        open_jobs = {d.get("job_id") for d in self.drafts if d.get("status") in OPEN_DRAFT}
        records = [dict(d, kind="draft") for d in self.drafts if d.get("status") in OPEN_DRAFT]
        records += [dict(a, kind="attempt") for a in latest_attempts(self.applications)
                    if a.get("job_id") not in open_jobs]
        for record in records:
            record["_starred"] = is_starred(record, self.priority)
        self.records = records
        approved = sum(1 for d in self.drafts if d.get("status") == "approved")
        # Starred applications are sent one by one, never by Send ready.
        sendable = sum(1 for d in records if d["kind"] == "draft" and (
            d.get("status") == "approved" or (d.get("status") == "ready" and not d["_starred"])))
        self.send_all_button.setText(f"Send ready ({sendable})" if sendable else "Send ready")
        self.send_all_button.setEnabled(bool(sendable))
        self.autofill_all_button.setEnabled(any(
            d.get("status") in ("ready", "needs_answers") and any(
                q.get("answer") in (None, "", []) and q.get("kind") != "file" for q in d.get("questions") or [])
            for d in self.drafts))
        review = sum(1 for d in self.drafts if d.get("status") in ("ready", "needs_answers", "practice"))
        sent = sum(1 for a in self.applications if a.get("status") == "applied")
        self.header.set_subtitle(
            f"{plural(review, 'application')} to review · {approved} approved · {sent} confirmed"
            if self.drafts else "Review prepared applications, send them, and see every attempt.")
        self._rebuild_list()

    def _visible_records(self) -> list[dict]:
        wanted = self.filter.currentData() or "all"
        query = self.search.text().casefold().strip()
        out = []
        for record in self.records:
            if wanted == "starred" and not record.get("_starred"):
                continue
            if wanted not in ("all", "starred") and bucket(record) != wanted:
                continue
            if query and query not in f"{record.get('company', '')} {record.get('title', '')}".casefold():
                continue
            out.append(record)
        return sort_records(out, str(self.sort.value() or "recent"))

    def _rebuild_list(self, *_args) -> None:
        previous = self._selected_key()
        visible = self._visible_records()
        by_posted = self.sort.value() == "posted"
        self.list_count.setText(plural(len(visible), "application") if visible else "")
        self.list.blockSignals(True)
        self.list.clear()
        restore = 0 if visible else -1
        for row, record in enumerate(visible):
            if record["kind"] == "draft":
                questions = record.get("questions") or []
                open_count = sum(1 for q in questions if missing_answer(q))
                luna = sum(1 for q in questions if q.get("source") in ("luna", "autofill") and not q.get("approved"))
                hint = (f"{plural(open_count, 'answer')} needed" if open_count else
                        f"Luna drafted {luna}" if luna else f"Match {float(record.get('score') or 0):.0f}")
                when = relative_time(record.get("updated_at") or record.get("created_at"))
            else:
                questions = list(dict.fromkeys(record.get("unanswered") or []))
                note = str(record.get("candidate_note") or "").strip()
                hint = note.splitlines()[0] if note else (plural(len(questions), "question") if questions else "")
                when = relative_time(record.get("finished_at") or record.get("started_at"))
            if by_posted:
                ago = posted_label(record.get("posted_at"))[1]
                when = f"Posted {ago[0].lower()}{ago[1:]}" if ago else ""
            item = QListWidgetItem(f"{record.get('company') or 'Employer'}\n{record.get('title') or 'Role'}")
            item.setData(RECORD_ROLE, record)
            item.setData(SUBTITLE_ROLE, hint)
            item.setData(SEARCH_ROLE, when)
            item.setToolTip(f"{record.get('company') or 'Employer'} · {record.get('title') or 'Role'}\n"
                            f"{status_label(record.get('status'))}" + (f"\n{hint}" if hint else "")
                            + ("\nStarred" if record.get("_starred") else "")
                            + (f"\nPosted {local_time(record['posted_at'])}" if record.get("posted_at") else ""))
            item.setSizeHint(QSize(0, ApplicationDelegate.HEIGHT))
            self.list.addItem(item)
            if previous is not None and self._key(record) == previous:
                restore = row
        self.list.blockSignals(False)
        self.list_stack.setCurrentWidget(self.list if visible else self.empty)
        if not visible:
            if self.records:
                self.empty.set_text("Nothing here", "No applications match this filter.")
            else:
                self.empty.set_text("No applications yet",
                                    "Press Apply on Overview to prepare applications for your best matches.")
        self.empty.setVisible(not visible)
        if restore >= 0:
            self.list.setCurrentRow(restore)   # list was cleared, so this always reselects
        else:
            self.selected_application = None
            self._set_detail_enabled(False)
            self._show_placeholder()

    @staticmethod
    def _key(record: dict) -> tuple:
        return (record.get("kind"), record.get("id"))

    def _selected_key(self):
        item = self.list.currentItem()
        record = item.data(RECORD_ROLE) if item else None
        return self._key(record) if record else None

    def select_application_id(self, app_id: object) -> None:
        for row in range(self.list.count()):
            record = self.list.item(row).data(RECORD_ROLE) or {}
            if record.get("kind") == "attempt" and record.get("id") == app_id:
                self.list.setCurrentRow(row)
                return
        if self.filter.currentData() != "all":
            self.filter.setCurrentIndex(0)
            self.select_application_id(app_id)

    def select_draft_for_job(self, job_id: str) -> None:
        for row in range(self.list.count()):
            record = self.list.item(row).data(RECORD_ROLE) or {}
            if record.get("kind") == "draft" and record.get("job_id") == job_id:
                self.list.setCurrentRow(row)
                return

    def show_filter(self, value: str) -> None:
        index = self.filter.findData(value)
        if index >= 0:
            self.filter.setCurrentIndex(index)

    def _show_placeholder(self) -> None:
        self.detail_stack.setCurrentWidget(self.attempt_page)
        self.detail_title.setText("Choose an application")
        self.detail_company.setText("Details, evidence, and your notes appear here.")
        self.monogram.set_text("")
        self.status_label.hide()
        self.meta_label.clear()
        self.next_step.hide()
        self.questions_label.hide()
        for widget in (self.answer_button, self.posting_button, self.resume_button, self.continue_button,
                       self.reopen_button):
            widget.hide()

    def _set_detail_enabled(self, enabled: bool) -> None:
        for widget in (self.note, self.answer_button, self.posting_button, self.resume_button, self.continue_button, self.save_note):
            widget.setEnabled(enabled)

    def _select_row(self, row: int) -> None:
        item = self.list.item(row) if row >= 0 else None
        record = item.data(RECORD_ROLE) if item else None
        if not record:
            self.selected_application = None
            self._set_detail_enabled(False)
            self._show_placeholder()
            return
        if record["kind"] == "draft":
            if self._note_dirty:
                self._save_note()
            current = self.draft_detail.draft
            same = current is not None and current.get("id") == record.get("id")
            if current is not None and not same and self.draft_detail.dirty():
                self.save_draft_requested.emit(int(current["id"]), self.draft_detail.edits())
            self.selected_application = None
            if not (same and self.draft_detail.dirty() and current.get("status") == record.get("status")):
                self.draft_detail.set_draft(record)
            self.detail_stack.setCurrentWidget(self.draft_detail)
            return
        current = self.draft_detail.draft
        if current is not None and self.draft_detail.dirty():
            self.save_draft_requested.emit(int(current["id"]), self.draft_detail.edits())
            self.draft_detail.draft = None
        self.detail_stack.setCurrentWidget(self.attempt_page)
        self._select_application(record)

    def _select_application(self, application: dict) -> None:
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
        if application.get("method") == "manual":
            time_parts.append("Sent by you outside Starployment")
        else:
            time_parts.append("Submission attempted" if application.get("submission_attempted") else "Submission not attempted")
        if application.get("posted_at"):
            time_parts.append(f"Posted {local_time(application['posted_at'])}")
        if application.get("stage") and application.get("stage") != "applied":
            time_parts.append(f"Stage: {status_label(application['stage'])}")
        self.meta_label.setText("  ·  ".join(time_parts))
        rejected = bool(re.search(r"spam filter rejected|flagged as (?:possible )?spam", str(application.get("error") or ""), re.I))
        self.reopen_button.setVisible(rejected and bool(application.get("submission_attempted")))
        if status == "applied" and application.get("method") == "manual":
            text = "You marked this application as sent. It is in the Tracker."
        elif status == "applied":
            text = "Employer confirmation recorded. Your application is complete."
        elif rejected and application.get("submission_attempted"):
            text = ("The employer's spam filter refused this submission, so it was not received. "
                    "Choose Prepare again to answer any questions and send it.")
        elif application.get("submission_attempted"):
            text = "The result is unconfirmed. Check the employer site before any retry."
        elif status == "dry_run":
            text = "Practice run complete. Review the resume and unanswered fields, then choose Apply when ready."
        elif self._questions:
            text = "Review the unanswered questions below. Required answers must be saved before submission."
        elif application.get("error"):
            text = "Next step · " + str(application["error"])
        else:
            text = "No unanswered questions are recorded."
        self.next_step.setText(text)
        self.next_step.show()
        if self._questions:
            self.questions_label.setText("Unanswered questions\n" + "\n".join(f"•  {q}" for q in self._questions))
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
        self.continue_button.setText("Apply" if status == "dry_run" else "Try again")
        self.continue_button.setVisible(application.get("id") in self._continuable_ids)
        self._set_detail_enabled(True)

    # --------------------------------------------------------------- actions
    def _note_edited(self) -> None:
        if self._loading_note or self.selected_application is None:
            return
        self._note_dirty = True
        self.note_status.setText("Editing…")
        self._autosave.start()

    def _answer_questions(self) -> None:
        if self._questions and self.selected_application and self.selected_application.get("job_id"):
            self.prepare_requested.emit(str(self.selected_application["job_id"]))
        elif self._questions:
            self.answer_questions_requested.emit(self._questions)

    def _open_posting(self) -> None:
        if self.selected_application and self.selected_application.get("url"):
            self.open_requested.emit(str(self.selected_application["url"]))

    def _open_resume(self) -> None:
        if self.selected_application and self.selected_application.get("resume_path"):
            self.resume_requested.emit(str(self.selected_application["resume_path"]))

    def _continue_application(self) -> None:
        if self.selected_application and self.selected_application.get("id") in self._continuable_ids:
            self._save_note()
            self.apply_requested.emit(str(self.selected_application["job_id"]))

    def _save_note(self) -> None:
        self._autosave.stop()
        if self.selected_application and self.selected_application.get("id") is not None:
            text = self.note.toPlainText()
            self.selected_application["candidate_note"] = text
            self._note_dirty = False
            self.note_status.setText("Saved")
            self.note_save_requested.emit(int(self.selected_application["id"]), text)

    def _confirm_remove(self, draft_id: int) -> None:
        self.remove_draft_requested.emit(draft_id)
        self.draft_detail.draft = None

    def _context_menu(self, position) -> None:
        item = self.list.itemAt(position)
        record = item.data(RECORD_ROLE) if item else None
        if not record:
            return
        self.list.setCurrentItem(item)
        menu = QMenu(self)
        job_id = str(record.get("job_id") or "")
        status = str(record.get("status") or "")
        busy = status in ("sending", "queued", "running")
        starred = bool(record.get("_starred"))
        if record.get("url"):
            menu.addAction("Open posting", lambda: self.open_requested.emit(str(record["url"])))
        if job_id:
            menu.addAction("Remove star" if starred else "Star", lambda: self.star_requested.emit(job_id, not starred))
        sent = record["kind"] == "attempt" and status == "applied"
        if job_id and not sent and not busy:
            menu.addAction("Mark as sent…", lambda: self._confirm_mark_sent(record))
        if not busy:
            menu.addSeparator()
            if record["kind"] == "draft":
                menu.addAction("Remove", lambda: self._confirm_remove(int(record["id"])))
                menu.addAction("Remove and skip this job", lambda: self._discard(int(record["id"])))
            else:
                menu.addAction("Remove from list", lambda: self._confirm_hide(record))
        menu.exec(self.list.viewport().mapToGlobal(position))

    def _confirm_mark_sent(self, record: dict) -> None:
        subject = f"{record.get('company') or 'Employer'} · {record.get('title') or 'Role'}"
        answer = QMessageBox.question(
            self.window(), "Mark as sent",
            f"Mark {subject} as sent?\n\nUse this when you applied on the employer's site yourself. "
            "It moves to the Tracker as Applied, and Starployment will not send it again.")
        if answer == QMessageBox.StandardButton.Yes:
            if record.get("kind") == "draft":
                self.draft_detail.draft = None
            self.mark_sent_requested.emit(str(record["job_id"]))

    def _confirm_hide(self, record: dict) -> None:
        sent = record.get("status") == "applied" or record.get("submission_attempted")
        detail = ("It stays in the Tracker and in your history." if sent else
                  "The job stays in Jobs, and its history is kept.")
        answer = QMessageBox.question(self.window(), "Remove from Applications",
                                      f"Remove {record.get('company') or 'this application'} from this list?\n\n{detail}")
        if answer == QMessageBox.StandardButton.Yes:
            self.hide_requested.emit(int(record["id"]))

    def _discard(self, draft_id: int) -> None:
        self.discard_draft_requested.emit(draft_id)
        self.draft_detail.draft = None

    def _confirm_reset(self) -> None:
        unsent = sum(1 for d in self.drafts if d.get("status") in OPEN_DRAFT - {"sending"})
        box = QMessageBox(self.window())
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Reset unfinished applications")
        box.setText(f"Remove {plural(unsent, 'prepared application')} that {'was' if unsent == 1 else 'were'} not sent?")
        box.setInformativeText("Their jobs return to New so you can prepare them again with your current resume "
                               "and answers. Anything that was submitted is always kept.")
        attempts = QCheckBox("Also clear practice runs and attempts that stopped before Submit")
        attempts.setChecked(True)
        box.setCheckBox(attempts)
        box.setStandardButtons(QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Reset)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() == QMessageBox.StandardButton.Reset:
            self.draft_detail.draft = None
            self.reset_requested.emit(attempts.isChecked())
