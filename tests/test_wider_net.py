"""Liveness pruning, employer caps, role-family variety, incremental scoring, and the
read connection. Local databases only; no board or employer is contacted."""
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from jobhub import config, db, families, jarvis, scheduler
from jobhub.models import Job
from jobhub.sources import base


def posting(number, title="Software Engineer", company=None, ats="greenhouse", board="northwind", **kwargs):
    urls = {"greenhouse": f"https://job-boards.greenhouse.io/{board}/jobs/{number}",
            "lever": f"https://jobs.lever.co/{board}/{number}",
            "linkedin": f"https://www.linkedin.com/jobs/view/{number}"}
    return Job(company=company or f"Example {number}", title=title, source=ats, ats=ats, url=urls[ats],
               description="Python SQL", **kwargs)


def settings_with(**patch):
    return config.merge_settings(db.get_settings(), {"target_titles": ["software engineer", "financial analyst"],
                                                     **patch})


# ------------------------------------------------------------------ families
@pytest.mark.parametrize("title, family", [
    ("Quantitative Analyst", "Quant & trading"),
    ("Financial Data Analyst", "Finance & FP&A"),
    ("FP&A Analyst", "Finance & FP&A"),
    ("Staff Accountant", "Accounting"),
    ("Business Systems Analyst", "Business & strategy"),
    ("Actuarial Analyst", "Risk & actuarial"),
    ("Data Scientist", "Data & ML"),
    ("Backend Engineer", "Software"),
    ("Fraud Analyst, Secured Lending", "Risk & actuarial"),
    ("Analyst, Deal Operations", "Business & strategy"),
    ("Office Coordinator", "Other"),
])
def test_role_families(title, family):
    assert families.family(title) == family


def test_interleave_alternates_groups_and_keeps_each_groups_order():
    items = ["s1", "s2", "s3", "f1", "a1", "f2"]
    assert families.interleave(items, lambda item: item[0]) == ["s1", "f1", "a1", "s2", "f2", "s3"]


def test_diversify_caps_employers_and_mixes_role_families():
    jobs = [posting(1, company="Anduril Industries"), posting(2, company="Roblox"), posting(3, company="Reddit"),
            posting(4, "Financial Analyst", "Northwind"), posting(5, "Staff Accountant", "Bluebird")]
    settings = {"company_cap": {"count": 3, "days": 30}, "mix_role_families": True}
    attempts = {"anduril industries": 3, "roblox": 1}
    order = [job.company for job in jarvis.diversify(jobs, settings, attempts)]
    assert order == ["Roblox", "Northwind", "Bluebird", "Reddit"]
    assert len(jarvis.diversify(jobs, {"company_cap": {"count": 0, "days": 30}}, attempts)) == 5


def test_recent_company_attempts_count_only_attempts_that_reached_the_employer(store):
    db.upsert_jobs([posting(1, company="Anduril Industries, Inc."), posting(2, company="Anduril Industries")])
    first = db.reserve_application(posting(1).id)
    db.mark_submission_attempted(posting(1).id)
    db.finish_application(first, "applied", [], [], [], "", None, True)
    second = db.reserve_application(posting(2).id)
    db.finish_application(second, "needs_human", [], [], [], "stopped before submit", None, False)
    assert db.recent_company_attempts(30) == {"anduril industries": 1}


# ------------------------------------------------------------------ pruning
def test_closed_postings_on_a_board_read_in_full_are_removed_and_touched_ones_stay(store):
    open_, closed, starred, other_board, unwanted = (
        posting(1), posting(2), posting(3), posting(4, board="bluebird"), posting(5, "Office Coordinator"))
    drafted = posting(6)
    db.upsert_jobs([open_, closed, starred, other_board, unwanted, drafted])
    db.set_job_star(starred.id, True)
    db.upsert_draft(drafted.id, status="ready")
    started = time.time() + 1
    db.mark_seen([open_.id], started)
    pruned = db.prune_jobs(settings_with(), boards_read={"greenhouse": {"Northwind"}}, scan_started=started)
    remaining = {job.id for job in db.list_jobs(status="all", limit=100)} | {drafted.id}
    assert pruned["closed"] == 1
    assert closed.id not in remaining
    # still listed, starred, on a board not read this scan, a title the scan never looked for, prepared
    assert {open_.id, starred.id, other_board.id, unwanted.id, drafted.id} <= remaining
    assert db.get_job(drafted.id) is not None


