"""Behavior of the Apple-style desktop primitives and refresh-safe views.

Animations are exercised with reduced motion forced on or off so the tests
stay deterministic: the end state must be identical either way.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    from jobhub.desktop.styles import apply_appearance

    application = QApplication.instance() or QApplication([])
    apply_appearance(application, "Light")
    return application


def _settle(app, ms: int = 0) -> None:
    from PySide6.QtTest import QTest

    app.processEvents()
    if ms:
        QTest.qWait(ms)


def test_qcolor_parses_css_rgba_and_blend_is_linear():
    from jobhub.desktop.widgets import blend, qcolor

    color = qcolor("rgba(10,20,30,0.5)")
    assert (color.red(), color.green(), color.blue()) == (10, 20, 30)
    assert abs(color.alphaF() - 0.5) < 0.01
    middle = blend("#000000", "#FFFFFF", 0.5)
    assert 126 <= middle.red() <= 129


def test_reduced_motion_environment_override(monkeypatch):
    from jobhub.desktop.widgets import reduced_motion

    monkeypatch.setenv("JOBHUB_REDUCED_MOTION", "1")
    assert reduced_motion() is True
    monkeypatch.setenv("JOBHUB_REDUCED_MOTION", "0")
    assert reduced_motion() is False


def test_toggle_switch_is_a_checkbox_and_lands_on_its_end_state(app, monkeypatch):
    from jobhub.desktop.widgets import ToggleSwitch

    monkeypatch.setenv("JOBHUB_REDUCED_MOTION", "0")
    switch = ToggleSwitch("Include remote roles")
    switch.resize(320, 30)
    switch.show()
    _settle(app)
    switch.setChecked(True)
    _settle(app, 800)
    assert switch.isChecked()
    assert switch._position == pytest.approx(1.0)
    switch.click()
    _settle(app, 800)
    assert not switch.isChecked()
    assert switch._position == pytest.approx(0.0)
    switch.close()


def test_segmented_control_mirrors_combo_box_api(app):
    from jobhub.desktop.widgets import SegmentedControl

    control = SegmentedControl()
    for text, value in (("Automatic", "System"), ("Light", "Light"), ("Dark", "Dark")):
        control.addItem(text, value)
    changes, activations = [], []
    control.currentIndexChanged.connect(changes.append)
    control.activated.connect(activations.append)
    assert control.currentIndex() == 0 and control.currentData() == "System"
    control.setCurrentIndex(control.findData("Dark"))
    assert control.currentText() == "Dark"
    assert changes == [2]
    assert activations == []  # programmatic changes are not user activations
    control.setCurrentText("Light")
    assert control.currentData() == "Light"


def test_fade_stack_removes_its_opacity_effect_after_the_transition(app, monkeypatch):
    from PySide6.QtWidgets import QLabel

    from jobhub.desktop.widgets import FadeStack, Motion

    monkeypatch.setenv("JOBHUB_REDUCED_MOTION", "0")
    stack = FadeStack()
    pages = [QLabel(f"Page {n}") for n in range(3)]
    for page in pages:
        stack.addWidget(page)
    stack.resize(300, 200)
    stack.show()
    _settle(app)
    stack.setCurrentIndex(2)
    assert stack.currentIndex() == 2
    assert pages[2].graphicsEffect() is not None
    _settle(app, Motion.PAGE + 700)
    assert pages[2].graphicsEffect() is None
    assert pages[2].pos() == stack.contentsRect().topLeft()
    stack.close()


def test_count_up_label_reaches_the_exact_value(app, monkeypatch):
    from jobhub.desktop.widgets import CountUpLabel, Motion

    monkeypatch.setenv("JOBHUB_REDUCED_MOTION", "0")
    number = CountUpLabel(0)
    number.show()
    number.set_value(1234)
    _settle(app, Motion.COUNT + 600)
    assert number.text() == "1,234"
    number.close()


def test_toast_shows_replaces_and_dismisses(app, monkeypatch):
    from PySide6.QtWidgets import QWidget

    from jobhub.desktop.widgets import Toast

    monkeypatch.setenv("JOBHUB_REDUCED_MOTION", "1")
    host = QWidget()
    host.resize(800, 500)
    host.show()
    toast = Toast(host)
    toast.show_message("Scanning…", "busy", 0)
    assert toast.isVisible() and toast.tone == "busy"
    toast.show_message("Found 12 jobs", "success", 0)
    assert toast.text == "Found 12 jobs"
    assert toast.geometry().bottom() <= host.height()
    toast.dismiss()
    assert not toast.isVisible()
    host.close()


def test_dirty_tracker_ignores_programmatic_loads(app):
    from PySide6.QtWidgets import QLineEdit

    from jobhub.desktop.widgets import DirtyTracker

    tracker = DirtyTracker()
    edit = QLineEdit()
    tracker.watch(edit)
    with tracker.suspended():
        edit.setText("loaded from the database")
    assert tracker.dirty is False
    edit.setText("typed by the candidate")
    assert tracker.dirty is True
    tracker.reset()
    assert tracker.dirty is False


def test_relative_time_and_countdown_copy():
    from jobhub.desktop.format import countdown, greeting, pretty_log_line, relative_time, salary

    now = datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc)
    assert relative_time((now - timedelta(seconds=20)).isoformat(), now) == "Just now"
    assert relative_time((now - timedelta(minutes=12)).isoformat(), now) == "12m ago"
    assert countdown(42 * 60) == "in 42 min"
    assert countdown(65 * 60) == "in 1 hr 5 min"
    assert greeting("Ada Lovelace", hour=9) == "Good morning, Ada"
    assert greeting("", hour=20) == "Good evening"
    assert salary(150_000, 190_000) == "$150k–$190k"
    assert salary(None, None) == "—"
    assert pretty_log_line("not a timestamp") == "not a timestamp"
    assert "Opened" in pretty_log_line("2026-09-26T18:53:28+00:00 Opened the form")
    assert "2026-09-26T" not in pretty_log_line("2026-09-26T18:53:28+00:00 Opened the form")


def test_jobs_refresh_keeps_selection_and_skips_identical_rebuilds(app):
    from jobhub.desktop.ui import JobsView
    from jobhub.models import Job

    jobs = [Job(source="fixture", company=f"Company {n}", title=f"Role {n}",
                url=f"https://example.test/jobs/{n}", score=90 - n) for n in range(30)]
    view = JobsView()
    view.resize(900, 600)
    view.show()
    view.set_jobs(jobs, total_count=30)
    view.table.selectRow(7)
    first_item = view.table.item(0, JobsView.ROLE)
    view.set_jobs(list(jobs), total_count=30)  # the 15-second refresh with unchanged data
    assert view.table.item(0, JobsView.ROLE) is first_item
    assert view.table.currentRow() == 7
    changed = [jobs[1], jobs[0], *jobs[2:]]
    view.set_jobs(changed, total_count=30)  # reordered: rebuilt, selection follows the job
    assert view.table.item(7, JobsView.ROLE).data(0x0100) == jobs[7].id
    assert view._selected_job_id() == jobs[7].id
    view.close()


def test_application_note_in_progress_survives_a_background_refresh(app):
    from jobhub.desktop.ui import ApplicationsView

    record = {"id": 3, "job_id": "a" * 16, "title": "Analyst", "company": "Example",
              "status": "needs_human", "unanswered": [], "candidate_note": ""}
    view = ApplicationsView()
    view.set_applications([dict(record)])
    view.note.setPlainText("Half-typed reminder")
    refreshed = dict(record, finished_at="2026-09-27T10:00:00Z")  # data changed underneath
    view.set_applications([refreshed])
    assert view.note.toPlainText() == "Half-typed reminder"
    saved = []
    view.note_save_requested.connect(lambda app_id, note: saved.append((app_id, note)))
    view._save_note()
    assert saved == [(3, "Half-typed reminder")]
    view.close()


def test_window_scan_state_and_badges(app):
    import time

    from jobhub.desktop.ui import APPLICATIONS, JOBS, JobHubWindow
    from jobhub.desktop.widgets import NAV_BADGE_ROLE

    window = JobHubWindow()
    window.set_scan_state(scanning=True, setup_complete=True)
    assert not window.scan.isEnabled()
    assert window.sidebar.status_title.text() == "Searching for jobs"
    window.set_scan_state(scanning=False, next_scan_at=time.time() + 42 * 60, auto_apply=False)
    assert window.scan.isEnabled()
    assert window.sidebar.status_title.text().startswith("Next scan in 4")
    assert window.sidebar.status_detail.text() == "Auto-apply off"
    window.set_scan_state(scanning=False, setup_complete=False)
    assert window.sidebar.status_title.text() == "Setup needed"
    window.set_data(stats={"new": 12, "applied": 1, "needs_human": 3, "total": 40})
    assert window.nav.item(JOBS).data(NAV_BADGE_ROLE) == "12"
    assert window.nav.item(APPLICATIONS).data(NAV_BADGE_ROLE) == "3"
    assert window.dashboard.tiles["new"].value.value() == 12
    window.close()


def test_dashboard_tiles_route_to_filtered_pages(app):
    from jobhub.desktop.ui import APPLICATIONS, JOBS, JobHubWindow

    window = JobHubWindow()
    window.dashboard.tiles["applied"].clicked.emit()
    assert window.stack.currentIndex() == JOBS
    assert window.jobs.selected_status == "applied"
    window.dashboard.tiles["needs_human"].clicked.emit()
    assert window.stack.currentIndex() == APPLICATIONS
    window.close()


def test_profile_edits_are_not_clobbered_by_account_refresh(app):
    from jobhub.desktop.ui import JobHubWindow

    window = JobHubWindow()
    window.set_data(profile={"first_name": "Ada", "city": "London"})
    assert window.profile.fields["city"].text() == "London"
    assert window.profile.save_button.isEnabled() is False
    window.profile.fields["city"].setText("Paris")
    assert window.profile.save_button.isEnabled() is True
    window.set_data(profile={"first_name": "Ada", "city": "London"})
    assert window.profile.fields["city"].text() == "Paris"
    window.close()
