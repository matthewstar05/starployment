"""Jobs bulk preparation and effective-star exclusions, with isolated data."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from jobhub import db, prepare
from jobhub.models import Job, Profile


def postings(monkeypatch):
    from jobhub import priority

    monkeypatch.setattr(priority, "is_fortune_500", lambda name: name == "Large Example")
    monkeypatch.setattr(priority, "is_fortune_1000", lambda name: name == "Growing Example")
    db.set_settings({"priority_companies": ["Priority Example"]})
    rows = [Job(source="fixture", ats="greenhouse", company=company, title="Analyst",
                url=f"https://example.test/jobs/{i}", score=99 - i)
            for i, company in enumerate(["Manual Example", "Priority Example", "Large Example", "Growing Example",
                                         "Unstarred Example", "Ordinary Example", "Another Example"])]
    db.upsert_jobs(rows)
    db.update_scores({job.id: (job.score, {}) for job in rows})
    db.set_job_star(rows[0].id, True)
    return rows


def test_star_exclusion_applies_before_counts_pagination_and_bulk_limit(store, monkeypatch):
    rows = postings(monkeypatch)
    assert db.count_jobs(exclude_starred=True) == 3
    assert [j.id for j in db.list_jobs(limit=2, exclude_starred=True)] == [j.id for j in rows[4:6]]
    assert [j.id for j in db.list_jobs(limit=2, offset=2, exclude_starred=True)] == [rows[6].id]
    assert db.fresh_counts(exclude_starred=True)["preparable_0"] == 3
    assert [j.id for j in prepare.plan_candidates(2, 0)] == [j.id for j in rows[4:6]]
    assert len(prepare.plan_candidates(10, 0, exclude_starred=False)) == 7
    db.set_job_star(rows[2].id, False)  # an explicitly removed automatic star is no longer visible
    assert db.count_jobs(exclude_starred=True) == 4
    assert prepare.plan_candidates(1, 0)[0].id == rows[2].id


def test_bulk_preparation_follows_saved_toggle_and_checks_new_stars(store, make_pdf, monkeypatch):
    rows = postings(monkeypatch)
    resume = make_pdf(store / "resume.pdf", ["Alex Example", "Analyst"])
    db.save_profile(Profile(first_name="Alex", last_name="Example", resume_path=str(resume)))
    seen = []

    def fake_prepare(job, profile, settings):
        seen.append(job.id)
        return db.upsert_draft(job.id, status="ready")

    monkeypatch.setattr(prepare, "prepare_job", fake_prepare)
    report = prepare.prepare_batch(2, 0, use_luna=False,
                                   progress=lambda *_: db.set_job_star(rows[4].id, True))
    assert report["prepared"] == 1 and seen == [rows[5].id]
    db.set_settings({"apply_plan": {"exclude_starred": False}})
    assert db.get_settings()["apply_plan"]["exclude_starred"] is False
    report = prepare.prepare_batch(1, 0, use_luna=False)
    assert report["prepared"] == 1 and seen[-1] == rows[0].id
    with pytest.raises(ValidationError):
        db.set_settings({"apply_plan": {"exclude_starred": "off"}})


def test_jobs_filter_handles_all_star_types_and_updates_when_unstarred(app, store, monkeypatch):
    from jobhub.desktop.views.jobs import JobsView

    rows = postings(monkeypatch)
    view = JobsView()
    view.set_priority(["Priority Example"])
    view.set_jobs(db.list_jobs())
    view.page = 2
    view.exclude_starred_toggle.setChecked(True)
    assert view.page == 0
    assert [view.table.isRowHidden(i) for i in range(7)] == [True] * 4 + [False] * 3
    db.set_job_star(rows[0].id, False)
    view.set_jobs(db.list_jobs())
    assert not view.table.isRowHidden(0)
    view.exclude_starred_toggle.setChecked(False)
    assert not any(view.table.isRowHidden(i) for i in range(7))


def test_jobs_button_and_main_menu_open_the_shared_plan_and_persist_toggle(app, store, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from jobhub import scheduler
    from jobhub.desktop.controller import DesktopController
    from jobhub.desktop.ui import JOBS, JobHubWindow
    from jobhub.desktop.views.dashboard import ApplyPlanSheet

    rows = postings(monkeypatch)
    db.set_settings({"setup_complete": True, "apply_plan": {"exclude_starred": False}})
    window = JobHubWindow()
    controller = DesktopController(window, scheduler_enabled=False, show_setup_on_start=False)
    seen = []
    monkeypatch.setattr(prepare, "prepare_batch", lambda *a, **kw: seen.append(kw) or {"prepared": 0})
    monkeypatch.setattr(controller, "_submit_assist", lambda kind, function, *a: function())
    opened = []

    def accept(sheet):
        opened.append(sheet.plan())
        assert sheet.exclude_starred.isChecked()
        assert "3 new roles" in sheet.available.text()
        if len(opened) == 2:
            sheet.exclude_starred.setChecked(False)
            assert "7 new roles" in sheet.available.text()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ApplyPlanSheet, "exec", accept)
    try:
        window.nav.setCurrentRow(JOBS)
        window.jobs.exclude_starred_toggle.setChecked(True)
        window.jobs.bulk_prepare.click()
        assert seen[-1]["exclude_starred"] is True
        assert db.get_settings()["apply_plan"]["exclude_starred"] is True
        controller._preparing = False
        window.jobs.exclude_starred_toggle.setChecked(False)
        controller.refresh(include_account=True)
        window.apply_action.trigger()
        assert len(opened) == 2 and seen[-1]["exclude_starred"] is False
        assert db.get_settings()["apply_plan"]["exclude_starred"] is False
        assert len(window.jobs.jobs) == len(rows)
    finally:
        controller.stop()
        scheduler.STATE.stop = False


def test_bulk_preparation_reads_forms_side_by_side(store, make_pdf, monkeypatch):
    import threading
    import time

    rows = postings(monkeypatch)
    resume = make_pdf(store / "resume.pdf", ["Alex Example", "Analyst"])
    db.save_profile(Profile(first_name="Alex", last_name="Example", resume_path=str(resume)))
    db.set_settings({"parallel_prepare": 3})
    active, peak, lock = 0, 0, threading.Lock()

    def slow_prepare(job, profile, settings):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.2)
        with lock:
            active -= 1
        return db.upsert_draft(job.id, status="ready")

    monkeypatch.setattr(prepare, "prepare_job", slow_prepare)
    report = prepare.prepare_batch(3, 0, use_luna=False)
    assert report["prepared"] == 3 and peak == 3
    with pytest.raises(ValidationError):
        db.set_settings({"parallel_prepare": 0})
