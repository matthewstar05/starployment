"""First-run setup and candidate facts collection."""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .editors import SUGGESTED_FACTS, SUGGESTED_QUESTIONS, KeyValueEditor, TagEditor, yes_no_control
from .icons import icon_pixmap
from .native_glass import prepare_window
from .theme import current_colors
from .widgets import (
    FadeStack,
    FormGroup,
    Motion,
    Stepper,
    ToggleSwitch,
    _animate,
    button,
    label,
    paint_backdrop,
    qcolor,
    reduced_motion,
    rounded_path,
    ui_font,
    with_alpha,
)

STEPS = (
    ("Resume", "Start with your resume", "Starployment reads it on this computer and fills in what it can. "
                                        "Your jobs, projects, and skills become your resume builder."),
    ("About you", "Confirm your details", "Employers see these facts. Correct anything the resume reader missed."),
    ("Targeting", "Choose what to look for", "Type a title or place and press Return. Starployment ranks every role "
                                              "it finds against these."),
    ("Answers", "Answer common questions", "Only answers you set are used. Anything left unset pauses an application "
                                            "and asks you."),
    ("Connect", "Connect your accounts", "Optional, and you can do it later in Settings."),
)
TITLE_SUGGESTIONS = ["Software Engineer", "Software Developer", "Backend Engineer", "Frontend Engineer",
                     "Full Stack Engineer", "Data Scientist", "Data Analyst", "Data Engineer", "Machine Learning Engineer",
                     "Quantitative Developer", "Quantitative Researcher", "Research Engineer", "Product Manager",
                     "New Grad", "Entry Level"]
PLACE_SUGGESTIONS = ["Remote", "San Francisco", "New York", "Seattle", "Los Angeles", "Chicago", "Boston", "Austin",
                     "Denver", "Washington, DC", "Atlanta", "San Diego", "Toronto", "London"]


