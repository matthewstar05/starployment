"""Overview page: greeting, metric tiles, top matches, attention list, activity."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QFont, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from ..delegates import score_color
from ..format import activity_label, activity_tone, greeting, local_time, plural, relative_time, salary
from ..theme import current_colors
from ..widgets import (
    ActionRow,
    Card,
    ClickableGlass,
    CountUpLabel,
    ElidedLabel,
    IconBadge,
    PageHeader,
    StatusDot,
    button,
    label,
    qcolor,
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


class DashboardView(QWidget):
    setup_requested = Signal()
    jobs_requested = Signal(str)          # filter: all | new | applied | needs_human
    applications_requested = Signal(object)  # application id or None
    apply_requested = Signal(str)
    open_url_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self._first_name = ""
        self._setup_complete = True
        self._salary_labels: list[QWidget] = []
        self._signatures: dict[str, object] = {}
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
            "JobHub only uses facts you confirm.", "subtitle"))
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
            ("applied", "Applied", "success", "green", "applied"),
            ("needs_human", "Needs review", "alert", "amber", None),
            ("total", "Tracked roles", "layers", "indigo", "all"),
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
        self.edit_button.setText("Edit setup" if complete else "Set up")

    def set_stats(self, stats: dict) -> None:
        for key, tile in self.tiles.items():
            tile.value.set_value(int(stats.get(key, 0) or 0))
        new, review = int(stats.get("new", 0) or 0), int(stats.get("needs_human", 0) or 0)
        if not self._setup_complete:
            text = "Finish setup and JobHub will start finding roles for you."
        elif new or review:
            parts = []
            if new:
                parts.append(f"{plural(new, 'new match', 'es')} ready to explore")
            if review:
                parts.append(f"{plural(review, 'application')} waiting on you")
            text = " and ".join(parts) + "."
        else:
            text = "You're all caught up. JobHub keeps searching in the background."
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
                apply = button("Apply", "secondary")
                apply.setToolTip("Queue this application")
                apply.clicked.connect(lambda _=False, jid=job.id: self.apply_requested.emit(jid))
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
            if app.get("status") != "needs_human":
                continue
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
