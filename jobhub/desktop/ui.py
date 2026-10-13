"""Native Qt Widgets shell for Starployment.

The window is intentionally controller-friendly. It does not import or call
the scheduler. A controller connects the public signals and feeds refreshed
data to ``set_data``; this keeps browser work and SQLite writes off the UI
thread. Page implementations live in ``jobhub.desktop.views`` and the setup
wizard in ``jobhub.desktop.wizard``; both are re-exported here for callers
that import from ``jobhub.desktop.ui``.
"""
from __future__ import annotations

import sys
import time
from typing import Any

import shiboken6
from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QVBoxLayout,
    QWidget,
)

from .format import activity_label as _activity_label
from .format import countdown, local_time as _local_time, retryable_job_ids
from .icon import icon_path
from .native import TITLE_BAR_HEIGHT, configure_window_chrome, style_title_bar
from .native_glass import prepare_window, window_has_native_glass
from .views.applications import ApplicationsView
from .views.dashboard import DashboardView
from .views.jobs import JobsView
from .views.profile import ProfileView
from .views.settings import SettingsView
from .views.tracker import TrackerView
from .widgets import (
    AmbientBackdrop,
    FadeStack,
    GlassNavList,
    GlassPanel,
    Motion,
    Toast,
    button,
    label,
    reduced_motion,
)
from .console import ActivityConsole
from .wizard import SetupWizard

__all__ = [
    "ApplicationsView", "DashboardView", "JobHubWindow", "JobsView", "ProfileView", "SettingsView",
    "SetupWizard", "StarploymentWindow", "TrackerView", "_activity_label", "_local_time",
]

PAGES = (
    ("Overview", "dashboard"),
    ("Jobs", "jobs"),
    ("Applications", "applications"),
    ("Tracker", "tracker"),
    ("Profile", "profile"),
    ("Settings", "settings"),
)
OVERVIEW, JOBS, APPLICATIONS, TRACKER, PROFILE, SETTINGS = range(len(PAGES))


class ShellBackdrop(AmbientBackdrop):
    """The window backdrop; with native chrome its top band drags the window."""

    def __init__(self, draggable: bool, parent: QWidget | None = None):
        super().__init__(parent)
        self._draggable = draggable

    def _in_title_band(self, event) -> bool:
        return self._draggable and event.position().y() <= TITLE_BAR_HEIGHT + 6

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._in_title_band(event):
            handle = self.window().windowHandle()
            if handle is not None and handle.startSystemMove():
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if self._in_title_band(event):
            window = self.window()
            window.showNormal() if window.isMaximized() else window.showMaximized()
            return
        super().mouseDoubleClickEvent(event)


class Sidebar(GlassPanel):
    """Floating glass sidebar: brand, navigation, and background-scan status."""

    WIDTH = 236

    def __init__(self, native_titlebar: bool, parent: QWidget | None = None):
        super().__init__(parent, radius=22, material="sidebar")
        self.setObjectName("sidebar")
        self.setFixedWidth(self.WIDTH)
        column = QVBoxLayout(self)
        column.setContentsMargins(6, TITLE_BAR_HEIGHT if native_titlebar else 18, 6, 12)
        column.setSpacing(6)
        brand = QHBoxLayout()
        brand.setContentsMargins(14, 0, 14, 6)
        brand.setSpacing(10)
        if icon_path().is_file():
            mark = QLabel()
            pixmap = QPixmap(str(icon_path()))
            pixmap.setDevicePixelRatio(2.0)
            mark.setPixmap(pixmap.scaled(56, 56, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation))
            brand.addWidget(mark)
        brand.addWidget(label("Starployment", "brand", wrap=False))
        brand.addStretch()
        column.addLayout(brand)
        self.nav = GlassNavList()
        for title, icon in PAGES:
            self.nav.add_destination(title, icon)
        self.nav.setFixedHeight(self.nav.sizeHintForRow(0) * len(PAGES) + 8)
        column.addWidget(self.nav)
        column.addSpacing(6)
        # A terminal-style log replaces pop-up notices: scans, inbox checks,
        # applications sent, Luna, and errors, with the scan status on top.
        self.console = ActivityConsole()
        console_row = QHBoxLayout()
        console_row.setContentsMargins(8, 0, 8, 0)
        console_row.addWidget(self.console)
        column.addLayout(console_row, 1)
        self.status_title = self.console.status_title
        self.status_detail = self.console.status_detail
        self.spinner = self.console.spinner
        self.status_dot = self.console.status_dot
        column.addSpacing(8)
        self.scan = button("Refresh jobs", "secondary", icon="refresh")
        self.scan.setToolTip("Scan every configured source now (⌘R)")
        refresh_row = QHBoxLayout()
        refresh_row.setContentsMargins(8, 0, 8, 0)
        refresh_row.addWidget(self.scan)
        column.addLayout(refresh_row)

    def set_status(self, title: str, detail: str, tone: str, busy: bool) -> None:
        self.console.set_status(title, detail, tone, busy)


