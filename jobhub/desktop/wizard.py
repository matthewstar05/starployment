"""First-run setup and candidate facts collection."""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .icons import icon_pixmap
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
    ("Resume", "Start with your resume", "JobHub reads it on this computer and fills in what it can."),
    ("About you", "Confirm your details", "Employers see these facts. Correct anything the resume parser missed."),
    ("Targeting", "Choose what to look for", "Titles, places, and sources decide which roles JobHub ranks for you."),
    ("Answers", "Answer common questions", "Only answers you enter are used. Unknown facts stay blank and pause an application."),
)


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
        self.setWindowTitle("Set up JobHub")
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
        for page in (self.resume, self.personal, self.targeting, self.answers):
            self.tabs.addWidget(_scroll(page))
        self._build_personal()
        self._build_resume()
        self._build_targeting()
        self._build_answers()

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
            "Choose a text-based PDF. Review what JobHub extracts on the next step.", "subtitle"))
        lay.addWidget(label(
            "Next: confirm your details, choose target roles, then add application answers.", "caption"))
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
        more = FormGroup("More about you", "Custom facts: one per line, as Fact name | value.")
        more.add_row("Summary", self._text_field("summary"))
        self.custom_facts = QTextEdit()
        self.custom_facts.setAcceptRichText(False)
        self.custom_facts.setFixedHeight(86)
        self.custom_facts.setPlaceholderText("Optional facts, one per line: Fact name | value")
        self.custom_facts.setPlainText(
            "\n".join(f"{k} | {v}" for k, v in (self.profile.get("custom_facts") or {}).items())
        )
        more.add_row("Custom facts", self.custom_facts)
        lay.addWidget(more)
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

        def lines(values, height: int) -> QPlainTextEdit:
            edit = QPlainTextEdit("\n".join(values))
            edit.setFixedHeight(height)
            return edit

        self.target_titles = lines(s.get("target_titles", []), 88)
        self.target_titles.setPlaceholderText("One title per line, e.g. Software Engineer")
        self.locations = lines(s.get("preferred_locations", []), 88)
        self.locations.setPlaceholderText("One place per line, e.g. San Francisco, CA")
        self.exclude_words = lines(s.get("exclude_title_words", []), 70)
        self.exclude_words.setPlaceholderText("Words that rule a title out, e.g. Senior")
        self.remote_ok = ToggleSwitch("Include remote roles")
        self.remote_ok.setChecked(bool(s.get("remote_ok", True)))
        self.include_internships = ToggleSwitch("Include internships and co-ops")
        self.include_internships.setChecked(bool(s.get("include_internships", False)))
        self.auto_apply = ToggleSwitch("Allow unattended applications when eligible")
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
        jobspy = s.get("jobspy", {})
        self.jobspy_terms = lines(jobspy.get("search_terms", []), 76)
        self.jobspy_location = QLineEdit(str(jobspy.get("location", "")))
        self.jobspy_location.setPlaceholderText("Blank rotates through your preferred locations")
        self.jobspy_country = QLineEdit(str(jobspy.get("country_indeed", "usa")))
        companies = s.get("companies", {})
        self.greenhouse_companies = lines(companies.get("greenhouse", []), 70)
        self.lever_companies = lines(companies.get("lever", []), 70)
        self.ashby_companies = lines(companies.get("ashby", []), 70)

        roles = FormGroup("Roles")
        roles.add_row("Target titles", self.target_titles)
        roles.add_row("Preferred locations", self.locations)
        roles.add_row("Exclude title words", self.exclude_words)
        roles.add_widget(self.remote_ok)
        roles.add_widget(self.include_internships)
        lay.addWidget(roles)
        experience = FormGroup("Experience and pay")
        experience.add_row("Minimum salary", Stepper(self.min_salary, width=132))
        experience.add_row("Professional experience", Stepper(self.years_experience, width=104))
        experience.add_widget(self.confirm_years_experience)
        lay.addWidget(experience)
        sources = FormGroup("Sources", "Board slugs are the company part of a public job-board URL, one per line.")
        sources.add_row("JobSpy search terms", self.jobspy_terms)
        sources.add_row("JobSpy location", self.jobspy_location)
        sources.add_row("Indeed country", self.jobspy_country)
        sources.add_row("Greenhouse boards", self.greenhouse_companies)
        sources.add_row("Lever boards", self.lever_companies)
        sources.add_row("Ashby boards", self.ashby_companies)
        lay.addWidget(sources)
        autonomy = FormGroup("Autonomy", "Off by default. You can change this any time in Settings.")
        autonomy.add_widget(self.auto_apply)
        lay.addWidget(autonomy)
        lay.addStretch()

    def _build_answers(self) -> None:
        a = self.settings.get("answers", {})
        lay = _page_layout(self.answers)
        self.pending_hint = label(
            "These questions came from an application. Fill only answers you know; blank lines are ignored.",
            "questions",
        )
        self.pending_hint.hide()
        lay.addWidget(self.pending_hint)
        common = FormGroup("Common questions", "Leave anything you are unsure of blank.")
        self.answer_fields: dict[str, QLineEdit] = {}
        for key, title, placeholder in [
            ("work_authorized_us", "Authorized to work in the US", "Yes or No"),
            ("requires_sponsorship", "Requires sponsorship", "Yes or No"),
            ("us_citizen_or_permanent_resident", "US citizen or permanent resident", "Yes or No"),
            ("salary_expectation", "Salary expectation", "e.g. $120,000"),
            ("available_start", "Available start date", "e.g. June 2026"),
            ("how_heard", "How did you hear about us?", "e.g. Company website"),
            ("willing_to_relocate", "Willing to relocate", "Yes or No"),
            ("preferred_name", "Preferred name", ""),
            ("pronouns", "Pronouns", ""),
        ]:
            edit = QLineEdit(str(a.get(key, "")))
            edit.setPlaceholderText(placeholder)
            edit.setAccessibleName(title)
            self.answer_fields[key] = edit
            common.add_row(title, edit)
        lay.addWidget(common)
        bank = FormGroup(
            "Custom question bank",
            "One per line: Question | Answer. Use true or false for checkboxes and \\n for longer answers.",
        )
        self.custom_answers = QTextEdit()
        self.custom_answers.setAcceptRichText(False)
        self.custom_answers.setMinimumHeight(190)
        self.custom_answers.setPlaceholderText("Why do you want to work here? | …")
        self.custom_answers.setPlainText(
            "\n".join(
                f"{k} | {str(v).replace(chr(10), r'\n')}"
                for k, v in a.get("field_answers", {}).items()
            )
        )
        bank.add_widget(self.custom_answers)
        lay.addWidget(bank)
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
        self.resume_note.setText("Ready. Review what JobHub found in About you.")
        self.resume_requested.emit(path)
        self.resume_selected.emit(path)
        self.tabs.setCurrentIndex(1)

    def add_pending_questions(self, questions: list[str]) -> None:
        """Place unanswered employer prompts into the candidate's answer bank."""
        lines = self.custom_answers.toPlainText().splitlines()
        known = {line.split("|", 1)[0].strip().casefold() for line in lines if "|" in line}
        added = 0
        for question in questions:
            text = re.sub(r"\s*\(required\)\s*$", "", str(question), flags=re.I)
            text = " ".join(text.split()).strip()
            if not text or text.casefold() in known or "|" in text:
                continue
            lines.append(f"{text} | ")
            known.add(text.casefold())
            added += 1
        if added:
            self.custom_answers.setPlainText("\n".join(lines))
            self.pending_hint.show()

    @staticmethod
    def _lines(edit: QPlainTextEdit) -> list[str]:
        return [x.strip() for x in edit.toPlainText().splitlines() if x.strip()]

    def _submit(self) -> None:
        profile = {key: edit.text().strip() for key, edit in self.fields.items()}
        profile["summary"] = self._summary_edit.toPlainText().strip()
        profile["resume_path"] = self.resume_path.text().strip()
        facts = {}
        for line in self.custom_facts.toPlainText().splitlines():
            if "|" in line:
                k, v = line.split("|", 1)
                if k.strip():
                    facts[k.strip()] = v.strip()
        profile["custom_facts"] = facts
        custom = {}
        for line in self.custom_answers.toPlainText().splitlines():
            if "|" in line:
                k, v = line.split("|", 1)
                if k.strip() and v.strip():
                    answer = v.strip()
                    custom[k.strip()] = (
                        answer.casefold() == "true"
                        if answer.casefold() in {"true", "false"}
                        else answer.replace(r"\n", "\n")
                    )
        answers = {key: edit.text().strip() for key, edit in self.answer_fields.items()}
        answers["years_experience"] = (
            str(self.years_experience.value()) if self.confirm_years_experience.isChecked() else ""
        )
        answers["field_answers"] = custom
        answers["confirmed_fields"] = [key for key, edit in self.answer_fields.items() if edit.text().strip()]
        if self.confirm_years_experience.isChecked():
            answers["confirmed_fields"].append("years_experience")
        settings = {
            "setup_complete": True,
            "auto_apply": self.auto_apply.isChecked(),
            "target_titles": self._lines(self.target_titles),
            "preferred_locations": self._lines(self.locations),
            "exclude_title_words": self._lines(self.exclude_words),
            "remote_ok": self.remote_ok.isChecked(),
            "include_internships": self.include_internships.isChecked(),
            "min_salary": self.min_salary.value(),
            "years_experience": float(self.years_experience.value()),
            "jobspy": {
                "search_terms": self._lines(self.jobspy_terms),
                "location": self.jobspy_location.text().strip(),
                "country_indeed": self.jobspy_country.text().strip(),
            },
            "companies": {
                "greenhouse": self._lines(self.greenhouse_companies),
                "lever": self._lines(self.lever_companies),
                "ashby": self._lines(self.ashby_companies),
            },
            "answers": answers,
        }
        self.completed.emit({"profile": profile, "settings": settings, "resume_path": profile["resume_path"]})