def test_postings_no_scan_has_found_for_weeks_are_removed(store):
    stale, fresh = posting(1, ats="linkedin"), posting(2, ats="linkedin")
    db.upsert_jobs([stale, fresh])
    db.mark_seen([stale.id], time.time() - 30 * 86400)
    assert db.prune_jobs(settings_with()) == {"closed": 0, "stale": 1}
    assert db.get_job(stale.id) is None and db.get_job(fresh.id) is not None
    assert db.prune_jobs(settings_with(prune={"enabled": False, "unseen_days": 3})) == {"closed": 0, "stale": 0}


def test_board_reads_are_reported_for_liveness():
    def fetch_one(slug, settings):
        if slug == "broken":
            raise RuntimeError("offline")
        return [posting(1, board=slug)]
    result = base.fetch_boards(["northwind", "broken"], fetch_one, {"_discovered_boards": {}}, source="greenhouse",
                               discovered=["bluebird"])
    assert result.boards_read == {"northwind", "bluebird"}


def test_old_postings_still_listed_qualify_for_all_mode(store):
    listed, gone = posting(1), posting(2)
    db.upsert_jobs([listed, gone])
    db.mark_seen([gone.id], time.time() - 10 * 86400)
    assert [job.id for job in jarvis.still_listed([listed, gone])] == [listed.id]


# ------------------------------------------------------------------ scoring
def test_rescore_scores_only_changed_postings_until_preferences_change(store, monkeypatch):
    db.set_settings({"target_titles": ["software engineer"]})
    db.upsert_jobs([posting(1), posting(2)])
    scored: list[list[str]] = []
    real = scheduler.score_all
    monkeypatch.setattr(scheduler, "score_all", lambda jobs, *a: (scored.append([j.id for j in jobs]), real(jobs, *a))[1])
    monkeypatch.setattr(scheduler, "_rescored", {"key": None, "at": 0.0, "since": ""})
    time.sleep(1.1)      # changes are compared by the second
    scheduler.rescore()
    assert len(scored[-1]) == 2
    time.sleep(1.1)
    scheduler.rescore()
    assert len(scored) == 1 or scored[-1] == []      # nothing changed
    time.sleep(1.1)
    db.upsert_jobs([posting(3)])
    scheduler.rescore()
    assert scored[-1] == [posting(3).id]
    db.set_settings({"target_titles": ["software engineer", "data analyst"]})
    scheduler.rescore()
    assert len(scored[-1]) == 3


# ------------------------------------------------------------------ the read connection
def test_window_reads_do_not_wait_for_a_worker_holding_the_database(store):
    db.upsert_jobs([posting(1)])
    held, release = threading.Event(), threading.Event()

    def worker():
        with db.conn():
            held.set()
            release.wait(5)

    thread = threading.Thread(target=worker)
    thread.start()
    held.wait(5)
    started = time.monotonic()
    try:
        assert db.count_jobs(status="all") == 1
        assert time.monotonic() - started < 1
    finally:
        release.set()
        thread.join()
    db.set_settings({"min_salary": 123})
    assert db.get_settings()["min_salary"] == 123     # the reader sees committed writes


def test_settings_validate_new_keys():
    settings = config.merge_settings(config.DEFAULT_SETTINGS, {"company_cap": {"count": 2}, "prune": {"unseen_days": 14}})
    validated = config.validate_settings(settings)
    assert validated["company_cap"] == {"count": 2, "days": 30}
    assert validated["prune"] == {"enabled": True, "unseen_days": 14}
    with pytest.raises(ValueError):
        config.validate_settings(config.merge_settings(settings, {"prune": {"unseen_days": 1}}))
    assert all(titles for titles in config.ROLE_PRESETS.values())


@pytest.mark.parametrize("title, eligible", [
    ("Strategic Finance Associate", True),
    ("Research Associate, Economics", True),
    ("Talent Acquisition Associate - New Grad", False),     # shares only "associate"
    ("Technical Recruiter - New Grad", False),               # shares only "technical"
    ("Accountant (Contract)", False),
    ("Market Research Associate - Part Time", False),
])
def test_degree_fit_needs_a_real_title_match_and_full_time_work(title, eligible):
    from jobhub.scoring import score_job
    settings = config.merge_settings(config.DEFAULT_SETTINGS, {
        "target_titles": ["research associate", "strategic finance", "technical accounting", "accountant",
                          "new grad"], "min_salary": 90000, "preferred_locations": ["United States"]})
    _score, detail = score_job(Job(company="Northwind", title=title, source="greenhouse", ats="greenhouse",
                                   url="https://job-boards.greenhouse.io/northwind/jobs/1", location="Denver, CO"),
                               None, settings)
    assert detail["auto_apply_eligible"] is eligible, detail["review_reasons"]