class JobHubWindow(QMainWindow):
    """Primary application shell.

    Signals form the controller contract: ``scan_requested``,
    ``apply_requested(job_id)``, ``status_requested(job_id, status)``,
    ``import_requested(url)``, ``save_profile_requested(data)``,
    ``save_settings_requested(patch)``, ``appearance_requested(appearance)``,
    ``setup_completed(data)``, ``setup_requested``,
    ``answer_questions_requested(questions)``, and ``open_url_requested(url)``.
    """

    scan_requested = Signal()
    apply_requested = Signal(str)
    skip_requested = Signal(str)
    status_requested = Signal(str, str)
    import_requested = Signal(str)
    save_profile_requested = Signal(dict)
    save_settings_requested = Signal(dict)
    appearance_requested = Signal(str)
    typeface_requested = Signal(str)
    setup_completed = Signal(dict)
    setup_requested = Signal()
    answer_questions_requested = Signal(list)
    open_url_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Starployment")
        prepare_window(self)
        self.resize(1280, 820)
        self.setMinimumSize(1000, 680)
        self._native_chrome = configure_window_chrome(self)
        self._setup_complete = True
        self._auto_apply = False
        self._crossfade: QPropertyAnimation | None = None

        root = ShellBackdrop(self._native_chrome)
        root.setObjectName("shell")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(8, 8, 0, 8) if self._native_chrome else outer.setContentsMargins(10, 10, 0, 10)
        outer.setSpacing(4)
        self.sidebar = Sidebar(self._native_chrome)
        root.add_shadow_target(self.sidebar, self.sidebar.radius)
        outer.addWidget(self.sidebar)
        self.nav = self.sidebar.nav
        self.scan = self.sidebar.scan
        self.scan.clicked.connect(self.scan_requested)

        self.stack = FadeStack(mode="rise")
        outer.addWidget(self.stack, 1)
        self.dashboard = DashboardView()
        self.jobs = JobsView()
        self.apps = ApplicationsView()
        self.tracker = TrackerView()
        self.profile = ProfileView()
        self.settings = SettingsView()
        for page in (self.dashboard, self.jobs, self.apps, self.tracker, self.profile, self.settings):
            self.stack.addWidget(page)
        self.toast = Toast(root)
        self.toast.set_area(self.stack)

        # Compatibility aliases used by tests and older controller code.
        self.review_button = self.dashboard.review_button
        self.activity_rows = self.dashboard.activity_rows
        self.onboarding = self.dashboard.onboarding
        self.metrics_container = self.dashboard.metrics_container
        self.empty_attention = self.dashboard.empty_attention
        self.empty_activity = self.dashboard.empty_activity
        self.unanswered = self.dashboard.unanswered

        self.nav.currentRowChanged.connect(self._switch_page)
        self.nav.setCurrentRow(OVERVIEW)

        self.dashboard.setup_requested.connect(self.setup_requested)
        self.dashboard.jobs_requested.connect(self.show_jobs)
        self.dashboard.applications_requested.connect(self.show_application)
        self.dashboard.apply_requested.connect(self.apply_requested)
        self.dashboard.open_url_requested.connect(self.open_url_requested)
        self.dashboard.review_requested.connect(lambda: self.show_applications("review"))
        self.dashboard.tracker_requested.connect(lambda: self.nav.setCurrentRow(TRACKER))
        self.jobs.review_requested.connect(self.show_draft_for_job)
        self.jobs.bulk_prepare_requested.connect(self._open_plan)
        self.tracker.open_requested.connect(self.open_url_requested)
        self.tracker.connections_requested.connect(self.show_connections)
        self.jobs.apply_requested.connect(self.apply_requested)
        self.jobs.open_requested.connect(self.open_url_requested)
        self.jobs.import_requested.connect(self.import_requested)
        self.jobs.status_requested.connect(self.status_requested)
        self.apps.open_requested.connect(self.open_url_requested)
        self.apps.apply_requested.connect(self.apply_requested)
        self.apps.answer_questions_requested.connect(self.answer_questions_requested)
        self.profile.save_requested.connect(self._profile_save)
        self.settings.save_requested.connect(self._settings_save)
        self.settings.appearance_changed.connect(self.appearance_requested)
        self.settings.typeface_changed.connect(self.typeface_requested)
        self._install_actions()
        self.set_scan_state(scanning=False, next_scan_at=None, setup_complete=True, scheduler_enabled=True)

    # ------------------------------------------------------------ chrome
    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._native_chrome:
            style_title_bar(self)

    def _install_actions(self) -> None:
        def action(text: str, shortcut: QKeySequence | str | None, slot, role=None) -> QAction:
            item = QAction(text, self)
            if shortcut is not None:
                item.setShortcut(QKeySequence(shortcut))
            item.triggered.connect(slot)
            if role is not None:
                item.setMenuRole(role)
            self.addAction(item)
            return item

        self.refresh_action = action("Refresh Jobs", "Ctrl+R", self.scan_requested.emit)
        self.apply_action = action("Prepare bulk…", "Ctrl+Shift+A", self._open_plan)
        self.add_posting_action = action("Add Posting…", "Ctrl+N", self._add_posting)
        self.find_action = action("Find Jobs", QKeySequence.StandardKey.Find, self._find_jobs)
        self.setup_action = action("Edit Setup…", "Ctrl+Shift+E", self.setup_requested.emit)
        self.save_action = action("Save", QKeySequence.StandardKey.Save, self._save_current)
        self.preferences_action = action("Settings…", QKeySequence.StandardKey.Preferences,
                                         lambda: self.nav.setCurrentRow(SETTINGS),
                                         QAction.MenuRole.PreferencesRole)
        self.page_actions = [
            action(title, f"Ctrl+{index + 1}", lambda _=False, row=index: self.nav.setCurrentRow(row))
            for index, (title, _) in enumerate(PAGES)
        ]
        # Only a native macOS menu bar; elsewhere the shortcuts stay on the window.
        if sys.platform == "darwin" and QGuiApplication.platformName() == "cocoa":
            bar = self.menuBar()
            file_menu = bar.addMenu("File")
            for item in (self.refresh_action, self.apply_action, self.add_posting_action, self.setup_action, self.save_action):
                file_menu.addAction(item)
            file_menu.addAction(self.preferences_action)
            edit_menu = bar.addMenu("Edit")
            edit_menu.addAction(self.find_action)
            view_menu = bar.addMenu("View")
            for item in self.page_actions:
                view_menu.addAction(item)
            bar.addMenu("Window")

    def _add_posting(self) -> None:
        self.nav.setCurrentRow(JOBS)
        self.jobs._import_dialog()

    def _find_jobs(self) -> None:
        self.nav.setCurrentRow(JOBS)
        self.jobs.focus_search()

    def _save_current(self) -> None:
        page = self.stack.currentWidget()
        if page is self.profile and self.profile.dirty:
            self.profile._save()
        elif page is self.settings and self.settings.dirty:
            self.settings._save()
        elif page is self.apps:
            self.apps._save_note()

    def _profile_save(self, data: dict) -> None:
        self.profile.tracker.reset()
        self.save_profile_requested.emit(data)

    def _settings_save(self, patch: dict) -> None:
        self.settings.tracker.reset()
        self.save_settings_requested.emit(patch)

    def _switch_page(self, index: int) -> None:
        if index >= 0:
            self.stack.setCurrentIndex(index)

    def _open_plan(self) -> None:
        """Open the shared bulk plan, carrying the Jobs star exclusion into it."""
        exclude = True if self.nav.currentRow() == JOBS and self.jobs.exclude_starred else None
        self.dashboard._open_plan(exclude_starred=exclude)

    def show_jobs(self, status: str = "all") -> None:
        """Open Jobs on a status filter; ``fresh`` shows new roles posted in the last 24 hours."""
        if status == "fresh":
            self.jobs.set_filter("new")
            self.jobs.set_posted_window(24)
        else:
            self.jobs.set_filter(status or "all")
            self.jobs.set_posted_window(0)
        self.nav.setCurrentRow(JOBS)

    def show_application(self, app_id: object = None) -> None:
        self.nav.setCurrentRow(APPLICATIONS)
        if app_id is not None:
            self.apps.select_application_id(app_id)

    def show_applications(self, filter_value: str = "all") -> None:
        self.apps.show_filter(filter_value)
        self.nav.setCurrentRow(APPLICATIONS)

    def show_draft_for_job(self, job_id: str) -> None:
        self.apps.show_filter("all")
        self.nav.setCurrentRow(APPLICATIONS)
        self.apps.select_draft_for_job(job_id)

    def show_connections(self) -> None:
        self.nav.setCurrentRow(SETTINGS)
        self.settings.show_connections()

    # ---------------------------------------------------------- feedback
    def notify(self, message: str, tone: str = "info", timeout: int = 4500) -> None:
        """Log a notice in the sidebar console. ``tone``: info, success, warning, error, or busy.

        A ``busy`` line updates in place until the next message. ``timeout`` is
        kept for callers written for the old pop-up notices.
        """
        self.sidebar.console.write(message, tone)

    def crossfade(self, change) -> None:
        """Run ``change`` (e.g. a theme switch) under a brief crossfade of the old frame."""
        root = self.centralWidget()
        if reduced_motion() or not self.isVisible() or root is None or window_has_native_glass(self):
            change()
            return
        # The previous fade is parented to its overlay, which deletes itself (and the
        # animation) when the fade ends. If one is still running, stop it and remove
        # its overlay too, or a frozen frame of the old theme would stay on top.
        previous = self._crossfade
        self._crossfade = None
        if previous is not None and shiboken6.isValid(previous):
            previous.stop()
            overlay = previous.parent()
            if overlay is not None and shiboken6.isValid(overlay):
                overlay.hide()
                overlay.deleteLater()
        snapshot = root.grab()
        overlay = QLabel(root)
        overlay.setPixmap(snapshot)
        overlay.setGeometry(root.rect())
        overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        overlay.show()
        overlay.raise_()
        change()
        effect = QGraphicsOpacityEffect(overlay)
        overlay.setGraphicsEffect(effect)
        fade = QPropertyAnimation(effect, b"opacity", overlay)
        fade.setDuration(Motion.SLOW)
        fade.setStartValue(1.0)
        fade.setEndValue(0.0)
        fade.setEasingCurve(QEasingCurve.Type.InOutCubic)
        fade.finished.connect(overlay.deleteLater)
        self._crossfade = fade
        fade.start()

    def set_scan_state(self, *, scanning: bool, next_scan_at: float | None = None,
                       setup_complete: bool | None = None, scheduler_enabled: bool = True,
                       auto_apply: bool | None = None) -> None:
        """Update the sidebar's background-activity summary."""
        if setup_complete is not None:
            self._setup_complete = setup_complete
        if auto_apply is not None:
            self._auto_apply = auto_apply
        automation = "Auto-apply on" if self._auto_apply else "Auto-apply off"
        self.scan.setEnabled(not scanning)
        self.scan.setText("Scanning…" if scanning else "Refresh jobs")
        if scanning:
            self.sidebar.set_status("Searching for jobs", "This usually takes a minute.", "accent", True)
        elif not self._setup_complete:
            self.sidebar.set_status("Setup needed", "Finish setup to start scanning.", "amber", False)
        elif not scheduler_enabled:
            self.sidebar.set_status("Scheduled scans paused", automation, "neutral", False)
        elif next_scan_at:
            self.sidebar.set_status(f"Next scan {countdown(next_scan_at - time.time())}", automation, "green", False)
        else:
            self.sidebar.set_status("Ready", automation, "green", False)

    # -------------------------------------------------------------- data
    def set_data(
        self, *, profile=None, settings=None, jobs=None, total_jobs=None,
        applications=None, stats=None, events=None, top_jobs=None,
        drafts=None, tracker=None, resume_files=None, mail=None,
    ) -> None:
        if profile is not None:
            if not self.profile.dirty:
                self.profile.set_profile(profile)
            self.dashboard.set_profile(profile)
        if settings is not None:
            if not self.settings.dirty:
                self.settings.set_settings(settings)
            if not self.profile.dirty:
                self.profile.set_settings(settings)
            self._setup_complete = bool(settings.get("setup_complete"))
            self._auto_apply = bool(settings.get("auto_apply"))
            self.dashboard.set_setup_complete(self._setup_complete)
            self.dashboard.set_settings(settings)
            backend = (settings.get("agent") or {}).get("backend", "none")
            priority = list(settings.get("priority_companies") or [])
            mailbox = settings.get("email_verification") or {}
            self.apps.set_context(practice=bool(settings.get("dry_run")), luna_connected=backend not in ("", "none"),
                                  priority=priority, mailbox={k: mailbox.get(k, "") for k in ("imap_host", "imap_username")},
                                  workday_send=bool((settings.get("workday") or {}).get("send")))
            self.jobs.set_priority(priority)
            tracking = settings.get("email_tracking") or {}
            self.tracker.set_email_state(bool(tracking.get("enabled")), (mail or {}).get("last_checked"),
                                         int((mail or {}).get("unmatched", 0)))
        if resume_files is not None:
            self.profile.files.set_files(resume_files)
        if top_jobs is not None:
            self.profile.builder.set_preview_jobs([(job.id, f"{job.company} · {job.title}") for job in top_jobs])
        retryable = self._retryable(applications or [])
        if jobs is not None:
            self.jobs.set_jobs(jobs, retryable, total_count=total_jobs)
        if top_jobs is not None:
            self.dashboard.set_top_jobs(top_jobs, retryable)
        if stats is not None:
            self.dashboard.set_stats(stats)
            new = int(stats.get("new", 0) or 0)
            self.nav.set_badge(JOBS, f"{new:,}" if new else "", "neutral")
            if drafts is None:
                review = int(stats.get("needs_human", 0) or 0)
                self.nav.set_badge(APPLICATIONS, str(review) if review else "", "amber")
        if applications is not None:
            self.apps.set_applications(applications, drafts)
            self.dashboard.set_applications(applications, stats)
        elif drafts is not None:
            self.apps.set_applications(self.apps.applications, drafts)
        if drafts is not None:
            counts: dict[str, int] = {}
            for draft in drafts:
                counts[draft.get("status", "")] = counts.get(draft.get("status", ""), 0) + 1
            review = sum(counts.get(key, 0) for key in ("ready", "needs_answers", "practice"))
            self.nav.set_badge(APPLICATIONS, str(review) if review else "", "accent")
        else:
            counts = None
        if tracker is not None:
            self.tracker.set_rows(tracker)
            focus = [row for row in tracker if row.get("stage") in ("assessment", "interview", "offer")]
            self.nav.set_badge(TRACKER, str(len(focus)) if focus else "", "amber")
            if counts is not None:
                self.dashboard.set_pipeline(counts, focus)
        if events is not None:
            self.dashboard.set_events(events, applications)
            self.sidebar.console.add_events(events, applications)

    @staticmethod
    def _retryable(applications: list[dict[str, Any]]) -> set[str]:
        return retryable_job_ids(applications)

    def show_setup(self, profile=None, settings=None) -> None:  # noqa: D102
        wizard = SetupWizard(profile, settings, self)
        wizard.completed.connect(self.setup_completed)
        wizard.exec()


StarploymentWindow = JobHubWindow
