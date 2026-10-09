"""Desktop: posting dates, sorting, the posting window, stars, mark as sent, and removing from Applications."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("JOBHUB_REDUCED_MOTION", "1")


def ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def posting(slug: str, number: int, *, company: str = "Example", hours: float | None = 2, score: float = 80):
    from jobhub.models import Job
    return Job(source="greenhouse", ats="greenhouse", company=company, title=f"Software Engineer {number}",
               url=f"https://job-boards.greenhouse.io/{slug}/jobs/{number}", location="Remote",
               posted_at=ago(hours) if hours is not None else None, score=score, discovered_at=ago(1))


@pytest.fixture
def controller(store, app, monkeypatch):
    from jobhub import db, scheduler
    from jobhub.desktop.controller import DesktopController
    from jobhub.desktop.ui import JobHubWindow

    monkeypatch.setattr(scheduler, "rescore", lambda: None)
    db.set_settings({"setup_complete": True, "priority_companies": ["Google"]})
    window = JobHubWindow()
    instance = DesktopController(window, scheduler_enabled=False, show_setup_on_start=False)
    yield instance
    instance.stop()
    window.close()
    window.deleteLater()
    scheduler.STATE.stop = False


# ------------------------------------------------------------------ copy
def test_posted_label_shows_time_only_when_the_source_gave_one():
    from jobhub.desktop.format import posted_label, posted_within
    now = datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc)
    when, age = posted_label("2026-10-03T17:30:00Z", now)
    assert age == "30m ago" and ":" in when
    assert posted_label("2026-10-02", now)[1] == "Yesterday"
    assert ":" not in posted_label("2026-10-02", now)[0]
    assert posted_label("2025-02-24T22:18:51+00:00", now)[0].endswith("2025")
    assert posted_label(None, now) == ("—", "")
    assert posted_within("2026-10-03T09:00:00Z", 24, now) and not posted_within("2026-09-30", 24, now)


# ------------------------------------------------------------------ Jobs
def test_jobs_table_shows_posting_dates_stars_and_sorts_by_header(app):
    from jobhub.desktop.delegates import POSTED_AT_ROLE, STAR_ROLE
    from jobhub.desktop.views.jobs import JobsView

    view = JobsView()
    view.set_priority(["PriorityCo"])
    priority_job, other = posting("p", 1, company="PriorityCo"), posting("example", 2, hours=None)
    view.set_jobs([priority_job, other])
    assert view.table.horizontalHeaderItem(JobsView.POSTED).text() == "POSTED"
    assert view.table.item(0, JobsView.POSTED).data(POSTED_AT_ROLE) == priority_job.posted_at
    assert view.table.item(0, JobsView.ROLE).data(STAR_ROLE) == "amber"
    assert view.table.item(1, JobsView.ROLE).data(STAR_ROLE) is False
    assert view.table.item(1, JobsView.POSTED).text() == "—"

    emitted = []
    view.query_changed.connect(lambda: emitted.append(True))
    view._header_clicked(JobsView.POSTED)
    assert (view.sort_key, view.sort_descending) == ("posted", True)
    assert view.sort.value() == "posted"
    view._header_clicked(JobsView.POSTED)
    assert view.sort_descending is False
    view.sort.choose("company")
    assert (view.sort_key, view.sort_descending) == ("company", False)
    view._query_timer.timeout.emit()
    assert emitted

    view.set_priority([])                                     # star follows the list
    assert view.table.item(0, JobsView.ROLE).data(STAR_ROLE) is False
    view.close()
    view.deleteLater()


def test_posting_window_hides_older_rows_until_the_query_returns(app):
    from jobhub.desktop.views.jobs import JobsView

    view = JobsView()
    view.set_jobs([posting("a", 1, hours=3), posting("a", 2, hours=50), posting("a", 3, hours=None)])
    view.set_posted_window(24)
    assert [view.table.isRowHidden(row) for row in range(3)] == [False, True, True]
    assert view.posted_hours == 24
    view.set_jobs([], total_count=0)
    assert "24 hours" in view.header.subtitle_label.text()
    view.close()
    view.deleteLater()


def test_controller_queries_jobs_with_the_window_and_sort(controller):
    from jobhub import db

    db.upsert_jobs([posting("a", 1, hours=3, score=50), posting("a", 2, hours=40, score=90)])
    jobs = controller.window.jobs
    jobs.set_posted_window(24)
    controller.refresh()
    assert [job.title for job in jobs.jobs] == ["Software Engineer 1"]
    jobs.set_posted_window(0)
    jobs.set_sort("posted")
    controller.refresh()
    assert [job.title for job in jobs.jobs] == ["Software Engineer 1", "Software Engineer 2"]
    controller.window.show_jobs("fresh")
    assert jobs.posted_hours == 24 and jobs.selected_status == "new"
    controller.window.show_jobs("new")
    assert jobs.posted_hours == 0


def test_fresh_tile_and_hero_count_postings_from_the_last_day(controller):
    from jobhub import db

    db.upsert_jobs([posting("a", 1, hours=3), posting("b", 2, hours=60)])
    controller.refresh()
    dashboard = controller.window.dashboard
    assert dashboard.tiles["fresh_24"].value.value() == 1
    assert "1 posted in the last 24 hours" in dashboard.hero.stats.text()
    dashboard.tiles["fresh_24"].clicked.emit()
    assert controller.window.jobs.posted_hours == 24


def test_apply_plan_sheet_offers_a_posting_window_and_order(app):
    from jobhub import config
    from jobhub.desktop.views.dashboard import ApplyPlanSheet

    settings = config.merge_settings(config.DEFAULT_SETTINGS, {"apply_plan": {"posted_within_hours": 24,
                                                                              "order": "newest", "count": 50}})
    sheet = ApplyPlanSheet(settings, 120, counts={"preparable_0": 80, "preparable_24": 12})
    plan = sheet.plan()
    assert plan["posted_within_hours"] == 24 and plan["order"] == "newest" and plan["count"] == 50
    assert "12 new roles" in sheet.available.text() and "24 hours" in sheet.available.text()
    sheet.window_choice.setCurrentIndex(sheet.window_choice.findData(0))
    assert "80 new roles" in sheet.available.text()
    sheet.close()


def test_start_plan_saves_and_passes_the_window(controller, monkeypatch):
    from jobhub import db, prepare

    seen = {}
    monkeypatch.setattr(prepare, "prepare_batch", lambda *args, **kwargs: seen.update(kwargs) or {"prepared": 0})
    monkeypatch.setattr(controller, "_submit_assist", lambda kind, function, context=None, *a: function())
    controller.start_plan({"count": 50, "min_score": 60, "one_per_company": True, "use_luna": False,
                           "posted_within_hours": 24, "order": "newest"})
    assert seen["posted_within_hours"] == 24 and seen["order"] == "newest"
    assert db.get_settings()["apply_plan"]["posted_within_hours"] == 24


# ------------------------------------------------------------------ Applications
def _records():
    return [
        {"kind": "draft", "id": 1, "status": "ready", "company": "Beta", "updated_at": ago(1), "score": 70,
         "posted_at": ago(30)},
        {"kind": "attempt", "id": 2, "status": "applied", "company": "Alpha", "finished_at": ago(5), "score": 90,
         "posted_at": ago(2)},
        {"kind": "draft", "id": 3, "status": "approved", "company": "Gamma", "updated_at": ago(3), "score": 60,
         "posted_at": None},
    ]


def test_application_sort_orders(app):
    from jobhub.desktop.views.applications import sort_records

    ids = lambda key: [r["id"] for r in sort_records(_records(), key)]  # noqa: E731
    assert ids("recent") == [1, 3, 2]
    assert ids("posted") == [2, 1, 3]        # unknown posting dates last
    assert ids("match") == [2, 1, 3]
    assert ids("company") == [2, 1, 3]
    assert ids("status") == [1, 3, 2]        # review, approved, then sent


def test_applications_star_filter_hidden_rows_and_send_ready_count(app):
    from jobhub.desktop.views.applications import ApplicationsView

    view = ApplicationsView()
    view.set_context(priority=["Google"])
    drafts = [{"id": 1, "job_id": "g", "status": "ready", "company": "Google", "title": "SWE", "questions": []},
              {"id": 2, "job_id": "e", "status": "ready", "company": "Example", "title": "SWE", "questions": []}]
    attempts = [{"id": 7, "job_id": "x", "status": "applied", "company": "Hidden Co", "title": "SWE", "hidden": 1},
                {"id": 8, "job_id": "y", "status": "needs_human", "company": "Shown Co", "title": "SWE", "hidden": 0}]
    view.set_applications(attempts, drafts)
    companies = {view.list.item(row).data(0x0100)["company"] for row in range(view.list.count())}
    assert companies == {"Google", "Example", "Shown Co"}
    assert view.send_all_button.text() == "Send ready (1)"      # the starred Google draft waits
    view.show_filter("starred")
    assert [view.list.item(row).data(0x0100)["company"] for row in range(view.list.count())] == ["Google"]
    assert view.draft_detail.starred_note.isVisibleTo(view.draft_detail)
    assert view.draft_detail.star.text() == "Starred"
    toggled = []
    view.star_requested.connect(lambda job_id, value: toggled.append((job_id, value)))
    view.draft_detail.star.click()
    assert toggled == [("g", False)]
    assert "1 application" in view.list_count.text()
    view.close()
    view.deleteLater()


def test_context_actions_confirm_then_emit(app, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from jobhub.desktop.views.applications import ApplicationsView

    view = ApplicationsView()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    sent, hidden, discarded = [], [], []
    view.mark_sent_requested.connect(sent.append)
    view.hide_requested.connect(hidden.append)
    view.discard_draft_requested.connect(discarded.append)
    view._confirm_mark_sent({"kind": "draft", "id": 4, "job_id": "job-4", "company": "Example"})
    view._confirm_hide({"kind": "attempt", "id": 9, "status": "applied"})
    view._discard(4)
    assert (sent, hidden, discarded) == (["job-4"], [9], [4])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    view._confirm_mark_sent({"kind": "draft", "id": 5, "job_id": "job-5"})
    assert sent == ["job-4"]
    view.close()
    view.deleteLater()


def test_controller_star_mark_sent_hide_and_discard(controller):
    from jobhub import db

    google, example, other = posting("google", 1, company="Google"), posting("example", 2), posting("other", 3)
    db.upsert_jobs([google, example, other])
    draft = db.upsert_draft(example.id, status="ready")
    discard = db.upsert_draft(other.id, status="ready")

    controller.set_star(example.id, True)
    assert db.get_job(example.id).starred is True
    controller.mark_sent(example.id)
    assert db.get_draft(draft)["status"] == "sent" and db.get_job(example.id).status == "applied"
    record = next(a for a in db.list_applications() if a["job_id"] == example.id)
    assert record["method"] == "manual"
    assert controller.window.tracker.rows and controller.window.sidebar.console.last_tone == "success"
    controller.mark_sent(example.id)
    assert controller.window.sidebar.console.last_tone == "warning"     # already sent

    controller.hide_application(record["id"])
    assert db.get_application(record["id"])["hidden"] == 1
    assert all(r.get("id") != record["id"] for r in controller.window.apps.records if r["kind"] == "attempt")

    controller.discard_draft(discard)
    assert db.get_draft(discard) is None and db.get_job(other.id).status == "skipped"


def test_settings_page_edits_priority_companies_and_discovery(app):
    from jobhub import config
    from jobhub.desktop.views.settings import SettingsView

    view = SettingsView()
    view.set_settings(config.merge_settings(config.DEFAULT_SETTINGS, {"priority_companies": ["Google"],
                                                                      "discover_boards": False}))
    assert view.priority_companies.values() == ["Google"] and not view.discover_boards.isChecked()
    view._restore_priority()
    assert view.dirty and "Jane Street" in view.priority_companies.values()
    view.discover_boards.setChecked(True)
    patches = []
    view.save_requested.connect(patches.append)
    view._save()
    assert patches[0]["discover_boards"] is True
    assert patches[0]["priority_companies"] == config.PRIORITY_COMPANIES
    view.set_board_count(42)
    assert "42 found so far" in view._discover_row.findChild(type(view.header.subtitle_label), "rowNote").text()
    view.close()
    view.deleteLater()


def test_unpreparable_toggle_and_action_header(app):
    from jobhub.desktop.views.jobs import JobsView
    from jobhub.models import Job

    view = JobsView()
    # ATS greenhouse -> kind is 'apply' (preparable)
    preparable = posting("greenhouse-co", 1, company="ApplyCo")
    # ATS other -> kind is 'view' (unpreparable)
    unpreparable = Job(source="custom", ats="custom", company="ViewCo", title="Software Engineer 2",
                       url="https://example.com/jobs/2", location="Remote", score=80)
    view.set_jobs([preparable, unpreparable])

    # 1. When unpreparable_toggle is checked (True), both rows are visible
    assert view.unpreparable_toggle.isChecked() is True
    assert not view.table.isRowHidden(0)
    assert not view.table.isRowHidden(1)

    # 2. When unpreparable_toggle is toggled to False, row with kind=='view' is hidden, kind=='apply' remains visible
    view.unpreparable_toggle.setChecked(False)
    assert not view.table.isRowHidden(0)
    assert view.table.isRowHidden(1)

    # 3. When toggled back to True, row with kind=='view' is restored
    view.unpreparable_toggle.setChecked(True)
    assert not view.table.isRowHidden(0)
    assert not view.table.isRowHidden(1)

    # 4. Clicking the ACTION header toggles unpreparable_toggle
    view._header_clicked(JobsView.ACTION)
    assert view.unpreparable_toggle.isChecked() is False
    assert view.table.isRowHidden(1)

    view._header_clicked(JobsView.ACTION)
    assert view.unpreparable_toggle.isChecked() is True
    assert not view.table.isRowHidden(1)
    view.close()
    view.deleteLater()

