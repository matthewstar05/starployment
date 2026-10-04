"""Overview page: greeting, metric tiles, top matches, attention list, activity."""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget

from ..delegates import score_color
from ..format import activity_label, activity_tone, greeting, local_time, plural, relative_time, salary
from ..theme import current_colors
from ..native_glass import prepare_window
from ..widgets import (
    ActionRow,
    Card,
    ClickableGlass,
    CountUpLabel,
    ElidedLabel,
    FormGroup,
    IconBadge,
    PageHeader,
    SegmentedControl,
    StatusDot,
    Stepper,
    ToggleSwitch,
    button,
    label,
    paint_backdrop,
    qcolor,
    rounded_path,
    ui_font,
)


def _today() -> str:
    now = datetime.now()
    return f"{now:%A, %B} {now.day}"


class ScoreRing(QWidget):
    """Compact match-score ring for list rows."""

    def __init__(self, score: float, diameter: int = 34, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(diameter, diameter)
        self._score = score

    def paintEvent(self, event) -> None:
        colors = current_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ring = QRectF(self.rect()).adjusted(2.5, 2.5, -2.5, -2.5)
        painter.setPen(QPen(qcolor(colors["fill_quiet"]), 3.4))
        painter.drawEllipse(ring)
        pen = QPen(qcolor(score_color(self._score)), 3.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        span = int(-360 * 16 * max(0.0, min(100.0, self._score)) / 100.0)
        if span:
            painter.drawArc(ring, 90 * 16, span)
        painter.setPen(qcolor(colors["text"]))
        painter.setFont(ui_font(11, QFont.Weight.DemiBold))
        painter.drawText(QRectF(self.rect()), Qt.AlignmentFlag.AlignCenter, f"{self._score:.0f}")
        painter.end()


class MetricTile(ClickableGlass):
    """A glass tile with a tinted symbol, a counting number, and a label."""

    def __init__(self, title: str, icon: str, tone: str, parent: QWidget | None = None):
        super().__init__(parent, radius=18)
        self.setMinimumHeight(118)
        self.setAccessibleName(title)
        column = QVBoxLayout(self)
        column.setContentsMargins(16, 15, 16, 14)
        column.setSpacing(2)
        top = QHBoxLayout()
        top.addWidget(IconBadge(icon, tone, 30))
        top.addStretch(1)
        column.addLayout(top)
        column.addStretch(1)
        self.value = CountUpLabel(0)
        self.value.setObjectName("metricValue")
        column.addWidget(self.value)
        self.caption = label(title, "metricLabel")
        column.addWidget(self.caption)


def paint_star(painter: QPainter, center: QPointF, radius: float, color: QColor) -> None:
    """A four-point sparkle: the Starployment mark in miniature."""
    from PySide6.QtGui import QPainterPath
    path = QPainterPath()
    inner = radius * 0.22
    points = []
    for index in range(8):
        angle = index * math.pi / 4 - math.pi / 2
        r = radius if index % 2 == 0 else inner
        points.append(QPointF(center.x() + r * math.cos(angle), center.y() + r * math.sin(angle)))
    path.moveTo(points[0])
    for point in points[1:]:
        path.lineTo(point)
    path.closeSubpath()
    painter.fillPath(path, color)


class ApplyHero(QWidget):
    """The Overview's call to action: a night-sky card with a glowing star and the Apply button."""

    apply_clicked = Signal()
    review_clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("applyHero")
        self.setMinimumHeight(176)
        row = QHBoxLayout(self)
        row.setContentsMargins(28, 24, 28, 24)
        row.setSpacing(24)
        text = QVBoxLayout()
        text.setSpacing(6)
        self.eyebrow = label("APPLY", "heroStat")
        self.title = label("Ready when you are", "heroTitle")
        self.text = label("Prepare applications for your best matches. Starployment builds a one-page resume for "
                          "each role and fills every answer it knows; Luna drafts the custom ones. Nothing is sent "
                          "until you approve it.", "heroText")
        self.stats = label("", "heroStat")
        self.text.setMaximumWidth(640)
        text.addWidget(self.eyebrow)
        text.addWidget(self.title)
        text.addWidget(self.text)
        text.addSpacing(4)
        text.addWidget(self.stats)
        text.addStretch(1)
        row.addLayout(text, 1)
        actions = QVBoxLayout()
        actions.setSpacing(10)
        actions.addStretch(3)
        self.apply_button = QPushButton("Apply")
        self.apply_button.setObjectName("star")
        self.apply_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.apply_button.setMinimumWidth(168)
        self.apply_button.setToolTip("Choose how many applications to prepare")
        self.apply_button.clicked.connect(self.apply_clicked)
        actions.addWidget(self.apply_button, 0, Qt.AlignmentFlag.AlignRight)
        self.review_button = QPushButton("Review applications")
        self.review_button.setObjectName("heroLink")
        self.review_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.review_button.setStyleSheet("QPushButton#heroLink { background: transparent; border: none; "
                                         "color: rgba(255,255,255,0.78); font-weight: 600; padding: 4px 8px; }"
                                         "QPushButton#heroLink:hover { color: #FFFFFF; }")
        self.review_button.clicked.connect(self.review_clicked)
        actions.addWidget(self.review_button, 0, Qt.AlignmentFlag.AlignRight)
        actions.addStretch(1)
        row.addLayout(actions)

    def set_counts(self, review: int, approved: int, focus: int, new: int, fresh: int = 0) -> None:
        parts = []
        if new:
            parts.append(f"{new:,} new matches")
        if fresh:
            parts.append(f"{fresh:,} posted in the last 24 hours")
        if review:
            parts.append(f"{review} to review")
        if approved:
            parts.append(f"{approved} approved")
        if focus:
            parts.append(f"{focus} in interviews or assessments")
        self.stats.setText("  ·  ".join(parts))
        self.stats.setVisible(bool(parts))
        self.review_button.setVisible(bool(review or approved))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = rounded_path(rect, 20)
        base = QLinearGradient(rect.topLeft(), rect.bottomRight())
        base.setColorAt(0.0, QColor("#0E0E14"))
        base.setColorAt(1.0, QColor("#050507"))
        painter.fillPath(path, base)
        painter.save()
        painter.setClipPath(path)
        # The star sits above the Apply button; its trail runs up and left, off the card.
        glow_center = QPointF(rect.right() - 112, rect.top() + 40)
        glow = QRadialGradient(glow_center, 230)
        glow.setColorAt(0.0, QColor(255, 196, 51, 120))
        glow.setColorAt(0.35, QColor(255, 196, 51, 38))
        glow.setColorAt(1.0, QColor(255, 196, 51, 0))
        painter.fillRect(rect, glow)
        # A faint trail behind the star.
        tail = QPointF(glow_center.x() - 190, glow_center.y() - 70)
        trail = QLinearGradient(tail, glow_center)
        trail.setColorAt(0.0, QColor(255, 210, 90, 0))
        trail.setColorAt(1.0, QColor(255, 210, 90, 170))
        pen = QPen(trail, 2.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(tail, glow_center)
        paint_star(painter, glow_center, 15, QColor("#FFD24D"))
        paint_star(painter, glow_center, 6, QColor("#FFF6D8"))
        for x, y, r in ((0.08, 0.22, 1.2), (0.33, 0.12, 0.9), (0.52, 0.80, 1.1), (0.64, 0.30, 0.8),
                        (0.90, 0.78, 1.0), (0.44, 0.48, 0.7), (0.20, 0.86, 0.9)):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 110))
            painter.drawEllipse(QPointF(rect.left() + rect.width() * x, rect.top() + rect.height() * y), r, r)
        painter.restore()
        painter.setPen(QPen(QColor(255, 255, 255, 26), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.end()


PLAN_WINDOWS = (("Any time", 0), ("24 hours", 24), ("3 days", 72), ("Week", 168))
WINDOW_PHRASES = {0: "", 24: " posted in the last 24 hours", 72: " posted in the last 3 days",
                  168: " posted in the last week"}


class ApplyPlanSheet(QDialog):
    """Choose how many applications to prepare and how.

    ``counts`` holds new roles on supported forms by posting window
    (``preparable_0`` for any time, ``preparable_24`` and so on).
    """

    def __init__(self, settings: dict, new_matches: int, parent: QWidget | None = None,
                 counts: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Prepare applications")
        prepare_window(self)
        self.setModal(True)
        self.setMinimumWidth(560)
        plan = settings.get("apply_plan") or {}
        column = QVBoxLayout(self)
        column.setContentsMargins(26, 24, 26, 22)
        column.setSpacing(12)
        column.addWidget(label("Prepare applications", "sheetTitle"))
        column.addWidget(label(
            "Starployment reads each employer's form, builds your resume for the role, and fills every answer "
            "you have saved. Luna drafts the custom questions. Each application waits in Applications until you "
            "approve and send it.", "subtitle"))
        group = FormGroup()
        self.count = QSpinBox()
        self.count.setRange(1, 500)
        self.count.setValue(int(plan.get("count", 100)))
        self.count.setSuffix(" jobs")
        self.min_score = QSpinBox()
        self.min_score.setRange(0, 100)
        self.min_score.setValue(int(plan.get("min_score", 60)))
        self.one_per_company = ToggleSwitch()
        self.one_per_company.setChecked(bool(plan.get("one_per_company", True)))
        self._counts = dict(counts or {})
        self.window_choice = SegmentedControl()
        for text, hours in PLAN_WINDOWS:
            self.window_choice.addItem(text, hours)
        self.window_choice.setCurrentIndex(max(0, self.window_choice.findData(int(plan.get("posted_within_hours", 0) or 0))))
        self.order = SegmentedControl()
        self.order.addItem("Best match", "match")
        self.order.addItem("Newest first", "newest")
        self.order.setCurrentIndex(max(0, self.order.findData(str(plan.get("order", "match")))))
        self.use_luna = ToggleSwitch()
        backend = (settings.get("agent") or {}).get("backend", "none")
        connected = backend not in ("", "none")
        self.use_luna.setChecked(connected and bool((settings.get("luna") or {}).get("draft_answers", True)))
        self.use_luna.setEnabled(connected)
        group.add_row("Prepare the top", Stepper(self.count, width=110), "Picked in the order below.")
        group.add_row("Posted within", self.window_choice, "Early applicants get seen first.")
        group.add_row("Order", self.order, "Best match, or newest postings.")
        group.add_row("Minimum match", Stepper(self.min_score), "Skip roles below this score.")
        group.add_row("One per company", self.one_per_company, "Spread applications across employers.")
        group.add_row("AI Autofill every question", self.use_luna,
                      "Each application comes out ready to send; assumptions are labeled for you to check."
                      if connected else "Connect Luna in Settings → Connections first.")
        strategy = {"tailored": "A one-page resume generated for each role from your resume builder",
                    "library": "The resume file that matches each role type",
                    "original": "Your main resume"}.get(str(settings.get("resume_strategy", "tailored")), "")
        group.add_row("Resume", label(strategy, "rowNote"), "Change this in Settings → Resume.")
        column.addWidget(group)
        self._new_matches = new_matches
        self.available = label("", "caption")
        column.addWidget(self.available)
        priority = [c for c in settings.get("priority_companies") or [] if c]
        if priority:
            column.addWidget(label("Priority companies (" + ", ".join(priority[:4]) + (" and more" if len(priority) > 4 else "")
                                   + ") are prepared and starred, then wait for you to approve each one.", "caption"))
        if settings.get("dry_run"):
            column.addWidget(label("Practice mode is on, so sending fills forms without clicking Submit. "
                                   "Turn it off in Settings when you are ready to submit for real.", "questions"))
        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = button("Cancel", "secondary")
        cancel.clicked.connect(self.reject)
        self.go = QPushButton()
        self.go.setObjectName("star")
        self.go.setDefault(True)
        self.go.setCursor(Qt.CursorShape.PointingHandCursor)
        self.go.clicked.connect(self.accept)
        self.count.valueChanged.connect(self._label)
        self.window_choice.currentIndexChanged.connect(self._label)
        self._label()
        actions.addWidget(cancel)
        actions.addWidget(self.go)
        column.addLayout(actions)

    def _label(self, *_args) -> None:
        count = self.count.value()
        self.go.setText(f"Prepare {count} application{'s' if count != 1 else ''}")
        hours = int(self.window_choice.currentData() or 0)
        if f"preparable_{hours}" in self._counts:
            ready = int(self._counts[f"preparable_{hours}"])
            self.available.setText(f"{ready:,} new role{'s' if ready != 1 else ''} on Greenhouse, Lever, or Ashby "
                                   f"forms{WINDOW_PHRASES.get(hours, '')}. Other roles stay in Jobs.")
        else:
            self.available.setText(f"You have {self._new_matches:,} new matches. Greenhouse, Lever, and Ashby roles "
                                   "can be prepared; others stay in Jobs.")

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        paint_backdrop(painter, self)
        painter.end()

    def plan(self) -> dict:
        return {"count": self.count.value(), "min_score": float(self.min_score.value()),
                "one_per_company": self.one_per_company.isChecked(), "use_luna": self.use_luna.isChecked(),
                "posted_within_hours": int(self.window_choice.currentData() or 0),
                "order": str(self.order.currentData() or "match")}


class DashboardView(QWidget):
    apply_plan_requested = Signal(dict)
    review_requested = Signal()
    prepare_requested = Signal(str)
    setup_requested = Signal()
    jobs_requested = Signal(str)          # filter: all | new | applied | needs_human | fresh (posted in 24 hours)
    applications_requested = Signal(object)  # application id or None
    apply_requested = Signal(str)
    open_url_requested = Signal(str)
    tracker_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self._first_name = ""
        self._setup_complete = True
        self._salary_labels: list[QWidget] = []
        self._signatures: dict[str, object] = {}
        self._settings: dict = {}
        self._new_matches = 0
        self._fresh = 0
        self._plan_counts: dict = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("page")
        scroll.setWidget(content)
        column = QVBoxLayout(content)
        column.setContentsMargins(30, 26, 30, 28)
        column.setSpacing(18)

        self.header = PageHeader(greeting(), "", _today())
        self.edit_button = button("Edit setup", "secondary", icon="settings")
        self.edit_button.clicked.connect(self.setup_requested)
        self.header.add_action(self.edit_button)
        column.addWidget(self.header)

        self.hero = ApplyHero()
        self.hero.apply_clicked.connect(self._open_plan)
        self.hero.review_clicked.connect(self.review_requested)
        column.addWidget(self.hero)

        # Onboarding hero, shown until candidate setup is complete.
        self.onboarding = Card(radius=20, tint="accent", padding=(22, 20, 22, 20))
        self.onboarding.title_label.hide()
        hero = QHBoxLayout()
        hero.setSpacing(18)
        hero.addWidget(IconBadge("file-up", "accent", 52), 0, Qt.AlignmentFlag.AlignTop)
        hero_text = QVBoxLayout()
        hero_text.setSpacing(4)
        hero_text.addWidget(label("GET STARTED", "eyebrow"))
        hero_text.addWidget(label("Build your application profile", "sectionTitle"))
        hero_text.addWidget(label(
            "Add your resume, contact details, target roles, and the answers employers ask for. "
            "Starployment only uses facts you confirm.", "subtitle"))
        begin = button("Complete setup", icon="chevron-right", trailing=True)
        begin.clicked.connect(self.setup_requested)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 8, 0, 0)
        actions.addWidget(begin)
        actions.addStretch(1)
        hero_text.addLayout(actions)
        hero.addLayout(hero_text, 1)
        self.onboarding.body.addLayout(hero)
        column.addWidget(self.onboarding)

        # Metric tiles.
        self.metrics_container = QWidget()
        self.metrics_container.setObjectName("page")
        tiles = QHBoxLayout(self.metrics_container)
        tiles.setContentsMargins(0, 0, 0, 0)
        tiles.setSpacing(14)
        self.tiles: dict[str, MetricTile] = {}
        for key, title, icon, tone, target in (
            ("new", "New matches", "trending", "accent", "new"),
            ("fresh_24", "Posted in 24 hours", "clock", "indigo", "fresh"),
            ("applied", "Applied", "success", "green", "applied"),
            ("needs_human", "Needs review", "alert", "amber", None),
        ):
            tile = MetricTile(title, icon, tone)
            if target is None:
                tile.clicked.connect(lambda: self.applications_requested.emit(None))
            else:
                tile.clicked.connect(lambda t=target: self.jobs_requested.emit(t))
            self.tiles[key] = tile
            tiles.addWidget(tile, 1)
        column.addWidget(self.metrics_container)

        # Lower area: top matches beside attention and activity.
        lower = QHBoxLayout()
        lower.setSpacing(14)
        self.matches_card = Card("Top matches")
        see_all = button("See all", "quiet")
        see_all.clicked.connect(lambda: self.jobs_requested.emit("new"))
        self.matches_card.add_header_action(see_all)
        self.empty_matches = label("New matches appear here after a scan.", "subtitle")
        self.matches_card.body.addWidget(self.empty_matches)
        lower.addWidget(self.matches_card, 3, Qt.AlignmentFlag.AlignTop)

        side = QVBoxLayout()
        side.setSpacing(14)
        self.review_card = Card("Needs your attention")
        self.review_button = button("Review", "quiet")
        self.review_button.clicked.connect(lambda: self.applications_requested.emit(None))
        self.review_button.hide()
        self.review_card.add_header_action(self.review_button)
        self.empty_attention = label("Nothing needs you right now.", "subtitle")
        self.review_card.body.addWidget(self.empty_attention)
        self.more_attention = label("", "caption")
        self.more_attention.hide()
        side.addWidget(self.review_card)

        self.focus_card = Card("In progress")
        self.focus_card.title_label.setToolTip("Applications with an assessment, interview, or offer")
        self.focus_card.hide()
        side.addWidget(self.focus_card)

        self.activity_card = Card("Recent activity")
        self.empty_activity = label("Scans and application updates will appear here.", "subtitle")
        self.activity_card.body.addWidget(self.empty_activity)
        self.activity_rows = QVBoxLayout()
        self.activity_rows.setSpacing(0)
        self.activity_card.body.addLayout(self.activity_rows)
        side.addWidget(self.activity_card)
        side.addStretch(1)
        lower.addLayout(side, 2)
        column.addLayout(lower)
        column.addStretch(1)
        self.unanswered = self.more_attention  # compatibility alias

    # ------------------------------------------------------------------ data
    def _changed(self, key: str, signature: object) -> bool:
        if self._signatures.get(key) == signature:
            return False
        self._signatures[key] = signature
        return True

    def set_profile(self, profile: Any) -> None:
        data = profile.model_dump() if hasattr(profile, "model_dump") else (profile or {})
        self._first_name = str(data.get("first_name") or data.get("name") or "").strip()
        self.header.title_label.setText(greeting(self._first_name))

    def set_setup_complete(self, complete: bool) -> None:
        self._setup_complete = complete
        self.onboarding.setVisible(not complete)
        self.hero.setVisible(complete)
        self.edit_button.setText("Edit setup" if complete else "Set up")

    def set_settings(self, settings: dict) -> None:
        self._settings = dict(settings or {})

    def set_pipeline(self, drafts: dict[str, int], focus: list[dict]) -> None:
        """Counts of prepared applications and the applications needing your focus."""
        review = sum(drafts.get(s, 0) for s in ("ready", "needs_answers", "practice"))
        self.hero.set_counts(review, drafts.get("approved", 0), len(focus), self._new_matches, self._fresh)
        signature = tuple((r.get("id"), r.get("stage")) for r in focus[:5])
        if not self._changed("focus", signature):
            return
        self.focus_card.clear_rows()
        self.focus_card.setVisible(bool(focus))
        for record in focus[:5]:
            row = ActionRow()
            stage = str(record.get("stage") or "")
            tone = {"interview": "amber", "assessment": "indigo", "offer": "green"}.get(stage, "accent")
            icon = {"interview": "clock", "assessment": "code", "offer": "award"}.get(stage, "info")
            row.layout_.addWidget(IconBadge(icon, tone, 28))
            text = QVBoxLayout()
            text.setSpacing(1)
            text.addWidget(ElidedLabel(f"{record.get('company', 'Employer')} · {record.get('title', 'Role')}", "rowPrimary"))
            text.addWidget(ElidedLabel({"interview": "Interview", "assessment": "Online assessment",
                                        "offer": "Offer"}.get(stage, stage.title()), "rowSecondary"))
            row.layout_.addLayout(text, 1)
            row.finish()
            row.clicked.connect(lambda: self.tracker_requested.emit())
            self.focus_card.add_row(row)

    def _open_plan(self) -> None:
        sheet = ApplyPlanSheet(self._settings, self._new_matches, self.window(), counts=self._plan_counts)
        if sheet.exec() == QDialog.DialogCode.Accepted:
            self.apply_plan_requested.emit(sheet.plan())

    def set_stats(self, stats: dict) -> None:
        self._new_matches = int(stats.get("new", 0) or 0)
        self._fresh = int(stats.get("fresh_24", 0) or 0)
        self._plan_counts = {key: value for key, value in stats.items() if key.startswith("preparable_")}
        for key, tile in self.tiles.items():
            tile.value.set_value(int(stats.get(key, 0) or 0))
        new, review = int(stats.get("new", 0) or 0), int(stats.get("needs_human", 0) or 0)
        if not self._setup_complete:
            text = "Finish setup and Starployment will start finding roles for you."
        elif new or review:
            parts = []
            if new:
                parts.append(f"{plural(new, 'new match', 'es')} ready to explore")
            if review:
                parts.append(f"{plural(review, 'application')} waiting on you")
            text = " and ".join(parts) + "."
        else:
            text = "You're all caught up. Starployment keeps searching in the background."
        self.header.set_subtitle(text)
        self.header.set_eyebrow(_today())

    def set_top_jobs(self, jobs: list[Any], retryable_ids: set[str] | None = None) -> None:
        signature = tuple((j.id, j.status, round(j.score, 1)) for j in jobs[:5])
        if not self._changed("jobs", signature):
            return
        self.matches_card.clear_rows()
        self._salary_labels = []
        self.empty_matches.setVisible(not jobs)
        for job in jobs[:5]:
            row = ActionRow(chevron=False)
            row.setToolTip("Open the posting")
            row.layout_.addWidget(ScoreRing(float(job.score)))
            text = QVBoxLayout()
            text.setSpacing(1)
            text.addWidget(ElidedLabel(job.title, "rowPrimary"))
            where = job.location or ("Remote" if job.remote else "Location not listed")
            text.addWidget(ElidedLabel(f"{job.company} · {where}", "rowSecondary"))
            row.layout_.addLayout(text, 1)
            pay = salary(job.salary_min, job.salary_max)
            if pay != "—":
                pay_label = label(pay, "caption", wrap=False)
                self._salary_labels.append(pay_label)
                row.layout_.addWidget(pay_label)
            if job.status == "new":
                apply = button("Prepare", "secondary")
                apply.setToolTip("Prepare this application for your review")
                apply.clicked.connect(lambda _=False, jid=job.id: self.prepare_requested.emit(jid))
                row.layout_.addWidget(apply)
            row.clicked.connect(lambda url=job.url: self.open_url_requested.emit(url))
            self.matches_card.add_row(row)
        self._fit_matches()

    def _fit_matches(self) -> None:
        # Salaries yield their space to role titles when the card is narrow.
        roomy = self.matches_card.width() >= 620
        for widget in self._salary_labels:
            widget.setVisible(roomy)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_matches()

    def set_applications(self, applications: list[dict], stats: dict | None) -> None:
        pending: list[tuple[dict, int]] = []
        seen_jobs: set[str] = set()
        for app in applications:
            job_id = app.get("job_id")
            if not job_id or job_id in seen_jobs:
                continue
            seen_jobs.add(job_id)
            if app.get("status") != "needs_human" or app.get("hidden"):
                continue   # removed from Applications by the candidate
            questions = list(dict.fromkeys(app.get("unanswered", []) or []))
            if questions:
                pending.append((app, len(questions)))
        needs_review = int((stats or {}).get("needs_human", 0) or 0)
        self.review_button.setVisible(bool(pending) or bool(needs_review))
        signature = (tuple((a.get("id"), a.get("job_id"), n) for a, n in pending), needs_review)
        if not self._changed("attention", signature):
            return
        self.review_card.clear_rows()
        if not pending and needs_review:
            self.empty_attention.setText(
                f"{plural(needs_review, 'application')} need review. Open Applications for details.")
        elif not pending:
            self.empty_attention.setText("Nothing needs you right now.")
        self.empty_attention.setVisible(not pending)
        for app, count in pending[:4]:
            row = ActionRow()
            row.layout_.addWidget(IconBadge("alert", "amber", 28))
            text = QVBoxLayout()
            text.setSpacing(1)
            text.addWidget(ElidedLabel(f"{app.get('company', 'Employer')} · {app.get('title', 'Role')}", "rowPrimary"))
            text.addWidget(ElidedLabel(f"{plural(count, 'question')} to answer", "rowSecondary"))
            row.layout_.addLayout(text, 1)
            row.finish()
            row.clicked.connect(lambda app_id=app.get("id"): self.applications_requested.emit(app_id))
            self.review_card.add_row(row)
        if len(pending) > 4:
            more = label(f"+ {len(pending) - 4} more in Applications", "caption")
            more.setContentsMargins(8, 6, 0, 0)
            self.review_card.add_row(more)

    def set_events(self, events: list[dict], applications: list[dict] | None) -> None:
        applications_by_job = {
            str(record.get("job_id")): record for record in (applications or []) if record.get("job_id")
        }
        entries: list[tuple[str, str, str]] = []
        for event in events:
            message = str(event.get("msg", ""))
            text = activity_label(message, applications_by_job)
            if text is None:
                continue
            entries.append((str(event.get("ts") or ""), text, activity_tone(message, str(event.get("level") or ""))))
            if len(entries) == 5:
                break
        if not self._changed("events", tuple(entries)):
            return
        while self.activity_rows.count():
            item = self.activity_rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.empty_activity.setVisible(not entries)
        for timestamp, text, tone in entries:
            row = QFrame()
            row.setObjectName("activityRow")
            line = QHBoxLayout(row)
            line.setContentsMargins(2, 7, 2, 7)
            line.setSpacing(10)
            line.addWidget(StatusDot(tone), 0, Qt.AlignmentFlag.AlignVCenter)
            message = ElidedLabel(text, "activityMessage")
            message.setToolTip(text)
            line.addWidget(message, 1)
            when = label(relative_time(timestamp), "activityTime", wrap=False)
            when.setToolTip(local_time(timestamp))
            line.addWidget(when)
            self.activity_rows.addWidget(row)
