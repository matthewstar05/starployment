"""Native Qt Widgets shell for Starployment.

The window is intentionally controller-friendly. It does not import or call
the scheduler. A controller connects the public signals and feeds refreshed
data to ``set_data``; this keeps browser work and SQLite writes off the UI
thread. Page implementations live in ``jobhub.desktop.views`` and the setup
wizard in ``jobhub.desktop.wizard``; both are re-exported here for callers
that import from ``jobhub.desktop.ui``.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any

import shiboken6
from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSettings, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QAction, QColor, QGuiApplication, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QDialog,
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
from ..jarvis import label as jarvis_label
from .theme import current_colors
from .widgets import (
    AmbientBackdrop,
    CapsuleButton,
    ChoicePopover,
    FadeStack,
    GlassNavList,
    GlassPanel,
    Motion,
    StatusPill,
    Toast,
    button,
    label,
    qcolor,
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


def _ui_pref(key: str, default):
    """A remembered window choice (sidebar width...). Tests set JOBHUB_UI_PREFS=0."""
    if os.environ.get("JOBHUB_UI_PREFS") == "0":
        return default
    return QSettings("Starployment", "Starployment").value(key, default)


def _save_ui_pref(key: str, value) -> None:
    if os.environ.get("JOBHUB_UI_PREFS") != "0":
        QSettings("Starployment", "Starployment").setValue(key, value)


class JarvisPill(StatusPill):
    """The sidebar's JARVIS status. Clicking it opens the mode menu."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clicked.emit()
            return
        super().keyPressEvent(event)


class SidebarHandle(QWidget):
    """The strip between the sidebar and the page: drag to resize, double-click to hide.

    Like Spotify's library pane, dragging it far enough left hides the sidebar.
    """

    resized = Signal(int)          # live width while dragging
    released = Signal(int)         # width at release
    toggled = Signal()

    def __init__(self, sidebar: "Sidebar", parent: QWidget | None = None):
        super().__init__(parent)
        self._sidebar = sidebar
        self._start: tuple[float, int] | None = None
        self._hover = False
        self.setFixedWidth(8)
        self.setCursor(Qt.CursorShape.SplitHCursor)
        self.setToolTip("Drag to resize the sidebar · double-click to hide it (⌘\\)")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._start = (event.globalPosition().x(), self._sidebar.width())
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._start is not None:
            origin, width = self._start
            self.resized.emit(int(width + event.globalPosition().x() - origin))

    def mouseReleaseEvent(self, event) -> None:
        if self._start is not None:
            origin, width = self._start
            self._start = None
            self.released.emit(int(width + event.globalPosition().x() - origin))

    def mouseDoubleClickEvent(self, event) -> None:
        self._start = None
        self.toggled.emit()

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()

    def paintEvent(self, event) -> None:
        if not (self._hover or self._start is not None):
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(qcolor(current_colors().get("accent", "#B8860B")))
        color.setAlphaF(0.55)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        height = min(64, self.height() - 24)
        painter.drawRoundedRect((self.width() - 3) / 2, (self.height() - height) / 2, 3, height, 1.5, 1.5)
        painter.end()


