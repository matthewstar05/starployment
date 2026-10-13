"""Paths that keep the desktop responsive on a large job library.

The job version counter lets refreshes skip unchanged libraries, discovery
upserts and re-scoring skip rows that would not change, and the skill scanner
finds the same skills as one search per skill. Every store is temporary.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from jobhub import config, db, scoring
from jobhub.models import Job


@pytest.fixture
def store(tmp_path, monkeypatch):
    db.close()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    db.init()
    yield tmp_path
    db.close()


def posting(number: int, **extra) -> Job:
    values = {"source": "greenhouse", "ats": "greenhouse", "company": "Example", "title": "Software Engineer",
              "url": f"https://job-boards.greenhouse.io/example/jobs/{number}", "description": "Python services",
              "location": "United States", "posted_at": "2026-10-01T12:00:00+00:00"}
    return Job(**{**values, **extra})


def test_unchanged_upsert_and_scores_leave_jobs_untouched(store):
    jobs = [posting(1), posting(2)]
    db.upsert_jobs(jobs)
    db.update_scores({job.id: (50.0, {"total": 50.0}) for job in jobs})
    version = db.jobs_version()

    assert db.upsert_jobs([posting(1), posting(2)]) == 0
    db.update_scores({job.id: (50.0, {"total": 50.0}) for job in jobs})
    assert db.jobs_version() == version

    db.upsert_jobs([posting(1, description="Python and SQL services")])
    assert db.jobs_version() > version
    assert db.get_job(jobs[0].id).description == "Python and SQL services"
    version = db.jobs_version()
    db.update_scores({jobs[1].id: (60.0, {"total": 60.0})})
    assert db.jobs_version() > version
    assert db.get_job(jobs[1].id).score == 60.0


def test_empty_discovered_fields_keep_stored_values(store):
    db.upsert_jobs([posting(1, salary_min=100_000)])
    version = db.jobs_version()
    db.upsert_jobs([posting(1, description="", salary_min=None)])
    assert db.jobs_version() == version
    assert db.get_job(posting(1).id).salary_min == 100_000


def test_list_jobs_pages_in_sort_order(store):
    jobs = [posting(number) for number in range(1, 8)]
    db.upsert_jobs(jobs)
    db.update_scores({job.id: (float(number), {}) for number, job in enumerate(jobs)})
    ranked = [job.id for job in reversed(jobs)]
    assert [job.id for job in db.list_jobs(limit=3)] == ranked[:3]
    assert [job.id for job in db.list_jobs(limit=3, offset=3)] == ranked[3:6]
    assert [job.id for job in db.list_jobs(limit=100, descending=False)] == ranked[::-1]


def test_skill_scanner_matches_one_search_per_skill():
    skills = ("java", "javascript", "c++", "sql", "machine learning", "learning", "git", "github", "node.js")
    for text in ("javascript and java on github", "we use c++, sql and git.", "machine learning with node.js",
                 "no match here: javas, sqlite, legit", "learning"):
        expected = [s for s in skills if scoring._skill_pattern(s).search(text)]
        assert scoring._skill_hits(skills, text) == expected


def test_refresh_reuses_job_queries_until_a_job_changes(store, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from jobhub.desktop.controller import DesktopController
    from jobhub.desktop.ui import JobHubWindow

    app = QApplication.instance() or QApplication([])
    db.upsert_jobs([posting(1)])
    window = JobHubWindow()
    controller = DesktopController(window, scheduler_enabled=False, show_setup_on_start=False)
    try:
        calls = []
        original = db.count_jobs
        monkeypatch.setattr(db, "count_jobs", lambda *a, **k: calls.append(1) or original(*a, **k))
        controller.refresh()
        controller.refresh()
        assert calls == []
        db.add_event("Unrelated activity")
        controller.refresh()
        assert calls == []

        db.upsert_jobs([posting(2)])
        controller.refresh()
        assert len(calls) == 1
        assert window.jobs.total_jobs == 2
    finally:
        controller.stop()
        window.close()