def _scroll(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setObjectName("formScroll")
    widget.setObjectName("formPage")
    area.setWidgetResizable(True)
    area.setWidget(widget)
    area.viewport().setObjectName("formViewport")
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    return area


def _page_layout(page: QWidget) -> QVBoxLayout:
    layout = QVBoxLayout(page)
    layout.setContentsMargins(2, 4, 10, 18)
    layout.setSpacing(20)
    return layout


class StepIndicator(QWidget):
    """Numbered steps joined by a progress line that fills as you advance."""

    step_clicked = Signal(int)

    def __init__(self, titles: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self._titles = titles
        self._current = 0
        self._progress = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.valueChanged.connect(self._set_progress)
        self.setFixedHeight(54)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _set_progress(self, value) -> None:
        self._progress = float(value)
        self.update()

    def set_current(self, index: int) -> None:
        self._current = index
        if reduced_motion() or not self.isVisible():
            self._set_progress(float(index))
        else:
            _animate(self._anim, self._progress, float(index), Motion.SLOW)

    def _centers(self) -> list[QPointF]:
        count = len(self._titles)
        span = self.width() / count
        return [QPointF(span * (i + 0.5), 15) for i in range(count)]

    def mousePressEvent(self, event) -> None:
        span = self.width() / len(self._titles)
        index = int(event.position().x() // span)
        if 0 <= index < len(self._titles):
            self.step_clicked.emit(index)

    def paintEvent(self, event) -> None:
        colors = current_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        centers = self._centers()
        radius = 11.0
        track = QPen(qcolor(colors["fill_quiet"]), 3)
        track.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(track)
        painter.drawLine(centers[0], centers[-1])
        if len(centers) > 1:
            filled_x = centers[0].x() + (centers[-1].x() - centers[0].x()) * (self._progress / (len(centers) - 1))
            fill = QPen(qcolor(colors["accent"]), 3)
            fill.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(fill)
            painter.drawLine(centers[0], QPointF(filled_x, centers[0].y()))
        for index, (center, title) in enumerate(zip(centers, self._titles)):
            circle = QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2)
            done = index < self._current
            current = index == self._current
            painter.setPen(Qt.PenStyle.NoPen)
            if done or current:
                if current:
                    painter.setBrush(with_alpha(colors["accent"], 0.18))
                    painter.drawEllipse(circle.adjusted(-4, -4, 4, 4))
                painter.setBrush(qcolor(colors["accent"]))
                painter.drawEllipse(circle)
            else:
                painter.setBrush(qcolor(colors["surface"]))
                painter.drawEllipse(circle)
                painter.setPen(QPen(qcolor(colors["border"]), 1.2))
                painter.drawEllipse(circle.adjusted(0.6, 0.6, -0.6, -0.6))
            if done:
                painter.drawPixmap(QPointF(center.x() - 7, center.y() - 7), icon_pixmap("check", "#FFFFFF", 14, 2.6))
            else:
                painter.setPen(qcolor("#FFFFFF" if current else colors["text_muted"]))
                painter.setFont(ui_font(11, QFont.Weight.DemiBold))
                painter.drawText(circle, Qt.AlignmentFlag.AlignCenter, str(index + 1))
            font = ui_font(11, QFont.Weight.DemiBold if current else QFont.Weight.Medium)
            painter.setFont(font)
            painter.setPen(qcolor(colors["text"] if current or done else colors["text_muted"]))
            width = self.width() / len(self._titles)
            text = QFontMetrics(font).elidedText(title, Qt.TextElideMode.ElideRight, int(width - 6))
            painter.drawText(QRectF(center.x() - width / 2, 32, width, 18), Qt.AlignmentFlag.AlignCenter, text)
        painter.end()


class ResumeDropZone(QFrame):
    """A dashed drop target for a resume PDF; also opens a file dialog on click."""

    file_chosen = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(190)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hover = False
        column = QVBoxLayout(self)
        column.setContentsMargins(24, 26, 24, 22)
        column.setSpacing(6)
        column.addStretch(1)
        self.icon = QWidget()
        self.icon.setFixedSize(44, 44)
        column.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(4)
        self.title = label("No PDF selected", "sectionTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.title)
        self.note = label("Drop a PDF here, or choose one from this computer.", "dropHint")
        self.note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.note)
        self.choose = button("Choose PDF…", "secondary")
        column.addSpacing(8)
        column.addWidget(self.choose, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addStretch(1)

    @staticmethod
    def _pdf_from(event) -> str | None:
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        paths = [url.toLocalFile() for url in urls if url.isLocalFile()]
        return next((path for path in paths if path.lower().endswith(".pdf")), None)

    def dragEnterEvent(self, event) -> None:
        if self._pdf_from(event):
            event.acceptProposedAction()
            self._hover = True
            self.update()

    def dragLeaveEvent(self, event) -> None:
        self._hover = False
        self.update()

    def dropEvent(self, event) -> None:
        self._hover = False
        self.update()
        path = self._pdf_from(event)
        if path:
            event.acceptProposedAction()
            self.file_chosen.emit(path)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.choose.click()

    def paintEvent(self, event) -> None:
        colors = current_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        fill = with_alpha(colors["accent"], 0.10) if self._hover else qcolor(colors["glass_bottom"])
        painter.fillPath(rounded_path(rect, 18), fill)
        pen = QPen(qcolor(colors["accent"] if self._hover else colors["border"]), 1.5, Qt.PenStyle.DashLine)
        pen.setDashPattern([5, 4])
        painter.setPen(pen)
        painter.drawPath(rounded_path(rect, 18))
        icon_rect = self.icon.geometry()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(qcolor(colors["accent_soft"]))
        painter.drawEllipse(QRectF(icon_rect))
        painter.drawPixmap(QPointF(icon_rect.center().x() - 11 + 1, icon_rect.center().y() - 11 + 1),
                           icon_pixmap("file-up", colors["accent_text"], 22, 1.9))
        painter.end()


class SetupWizard(QDialog):
    """First-run setup and candidate facts collection.

    ``completed`` emits a JSON-friendly dict with profile, settings, and
    resume_path keys. Resume parsing remains the controller's job.
    """

    completed = Signal(dict)
    resume_requested = Signal(str)
    # Backward-compatible alias for early controller prototypes.
    resume_selected = Signal(str)

    def __init__(
        self,
        profile: dict | None = None,
        settings: dict | None = None,
        parent: QWidget | None = None,
        pending_questions: list[str] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Set up Starployment")
        prepare_window(self)
        self.resize(860, 700)
        self.setMinimumSize(720, 560)
        self.profile = profile or {}
        self.settings = settings or {}
        root = QVBoxLayout(self)
        root.setContentsMargins(34, 26, 34, 22)
        root.setSpacing(14)
        self.steps = StepIndicator([title for title, _, _ in STEPS])
        root.addWidget(self.steps)
        heading = QVBoxLayout()
        heading.setSpacing(3)
        self.step_indicator = label("", "eyebrow")
        heading.addWidget(self.step_indicator)
        self.title = label(STEPS[0][1], "pageTitle")
        heading.addWidget(self.title)
        self.subtitle = label(STEPS[0][2], "subtitle")
        heading.addWidget(self.subtitle)
        root.addLayout(heading)

        self.tabs = FadeStack(mode="push", distance=36)
        root.addWidget(self.tabs, 1)
        self.resume = QWidget()
        self.personal = QWidget()
        self.targeting = QWidget()
        self.answers = QWidget()
        self.connect_page = QWidget()
        for page in (self.resume, self.personal, self.targeting, self.answers, self.connect_page):
            self.tabs.addWidget(_scroll(page))
        self._build_personal()
        self._build_resume()
        self._build_targeting()
        self._build_answers()
        self._build_connect()

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.back_button = button("Back", "secondary", icon="chevron-left")
        self.back_button.clicked.connect(lambda: self._go(self.tabs.currentIndex() - 1))
        actions.addWidget(self.back_button)
        actions.addStretch()
        cancel = button("Cancel", "quiet")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self.next_button = button("Continue", icon="chevron-right", trailing=True)
        self.next_button.setDefault(True)
        self.next_button.clicked.connect(lambda: self._go(self.tabs.currentIndex() + 1))
        actions.addWidget(self.next_button)
        self.save_button = button("Save setup", icon="check")
        self.save_button.clicked.connect(self._submit)
        actions.addWidget(self.save_button)
        root.addLayout(actions)
        self.tabs.currentChanged.connect(self._update_step)
        self.steps.step_clicked.connect(self._go)
        if pending_questions:
            self.add_pending_questions(pending_questions)
            self.tabs.setCurrentIndex(3)
        elif self.profile.get("resume_path"):
            self.tabs.setCurrentIndex(1)
        self._update_step(self.tabs.currentIndex())

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        paint_backdrop(painter, self)
        painter.end()

    def _go(self, index: int) -> None:
        self.tabs.setCurrentIndex(max(0, min(self.tabs.count() - 1, index)))

    def _update_step(self, index: int) -> None:
        _, title, subtitle = STEPS[index]
        self.step_indicator.setText(f"STEP {index + 1} OF {self.tabs.count()}")
        self.title.setText(title)
        self.subtitle.setText(subtitle)
        self.steps.set_current(index)
        self.back_button.setVisible(index > 0)
        self.next_button.setVisible(index < self.tabs.count() - 1)
        self.save_button.setVisible(index == self.tabs.count() - 1)
        (self.save_button if index == self.tabs.count() - 1 else self.next_button).setDefault(True)

    # ----------------------------------------------------------------- pages
    def _build_resume(self) -> None:
        lay = _page_layout(self.resume)
        self.resume_path = QLineEdit(str(self.profile.get("resume_path", "")))
        self.resume_path.setReadOnly(True)
        self.resume_path.hide()
        lay.addWidget(self.resume_path)
        self.drop_zone = ResumeDropZone()
        self.drop_zone.choose.clicked.connect(self._choose_resume)
        self.drop_zone.file_chosen.connect(self._load_resume)
        filename = Path(self.resume_path.text()).name if self.resume_path.text() else "No PDF selected"
        self.resume_file_label = self.drop_zone.title
        self.resume_file_label.setText(filename)
        self.resume_note = self.drop_zone.note
        if self.resume_path.text():
            self.resume_note.setText("Saved resume. Choose another PDF to replace it.")
        lay.addWidget(self.drop_zone)
        lay.addWidget(label(
            "Choose a text-based PDF. Review what Starployment extracts on the next step.", "subtitle"))
        lay.addWidget(label(
            "Next: confirm your details, choose what to look for, add common answers, and optionally connect Luna "
            "and your inbox.", "caption"))
        lay.addStretch()

    def _build_personal(self) -> None:
        lay = _page_layout(self.personal)
        self.fields: dict[str, QLineEdit] = {}
        groups = (
            ("Contact", (("first_name", "First name"), ("last_name", "Last name"),
                         ("email", "Email"), ("phone", "Phone"))),
            ("Location", (("address_line1", "Address"), ("address_line2", "Address line 2"),
                          ("city", "City"), ("state", "State / region"), ("region", "Region / province"),
                          ("postal_code", "Postal code"), ("country", "Country"))),
            ("Online", (("linkedin", "LinkedIn URL"), ("github", "GitHub URL"),
                        ("website", "Personal website"), ("headline", "Professional headline"))),
        )
        for title, spec in groups:
            group = FormGroup(title)
            for key, name in spec:
                edit = QLineEdit(str(self.profile.get(key, "")))
                edit.setPlaceholderText(name)
                edit.setAccessibleName(name)
                self.fields[key] = edit
                group.add_row(name, edit)
            lay.addWidget(group)
        more = FormGroup("More about you")
        more.add_row("Summary", self._text_field("summary"))
        lay.addWidget(more)
        facts = FormGroup("Other facts", "Anything an application might ask that is not above. Pick a label or "
                                         "type your own, then fill in the value.")
        self.custom_facts = KeyValueEditor(suggestions=SUGGESTED_FACTS, key_placeholder="Label",
                                           value_placeholder="Your answer", add_text="Add a fact")
        self.custom_facts.set_values(dict(self.profile.get("custom_facts") or {}))
        facts.add_widget(self.custom_facts)
        lay.addWidget(facts)
        lay.addStretch()

    def _text_field(self, key: str, rows: int = 3) -> QTextEdit:
        edit = QTextEdit(str(self.profile.get(key, "")))
        edit.setAcceptRichText(False)
        edit.setFixedHeight(78 if rows <= 3 else 130)
        setattr(self, f"_{key}_edit", edit)
        return edit

    def _build_targeting(self) -> None:
        s = self.settings
        lay = _page_layout(self.targeting)
        self.target_titles = TagEditor("Add a title", TITLE_SUGGESTIONS)
        self.target_titles.set_values(list(s.get("target_titles") or []))
        self.locations = TagEditor("Add a place", PLACE_SUGGESTIONS)
        self.locations.set_values(list(s.get("preferred_locations") or []))
        self.exclude_words = TagEditor("Add a word")
        self.exclude_words.set_values(list(s.get("exclude_title_words") or []))
        self.remote_ok = ToggleSwitch("Include remote roles")
        self.remote_ok.setChecked(bool(s.get("remote_ok", True)))
        self.include_internships = ToggleSwitch("Include internships and co-ops")
        self.include_internships.setChecked(bool(s.get("include_internships", False)))
        self.auto_apply = ToggleSwitch("Also apply automatically to eligible matches, without review")
        self.auto_apply.setChecked(bool(s.get("auto_apply", False)))
        self.min_salary = QSpinBox()
        self.min_salary.setRange(0, 10_000_000)
        self.min_salary.setSingleStep(5_000)
        self.min_salary.setValue(int(s.get("min_salary", 0)))
        self.min_salary.setPrefix("$ ")
        self.min_salary.setGroupSeparatorShown(True)
        self.years_experience = QSpinBox()
        self.years_experience.setRange(0, 80)
        self.years_experience.setValue(int(s.get("years_experience", 0)))
        self.years_experience.setSuffix(" years")
        self.confirm_years_experience = ToggleSwitch("Use this number to answer application questions")
        saved_answers = s.get("answers", {})
        self.confirm_years_experience.setChecked(
            "years_experience" in saved_answers.get("confirmed_fields", [])
        )
        roles = FormGroup("Roles")
        roles.add_row("Job titles", self.target_titles)
        roles.add_row("Places", self.locations)
        roles.add_row("Leave out titles with", self.exclude_words)
        roles.add_widget(self.remote_ok)
        roles.add_widget(self.include_internships)
        lay.addWidget(roles)
        experience = FormGroup("Experience and pay")
        experience.add_row("Minimum salary", Stepper(self.min_salary, width=132))
        experience.add_row("Professional experience", Stepper(self.years_experience, width=104))
        experience.add_widget(self.confirm_years_experience)
        lay.addWidget(experience)
        lay.addWidget(label("Starployment scans dozens of public Greenhouse, Lever, and Ashby company boards plus "
                            "major job sites. Edit the list any time in Settings → Company job boards.", "caption"))
        autonomy = FormGroup("Autonomy", "Off by default. With it off, press Apply on Overview to prepare "
                                         "applications and approve each one before it is sent.")
        autonomy.add_widget(self.auto_apply)
        lay.addWidget(autonomy)
        lay.addStretch()

    def _build_answers(self) -> None:
        a = self.settings.get("answers", {})
        lay = _page_layout(self.answers)
        self.pending_hint = label(
            "These questions came from an application. Answer only what you know; blank answers are ignored.",
            "questions",
        )
        self.pending_hint.hide()
        lay.addWidget(self.pending_hint)
        eligibility = FormGroup("Eligibility", "Leave “Not set” if you are unsure.")
        self.yes_no = {}
        for key, title in (("work_authorized_us", "Authorized to work in the US"),
                           ("requires_sponsorship", "Needs visa sponsorship"),
                           ("us_citizen_or_permanent_resident", "US citizen or permanent resident"),
                           ("willing_to_relocate", "Willing to relocate")):
            control = yes_no_control()
            control.setAccessibleName(title)
            control.setCurrentIndex(max(0, control.findData(str(a.get(key) or ""))))
            self.yes_no[key] = control
            eligibility.add_row(title, control)
        lay.addWidget(eligibility)
        common = FormGroup("Common questions", "Leave anything you are unsure of blank.")
        self.answer_fields: dict[str, QLineEdit | QComboBox] = {}
        for key, title, placeholder, suggestions in [
            ("salary_expectation", "Salary expectation", "e.g. $120,000 or Negotiable", ()),
            ("available_start", "Available start date", "e.g. June 2026", ()),
            ("how_heard", "How did you hear about us?", "Choose or type",
             ("Company website", "LinkedIn", "Job board", "Referral", "University career site")),
            ("preferred_name", "Preferred name", "Only if different from your first name", ()),
            ("pronouns", "Pronouns", "Optional", ("she/her", "he/him", "they/them")),
        ]:
            if suggestions:
                edit = QComboBox()
                edit.setEditable(True)
                edit.addItems(["", *suggestions])
                edit.setCurrentText(str(a.get(key, "")))
                edit.lineEdit().setPlaceholderText(placeholder)
            else:
                edit = QLineEdit(str(a.get(key, "")))
                edit.setPlaceholderText(placeholder)
            edit.setAccessibleName(title)
            self.answer_fields[key] = edit
            common.add_row(title, edit)
        lay.addWidget(common)
        bank = FormGroup("Question bank", "Questions employers ask word for word, with your answers. Pick a "
                                          "common question or type your own.")
        self.custom_answers = KeyValueEditor(suggestions=SUGGESTED_QUESTIONS, multiline=True,
                                             key_placeholder="Question", value_placeholder="Your answer",
                                             add_text="Add a question")
        self.custom_answers.set_values(dict(a.get("field_answers") or {}))
        bank.add_widget(self.custom_answers)
        lay.addWidget(bank)
        lay.addStretch()

    def _build_connect(self) -> None:
        from .views.connections import EmailConnection, LunaConnection
        lay = _page_layout(self.connect_page)
        self.luna = LunaConnection(title="Luna (optional)", compact=True)
        self.luna.set_values(self.settings)
        self.email = EmailConnection(compact=True)
        self.email.set_values(self.settings)
        lay.addWidget(self.luna)
        lay.addWidget(self.email)
        lay.addStretch()

    # ------------------------------------------------------------- behavior
    def _choose_resume(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose resume", str(Path.home()), "PDF files (*.pdf)")
        if path:
            self._load_resume(path)

    def _load_resume(self, path: str) -> None:
        try:
            self._preview_resume(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Could not read resume", str(exc))

    def _preview_resume(self, path: str) -> None:
        """Fill only empty visible fields from a local PDF before it is saved."""
        from ..profile import apply_links, extract_links, extract_text, heuristic_profile

        text = extract_text(path)
        if not text.strip():
            raise ValueError("No readable text found in this PDF. Choose a text-based resume PDF.")
        preview = apply_links(heuristic_profile(text), extract_links(path))
        for key, edit in self.fields.items():
            parsed = getattr(preview, key, "")
            if not edit.text().strip() and isinstance(parsed, str) and parsed.strip():
                edit.setText(parsed.strip())
        if not self._summary_edit.toPlainText().strip() and preview.summary.strip():
            self._summary_edit.setPlainText(preview.summary.strip())
        self.resume_path.setText(path)
        self.resume_file_label.setText(Path(path).name)
        self.resume_note.setText("Ready. Review what Starployment found in About you.")
        self.resume_requested.emit(path)
        self.resume_selected.emit(path)
        self.tabs.setCurrentIndex(1)

    def add_pending_questions(self, questions: list[str]) -> None:
        """Place unanswered employer prompts into the candidate's answer bank."""
        known = {row.pair()[0].casefold() for row in self.custom_answers.rows}
        added = 0
        for question in questions:
            text = re.sub(r"\s*\(required\)\s*$", "", str(question), flags=re.I)
            text = " ".join(text.split()).strip()
            if not text or text.casefold() in known:
                continue
            self.custom_answers.add_row(text, "")
            known.add(text.casefold())
            added += 1
        if added:
            self.pending_hint.show()

    def _submit(self) -> None:
        profile = {key: edit.text().strip() for key, edit in self.fields.items()}
        profile["summary"] = self._summary_edit.toPlainText().strip()
        profile["resume_path"] = self.resume_path.text().strip()
        profile["custom_facts"] = self.custom_facts.values()
        custom = {}
        for question, answer in self.custom_answers.values().items():
            lowered = answer.casefold()
            custom[question] = lowered == "true" if lowered in {"true", "false"} else answer
        answers = {key: str(control.currentData() or "") for key, control in self.yes_no.items()}
        for key, edit in self.answer_fields.items():
            answers[key] = (edit.currentText() if isinstance(edit, QComboBox) else edit.text()).strip()
        answers["years_experience"] = (
            str(self.years_experience.value()) if self.confirm_years_experience.isChecked() else ""
        )
        answers["field_answers"] = custom
        answers["confirmed_fields"] = [key for key, value in answers.items()
                                       if key not in ("field_answers", "years_experience") and value]
        if self.confirm_years_experience.isChecked():
            answers["confirmed_fields"].append("years_experience")
        settings = {
            "setup_complete": True,
            "auto_apply": self.auto_apply.isChecked(),
            "target_titles": self.target_titles.values(),
            "preferred_locations": self.locations.values(),
            "exclude_title_words": self.exclude_words.values(),
            "remote_ok": self.remote_ok.isChecked(),
            "include_internships": self.include_internships.isChecked(),
            "min_salary": self.min_salary.value(),
            "years_experience": float(self.years_experience.value()),
            "answers": answers,
        }
        luna = self.luna.values()
        settings["agent"] = luna["agent"]
        settings["luna"] = luna["luna"]
        settings["resume_builder"] = luna["resume_builder"]
        settings.update(self.email.values())
        self.completed.emit({"profile": profile, "settings": settings, "resume_path": profile["resume_path"]})