class Sidebar(GlassPanel):
    """Floating glass sidebar: brand, navigation, and background-scan status."""

    WIDTH = 236        # default
    MIN_WIDTH = 200
    MAX_WIDTH = 460
    COLLAPSE_AT = 150  # dragging narrower than this hides the sidebar

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
        self.jarvis_status = JarvisPill()
        self.jarvis_status.setObjectName("jarvisStatus")
        self.jarvis_status.set_status("", "JARVIS · OFF  ▾")
        jarvis_row = QHBoxLayout()
        jarvis_row.setContentsMargins(14, 0, 14, 4)
        jarvis_row.addWidget(self.jarvis_status)
        jarvis_row.addStretch()
        column.addLayout(jarvis_row)
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
    jarvis_mode_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Starployment")
        prepare_window(self)
        self.resize(1280, 820)
        self.setMinimumSize(1000, 680)
        self._native_chrome = configure_window_chrome(self)
        self._setup_complete = True
        self._auto_apply = False
        self._jarvis_mode = "off"
        self._jarvis_practice = False
        self._scheduler_enabled = True
        self._crossfade: QPropertyAnimation | None = None

        root = ShellBackdrop(self._native_chrome)
        root.setObjectName("shell")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(8, 8, 0, 8) if self._native_chrome else outer.setContentsMargins(10, 10, 0, 10)
        outer.setSpacing(0)
        self.sidebar = Sidebar(self._native_chrome)
        root.add_shadow_target(self.sidebar, self.sidebar.radius)
        outer.addWidget(self.sidebar)
        self.sidebar_handle = SidebarHandle(self.sidebar)
        outer.addWidget(self.sidebar_handle)
        self._outer = outer
        self._sidebar_width = self._clamp_sidebar(int(_ui_pref("sidebar/width", Sidebar.WIDTH) or Sidebar.WIDTH))
        self._sidebar_anim: QVariantAnimation | None = None
        self.sidebar_handle.resized.connect(self._drag_sidebar)
        self.sidebar_handle.released.connect(self._drop_sidebar)
        self.sidebar_handle.toggled.connect(self.toggle_sidebar)
        # Shown at the left edge while the sidebar is hidden.
        self.sidebar_reveal = CapsuleButton("", "secondary", "chevron-right", root)
        self.sidebar_reveal.setToolTip("Show the sidebar (⌘\\)")
        self.sidebar_reveal.setAccessibleName("Show the sidebar")
        self.sidebar_reveal.clicked.connect(self.toggle_sidebar)
        self.sidebar_reveal.hide()
        self.nav = self.sidebar.nav
        self.scan = self.sidebar.scan
        self.scan.clicked.connect(self.scan_requested)
        self.sidebar.jarvis_status.clicked.connect(self._jarvis_menu)

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
        self.sidebar.setFixedWidth(self._sidebar_width)
        if str(_ui_pref("sidebar/hidden", "0")) in ("1", "true"):
            self.set_sidebar_hidden(True, animate=False)

    # ------------------------------------------------------------ sidebar
    @property
    def sidebar_hidden(self) -> bool:
        return not self.sidebar.isVisible() if self.isVisible() else self.sidebar.isHidden()

    def _clamp_sidebar(self, width: int) -> int:
        return max(Sidebar.MIN_WIDTH, min(Sidebar.MAX_WIDTH, int(width)))

    def _drag_sidebar(self, width: int) -> None:
        if self.sidebar.isHidden():
            if width < Sidebar.COLLAPSE_AT:
                return
            self.set_sidebar_hidden(False, animate=False)
        self.sidebar.setFixedWidth(self._clamp_sidebar(width))

    def _drop_sidebar(self, width: int) -> None:
        if width < Sidebar.COLLAPSE_AT:
            self.set_sidebar_hidden(True)
            return
        self._sidebar_width = self._clamp_sidebar(width)
        self.sidebar.setFixedWidth(self._sidebar_width)
        _save_ui_pref("sidebar/width", self._sidebar_width)

    def toggle_sidebar(self) -> None:
        self.set_sidebar_hidden(not self.sidebar.isHidden())

    def set_sidebar_hidden(self, hidden: bool, animate: bool = True) -> None:
        """Hide the sidebar so the current page takes the whole window, or bring it back."""
        if self._sidebar_anim is not None:
            self._sidebar_anim.stop()
            self._sidebar_anim = None
        _save_ui_pref("sidebar/hidden", "1" if hidden else "0")
        if hasattr(self, "toggle_sidebar_action"):
            self.toggle_sidebar_action.setText("Show Sidebar" if hidden else "Hide Sidebar")
        start = self.sidebar.width() if self.sidebar.isVisible() else 0
        end = 0 if hidden else self._sidebar_width

        def finish() -> None:
            self._sidebar_anim = None
            self.sidebar.setVisible(not hidden)
            self.sidebar_handle.setVisible(not hidden)
            self.sidebar.setFixedWidth(self._sidebar_width)
            # Hidden, a narrow gutter holds the button that brings the sidebar back.
            edge = 8 if self._native_chrome else 10
            self._outer.setContentsMargins(46 if hidden else edge, edge, 0, edge)
            self._place_reveal()

        if not animate or reduced_motion() or not self.isVisible():
            finish()
            return
        self.sidebar.show()
        self.sidebar.setFixedWidth(max(1, start))
        animation = QVariantAnimation(self)
        animation.setDuration(Motion.BASE)
        animation.setStartValue(max(1, start))
        animation.setEndValue(max(1, end))
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.valueChanged.connect(lambda value: self.sidebar.setFixedWidth(max(1, int(value))))
        animation.finished.connect(finish)
        self._sidebar_anim = animation
        animation.start()

    def _place_reveal(self) -> None:
        hidden = self.sidebar.isHidden()
        self.sidebar_reveal.setVisible(hidden)
        if hidden:
            top = (TITLE_BAR_HEIGHT if self._native_chrome else 12) + 6
            self.sidebar_reveal.adjustSize()
            self.sidebar_reveal.setFixedSize(32, 32)
            self.sidebar_reveal.move(8, top)
            self.sidebar_reveal.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.sidebar.isHidden():
            self._place_reveal()

    # ------------------------------------------------------------ JARVIS
    JARVIS_MODES = (
        ("off", "Off", "Your ordinary automation settings", "disabled"),
        ("new", "New", "Postings from the last 24 hours, saved answers only", "green"),
        ("all", "All", "New and older open postings, AI Autofill sent without review. Gentle on your Mac", "accent"),
        ("all_plus", "All+", "ALL with a wider net at full speed, for overnight", "red"),
    )

    def _jarvis_menu(self) -> None:
        popover = ChoicePopover(self, "JARVIS", list(self.JARVIS_MODES), self._jarvis_mode,
                                footer="JARVIS settings…")
        popover.chosen.connect(self.choose_jarvis_mode)
        popover.footer_chosen.connect(self.show_jarvis_settings)
        self._jarvis_popover = popover
        popover.popup(self.sidebar.jarvis_status)

    def _confirm_jarvis_all(self, mode: str = "all") -> bool:
        from .views.jarvis_sheet import JarvisConfirmSheet
        sheet = JarvisConfirmSheet(mode, self)
        self._jarvis_sheet = sheet
        return sheet.exec() == QDialog.DialogCode.Accepted

    def choose_jarvis_mode(self, mode: str) -> None:
        """Change JARVIS from the sidebar. ALL and ALL+ ask once, since they send without review."""
        if mode not in ("off", "new", "all", "all_plus") or mode == self._jarvis_mode:
            return
        # Moving between ALL and ALL+ keeps the permission already given; ALL+ still shows its pace.
        if mode in ("all", "all_plus") and not (self._jarvis_mode in ("all", "all_plus") and mode == "all") \
                and not self._confirm_jarvis_all(mode):
            return
        self._jarvis_mode = mode
        self._update_jarvis_status()
        control = self.settings.jarvis_mode
        with self.settings.tracker.suspended():
            control.setCurrentIndex(max(0, control.findData(mode)))
        self.jarvis_mode_requested.emit(mode)

    def show_jarvis_settings(self) -> None:
        self.nav.setCurrentRow(SETTINGS)
        self.settings.show_jarvis()

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
        self.toggle_sidebar_action = action("Hide Sidebar", "Ctrl+\\", self.toggle_sidebar)
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
            view_menu.addAction(self.toggle_sidebar_action)
            view_menu.addSeparator()
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
        self._scheduler_enabled = scheduler_enabled
        self._update_jarvis_status()
        automation = (f"JARVIS {jarvis_label(self._jarvis_mode)} on" if self._jarvis_mode != "off"
                      else "Auto-apply on" if self._auto_apply else "Auto-apply off")
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

    def _update_jarvis_status(self) -> None:
        """Keep the saved JARVIS mode visible independently of page and scan activity."""
        mode = self._jarvis_mode
        text = f"JARVIS · {jarvis_label(mode)}"
        status = ""
        if mode == "off":
            detail = "JARVIS is off. Ordinary automatic applications follow your saved settings."
        else:
            detail = {"new": "Recent postings using saved answers.",
                      "all": "Recent postings and strong matches, including automatic AI Autofill and sending, "
                             "at background priority.",
                      }.get(mode, "Everything ALL does with a wider net at full speed, for overnight runs.")
            status = "applied"
            if not self._scheduler_enabled or not self._setup_complete:
                text += " · Paused"
                status = "needs_human"
                detail += " Background work is paused until scheduled scans and setup are enabled."
            elif self._jarvis_practice:
                text += " · Practice"
                status = "dry_run"
                detail += " Practice mode prevents submission."
        text += "  ▾"
        pill = self.sidebar.jarvis_status
        if pill.text() != text or pill.property("jarvisTone") != status:
            pill.set_status(status, text)
            pill.setProperty("jarvisTone", status)
        pill.setToolTip(detail + " Click to change the mode.")

    # -------------------------------------------------------------- data
    def set_data(
        self, *, profile=None, settings=None, jobs=None, total_jobs=None,
        applications=None, stats=None, events=None, top_jobs=None,
        drafts=None, tracker=None, resume_files=None, mail=None, inbox=None,
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
            mode = settings.get("jarvis_mode", "off")
            self._jarvis_mode = mode if mode in ("new", "all", "all_plus") else "off"
            self._jarvis_practice = bool(settings.get("dry_run"))
            self._update_jarvis_status()
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
        if applications is not None:
            self._applications = applications
        known_applications = getattr(self, "_applications", None) or []
        retryable = self._retryable(known_applications)
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
            self._draft_counts = counts
        if tracker is not None:
            self.tracker.set_rows(tracker)
            self._tracker_focus = [row for row in tracker if row.get("stage") in ("assessment", "interview", "offer")]
        if inbox is not None:
            # Assessments, interviews, and offers in mail that no tracked application matched.
            self._inbox_focus = [{"id": f"mail-{row['id']}", "company": row.get("employer") or row.get("sender") or "Employer",
                                  "title": row.get("subject") or "", "stage": row.get("stage"), "from_inbox": True,
                                  "stage_mail_message_id": row.get("message_id"), "stage_mail_sender": row.get("sender"),
                                  "stage_mail_subject": row.get("subject")} for row in inbox]
        if tracker is not None or inbox is not None or drafts is not None:
            focus = getattr(self, "_tracker_focus", []) + getattr(self, "_inbox_focus", [])
            self.nav.set_badge(TRACKER, str(len(focus)) if focus else "", "amber")
            counts = getattr(self, "_draft_counts", None)
            if counts is not None:
                self.dashboard.set_pipeline(counts, focus)
        if events is not None:
            self.dashboard.set_events(events, known_applications)
            self.sidebar.console.add_events(events, known_applications)

    @staticmethod
    def _retryable(applications: list[dict[str, Any]]) -> set[str]:
        return retryable_job_ids(applications)

    def show_setup(self, profile=None, settings=None) -> None:  # noqa: D102
        wizard = SetupWizard(profile, settings, self)
        wizard.completed.connect(self.setup_completed)
        wizard.exec()


StarploymentWindow = JobHubWindow
