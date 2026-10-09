"""Posting dates, priority companies and stars, marking as sent, hiding, and board discovery.

Every store is temporary and every HTTP response is a local fake; no employer is contacted.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

from jobhub import config, db, prepare, scheduler
from jobhub.models import Job, Profile, board_of, posted_epoch
from jobhub.priority import held_from_automation, held_from_bulk_send, is_priority, is_starred, normalize_company
from jobhub.sources import base, greenhouse


def ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def posting(slug: str, number: int, *, company: str = "Example", hours: float | None = 2, score: float = 80,
            title: str = "Software Engineer", **extra) -> Job:
    return Job(source="greenhouse", ats="greenhouse", company=company, title=title,
               url=f"https://job-boards.greenhouse.io/{slug}/jobs/{number}", description="Python services",
               location="United States", posted_at=ago(hours) if hours is not None else None, score=score,
               score_json={"auto_apply_eligible": True}, **extra)


def save_jobs(*jobs: Job) -> None:
    db.upsert_jobs(list(jobs))
    db.update_scores({job.id: (job.score, job.score_json) for job in jobs})


# ------------------------------------------------------------------ posting dates
def test_posted_epoch_reads_every_source_format():
    assert posted_epoch("2026-10-03T09:20:21-04:00") == datetime(2026, 10, 3, 13, 20, 21, tzinfo=timezone.utc).timestamp()
    assert posted_epoch("2026-10-03T13:20:21Z") == posted_epoch("2026-10-03T09:20:21-04:00")
    assert posted_epoch("2026-10-03") == datetime(2026, 10, 3, 12, tzinfo=timezone.utc).timestamp()
    assert posted_epoch(1759497621000) == 1759497621.0
    assert posted_epoch("not a date") is None and posted_epoch(None) is None and posted_epoch("") is None


def test_posting_window_filters_and_sorts_across_the_whole_catalog(store):
    fresh = posting("alpha", 1, hours=3, score=60)
    newest = posting("alpha", 2, hours=1, score=50)
    old = posting("alpha", 3, hours=80, score=95)
    unknown = posting("alpha", 4, hours=None, score=99)
    save_jobs(fresh, newest, old, unknown)

    within = db.list_jobs(status="new", posted_within_hours=24)
    assert {job.id for job in within} == {fresh.id, newest.id}
    assert db.count_jobs(status="new", posted_within_hours=24) == 2
    assert db.count_jobs(status="new") == 4
    by_posted = [job.id for job in db.list_jobs(order="posted")]
    assert by_posted == [newest.id, fresh.id, old.id, unknown.id]   # unknown dates last
    assert [job.id for job in db.list_jobs(order="posted", descending=False)][:3] == [old.id, fresh.id, newest.id]
    assert db.list_jobs()[0].id == unknown.id                        # best match stays the default
    with pytest.raises(ValueError):
        db.list_jobs(order="score; DROP TABLE jobs")
    counts = db.fresh_counts()
    assert counts["fresh_24"] == 2 and counts["fresh_168"] == 3
    assert counts["preparable_0"] == 4 and counts["preparable_24"] == 2


def test_existing_rows_get_posted_time_on_upgrade(store):
    job = posting("alpha", 1, hours=5)
    save_jobs(job)
    with db.conn() as c:
        c.execute("UPDATE jobs SET posted_ts=NULL")
    db.close()
    db.init()
    assert db.list_jobs(posted_within_hours=24)[0].id == job.id


def test_plan_prepares_the_newest_postings_inside_the_window(store):
    jobs = [posting(f"c{i}", i, company=f"Company {i}", hours=hours, score=score)
            for i, (hours, score) in enumerate(((2, 70), (10, 90), (30, 99), (1, 61)))]
    save_jobs(*jobs)
    best = prepare.plan_candidates(5, 60, posted_within_hours=24)
    assert [job.id for job in best] == [jobs[1].id, jobs[0].id, jobs[3].id]
    newest = prepare.plan_candidates(2, 60, posted_within_hours=24, order="newest")
    assert [job.id for job in newest] == [jobs[3].id, jobs[0].id]
    assert len(prepare.plan_candidates(10, 60)) == 4


# ------------------------------------------------------------------ priority companies and stars
def test_priority_names_match_whole_words_only():
    priority = config.PRIORITY_COMPANIES
    assert normalize_company("D. E. Shaw & Co.") == normalize_company("DE Shaw") == "de shaw"
    for name in ("Meta Platforms, Inc.", "Google LLC", "Jumptrading", "Citadel Securities", "Amazon Web Services"):
        assert is_priority(name, priority), name
    for name in ("Metabase", "Applied Intuition", "Stripes Co", "Example", "Acme", "IMCD"):
        assert not is_priority(name, priority), name


def test_star_is_the_candidates_choice_or_else_the_priority_list(store):
    job = posting("google", 1, company="Google")
    save_jobs(job)
    priority = ["Google"]
    assert is_starred(db.get_job(job.id), priority)
    db.set_job_star(job.id, False)
    assert not is_starred(db.get_job(job.id), priority)
    assert held_from_automation(db.get_job(job.id), priority)    # unattended runs still never touch it
    plain = posting("example", 2)
    save_jobs(plain)
    db.set_job_star(plain.id, True)
    assert is_starred(db.get_job(plain.id), priority) and held_from_automation(db.get_job(plain.id), priority)


def test_automatic_runs_skip_priority_companies_and_starred_roles(store, monkeypatch):
    priority_job = posting("google", 1, company="Google", score=99)
    starred_job = posting("starred", 2, company="Starred Co", score=98)
    normal = posting("normal", 3, company="Normal Co", score=97)
    save_jobs(priority_job, starred_job, normal)
    db.set_job_star(starred_job.id, True)
    db.set_settings({"auto_apply": True, "setup_complete": True, "max_apply_per_run": 5,
                     "priority_companies": ["Google"]})
    monkeypatch.setattr(scheduler, "rescore", lambda: None)
    monkeypatch.setattr(scheduler.STATE, "stop", False)
    monkeypatch.setattr(scheduler, "preflight_job", lambda *a: type("R", (), {"status": "ready"})())
    attempted = []
    monkeypatch.setattr(scheduler, "apply_one", lambda job_id, **kw: attempted.append(job_id) or {"status": "dry_run"})
    scheduler.auto_apply_batch()
    assert attempted == [normal.id]


def test_unattended_attempt_on_a_priority_role_stops_before_the_browser(store, make_pdf, monkeypatch):
    resume = make_pdf(store / "resumes" / "main.pdf", ["Ada Example"])
    db.save_profile(Profile(first_name="Ada", last_name="Example", email="ada@example.test", resume_path=str(resume)))
    job = posting("google", 1, company="Google", score=99)
    save_jobs(job)
    db.set_settings({"auto_apply": True, "setup_complete": True, "priority_companies": ["Google"]})
    monkeypatch.setattr(scheduler, "enrich_job", lambda job: job)
    monkeypatch.setattr(scheduler, "apply_to_job", lambda *a, **k: pytest.fail("browser must not run"))
    result = scheduler.apply_one(job.id, require_eligible=True)
    assert result["status"] == "needs_human"
    assert "waits for your own approval" in result["message"]
    assert not db.list_applications()[0]["submission_attempted"]


def test_send_ready_leaves_starred_applications_for_one_by_one_review():
    drafts = [{"id": 1, "status": "ready", "company": "Google", "starred": None},
              {"id": 2, "status": "ready", "company": "Example", "starred": None},
              {"id": 3, "status": "approved", "company": "Google", "starred": None},
              {"id": 4, "status": "ready", "company": "Example", "starred": 1},
              {"id": 5, "status": "ready", "company": "Google", "starred": 0}]
    send, held = prepare.bulk_send_candidates(drafts, ["Google"])
    assert [d["id"] for d in send] == [2, 3, 5]
    assert [d["id"] for d in held] == [1, 4]


def test_fortune_stars_and_hold_policies():
    # Fortune 500 companies (Apple, Walmart): red star, held from bulk send and automation
    for company in ("Apple", "Walmart"):
        record = {"company": company, "starred": None}
        assert is_starred(record, []) == "red"
        assert held_from_bulk_send(record, []) is True
        assert held_from_automation(record, []) is True

    # Fortune 501-1000 companies (Snowflake, Synopsys): amber star (star for a label), NOT held from bulk send or automation
    for company in ("Snowflake", "Synopsys"):
        record = {"company": company, "starred": None}
        assert is_starred(record, []) == "amber"
        assert held_from_bulk_send(record, []) is False
        assert held_from_automation(record, []) is False

    # Manual star on 501-1000 company (starred=1): amber star, held from bulk send and automation
    manual_starred = {"company": "Snowflake", "starred": 1}
    assert is_starred(manual_starred, []) == "amber"
    assert held_from_bulk_send(manual_starred, []) is True
    assert held_from_automation(manual_starred, []) is True

    # Explicit unstar on Fortune 500 company (starred=0): is_starred is False, held_from_bulk_send is False
    unstarred_f500 = {"company": "Apple", "starred": 0}
    assert is_starred(unstarred_f500, []) is False
    assert held_from_bulk_send(unstarred_f500, []) is False
    # held_from_automation remains True if company is priority, but if not in priority, it's False when starred=0
    assert held_from_automation(unstarred_f500, []) is False

    # bulk_send_candidates sends ready drafts for Fortune 501-1000, holds for Fortune 500
    drafts = [
        {"id": 10, "status": "ready", "company": "Apple", "starred": None},     # F500 -> held
        {"id": 11, "status": "ready", "company": "Snowflake", "starred": None}, # F1000 -> sent
        {"id": 12, "status": "ready", "company": "Synopsys", "starred": None},  # F1000 -> sent
        {"id": 13, "status": "ready", "company": "Walmart", "starred": None},   # F500 -> held
    ]
    send, held = prepare.bulk_send_candidates(drafts, [])
    assert [d["id"] for d in send] == [11, 12]
    assert [d["id"] for d in held] == [10, 13]


def test_settings_validate_priority_companies_and_plan_options(store):
    saved = db.set_settings({"priority_companies": ["  Jane   Street ", "jane street", "OpenAI"],
                             "apply_plan": {"posted_within_hours": 24, "order": "newest"}})
    assert saved["priority_companies"] == ["Jane Street", "OpenAI"]
    assert saved["apply_plan"]["posted_within_hours"] == 24 and saved["apply_plan"]["order"] == "newest"
    with pytest.raises(ValidationError):
        db.set_settings({"apply_plan": {"order": "random"}})


# ------------------------------------------------------------------ mark as sent and remove from the list
def test_marking_a_prepared_application_as_sent_moves_it_to_the_tracker(store):
    job = posting("example", 1)
    save_jobs(job)
    draft_id = db.upsert_draft(job.id, status="ready", resume_path="/tmp/resume.pdf")
    app_id = db.mark_application_sent(job.id)

    record = db.get_application(app_id)
    assert record["status"] == "applied" and record["method"] == "manual" and record["submission_attempted"]
    assert db.get_draft(draft_id)["status"] == "sent"
    assert db.get_job(job.id).status == "applied"
    assert [row["id"] for row in db.list_tracker()] == [app_id]
    with pytest.raises(ValueError, match="already recorded"):
        db.mark_application_sent(job.id)
    with pytest.raises(ValueError, match="submission record"):
        db.reserve_application(job.id)                     # never sent again


def test_marking_sent_supersedes_earlier_unfinished_attempts(store):
    job = posting("example", 1)
    save_jobs(job)
    first = db.create_application(job.id, "engine")
    db.finish_application(first, "needs_human", [], [], ["Visa status"], "Required fields")
    db.mark_application_sent(job.id)
    rows = {row["id"]: row for row in db.list_applications()}
    assert rows[first]["hidden"] == 1


def test_hiding_keeps_history_and_the_tracker(store):
    job = posting("example", 1)
    save_jobs(job)
    app_id = db.mark_application_sent(job.id)
    db.hide_application(app_id)
    assert db.get_application(app_id)["hidden"] == 1
    assert [row["id"] for row in db.list_tracker()] == [app_id]
    with pytest.raises(ValueError, match="submission record"):
        db.reserve_application(job.id)
    busy = posting("example", 2)
    save_jobs(busy)
    queued = db.reserve_application(busy.id)
    with pytest.raises(ValueError, match="being sent"):
        db.hide_application(queued)
    with pytest.raises(ValueError, match="being sent"):
        db.mark_application_sent(busy.id)


# ------------------------------------------------------------------ board discovery
def test_boards_are_read_from_hosted_posting_urls_only():
    assert board_of("https://job-boards.greenhouse.io/Perpay/jobs/4034578007") == ("greenhouse", "perpay")
    assert board_of("https://boards.greenhouse.io/embed/job_app?for=acme&token=1") == ("greenhouse", "acme")
    assert board_of("https://jobs.lever.co/wyetechllc/e0670581") == ("lever", "wyetechllc")
    assert board_of("https://jobs.ashbyhq.com/arch.co/056e41f8") == ("ashby", "arch.co")
    for url in ("https://evilgreenhouse.io/acme/jobs/1", "https://jobs.lever.co.evil.test/acme/1",
                "https://jobs.ashbyhq.com/acme", "https://example.test/careers/1", "not a url"):
        assert board_of(url) is None, url


def test_discovered_boards_rotate_rest_after_failures_and_skip_configured(store):
    jobs = [Job(source="simplify:newgrad", ats="greenhouse", company=name, title="Software Engineer",
                url=f"https://job-boards.greenhouse.io/{name}/jobs/{i}") for i, name in enumerate(("a1", "b2", "c3"))]
    assert db.discover_boards(jobs) == 3
    assert db.discover_boards(jobs) == 0
    assert db.boards_for_scan("greenhouse", {"B2"}, 5) == ["a1", "c3"]
    db.record_board_scans("greenhouse", {"a1": 4})
    assert db.boards_for_scan("greenhouse", set(), 2) == ["b2", "c3"]   # least recently read first
    for _ in range(3):
        db.record_board_scans("greenhouse", {"c3": None})
    assert "c3" not in db.boards_for_scan("greenhouse", set(), 5)        # rests for a week
    assert db.board_counts() == {"greenhouse": 2}


def test_discovered_board_failures_are_quiet_and_reported_per_board():
    def fetch(slug, settings):
        if slug in ("broken", "configured-bad"):
            raise RuntimeError("HTTP 500")
        return [posting(slug, 1)]

    result = base.fetch_boards(["configured", "configured-bad"], fetch, {}, source="greenhouse",
                               discovered=["found", "broken", "CONFIGURED"])
    assert len(result) == 2
    assert len(result.warnings) == 1 and "configured-bad" in result.warnings[0]
    assert result.board_results == {"found": 1, "broken": None}
    assert "Read 1 of 2 discovered board(s)" in result.notices


def test_scan_reads_discovered_boards_and_learns_new_ones(store, monkeypatch):
    db.discover_boards([posting("seen", 1)])
    db.set_settings({"companies": {"greenhouse": ["configured"], "lever": [], "ashby": []},
                     "sources": {"greenhouse": True, "lever": False, "ashby": False, "simplify": False,
                                 "remoteok": False, "jobspy": False}})
    calls = []

    def fake_greenhouse(settings):
        calls.append(settings["_discovered_boards"])
        result = base.SourceJobs([posting("configured", 7), Job(
            source="simplify:newgrad", ats="lever", company="Elsewhere", title="Software Engineer",
            url="https://jobs.lever.co/elsewhere/abc")])
        result.board_results = {"seen": 1}
        return result

    monkeypatch.setattr(scheduler, "SOURCES", {"greenhouse": fake_greenhouse})
    report = scheduler.scan_once()
    assert report["status"] == "ok"
    assert calls == [{"greenhouse": ["seen"]}]
    assert db.boards_for_scan("lever", set(), 5) == ["elsewhere"]
    assert db.boards_for_scan("greenhouse", set(), 5) == ["configured", "seen"]  # read boards rotate to the back
    assert any("discovered 2 more company board" in line for line in report["log"])


class FakeGreenhouse:
    """A local Greenhouse API: a light listing, per-posting details, and a full listing."""

    def __init__(self, count: int):
        self.requests: list[str] = []
        self.jobs = [{"id": 100 + i, "title": "Software Engineer" if i % 2 == 0 else "Account Executive",
                      "absolute_url": f"https://example.test/careers/{100 + i}", "company_name": "Example",
                      "location": {"name": "Remote"}, "first_published": ago(5)} for i in range(count)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        path = request.url.path
        if path.endswith("/jobs"):
            content = request.url.params.get("content") == "true"
            return httpx.Response(200, json={"jobs": [dict(j, content="Python &lt;b&gt;services&lt;/b&gt;" if content else None)
                                                       for j in self.jobs]})
        job_id = int(path.rsplit("/", 1)[1])
        job = next(j for j in self.jobs if j["id"] == job_id)
        return httpx.Response(200, json=dict(job, content="Python services $150,000 - $180,000",
                                             departments=[{"name": "Engineering"}]))


def test_greenhouse_reads_the_light_listing_then_describes_only_new_matches(monkeypatch):
    api = FakeGreenhouse(6)
    monkeypatch.setattr(greenhouse, "client", lambda **_: httpx.Client(transport=httpx.MockTransport(api.handler)))
    settings = {"target_titles": ["software engineer"], "_known_postings": {"greenhouse:acme:100"}}
    jobs = greenhouse._fetch_company("acme", settings)
    assert [job.title for job in jobs] == ["Software Engineer"] * 3
    detail_requests = [url for url in api.requests if not url.split("?")[0].endswith("/jobs")]
    assert len(detail_requests) == 2                         # posting 100 is already described
    described = {job.raw["gh_id"]: job for job in jobs}
    assert described[102].description == "Python services $150,000 - $180,000"
    assert described[102].salary_min == 150000 and described[102].posted_at
    assert described[100].description == ""                 # keeps the stored description on upsert
    assert described[102].apply_url == "https://job-boards.greenhouse.io/acme/jobs/102"


def test_greenhouse_reads_one_full_listing_when_many_matches_are_new(monkeypatch):
    api = FakeGreenhouse(2 * (greenhouse.DETAIL_REQUEST_LIMIT + 2))
    monkeypatch.setattr(greenhouse, "client", lambda **_: httpx.Client(transport=httpx.MockTransport(api.handler)))
    jobs = greenhouse._fetch_company("acme", {"target_titles": ["software engineer"]})
    assert len(jobs) == greenhouse.DETAIL_REQUEST_LIMIT + 2
    assert len(api.requests) == 2 and "content=true" in api.requests[1]
    assert all(job.description == "Python services" for job in jobs)


# ------------------------------------------------------------------ upgrading an existing install
def test_existing_install_receives_new_catalog_boards_once(store):
    removed = config.CATALOG_ADDITIONS["greenhouse"][0]
    with db.conn() as c:
        c.execute("DELETE FROM settings WHERE key='catalog_version'")
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('companies', ?)",
                  (json.dumps({"greenhouse": ["stripe", "my-company"], "lever": ["palantir"], "ashby": []}),))
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('jobspy', ?)", (json.dumps({
            "sites": ["indeed", "linkedin", "zip_recruiter", "google"], "search_terms": ["quant developer"],
            "location": "", "country_indeed": "usa", "results_wanted": 20, "hours_old": 72}),))
    db.close()
    db.init()
    settings = db.get_settings()
    greenhouse_boards = settings["companies"]["greenhouse"]
    assert greenhouse_boards[:2] == ["stripe", "my-company"]
    assert set(config.CATALOG_ADDITIONS["greenhouse"]) <= set(greenhouse_boards)
    assert "spotify" in settings["companies"]["lever"]
    assert settings["jobspy"]["sites"] == ["indeed", "linkedin", "google"]
    assert settings["jobspy"]["results_wanted"] == 50 and settings["jobspy"]["search_terms"] == ["quant developer"]
    assert settings["catalog_version"] == config.CATALOG_VERSION

    # A board the candidate removes afterwards stays removed.
    db.set_settings({"companies": {**settings["companies"],
                                   "greenhouse": [s for s in greenhouse_boards if s != removed]}})
    db.close()
    db.init()
    assert removed not in db.get_settings()["companies"]["greenhouse"]


def test_fresh_install_uses_the_full_catalog(store):
    settings = db.get_settings()
    assert set(config.CATALOG_ADDITIONS["ashby"]) <= set(settings["companies"]["ashby"])
    assert settings["discover_boards"] is True
    assert "zip_recruiter" not in settings["jobspy"]["sites"]
    with sqlite3.connect(config.DB_PATH) as raw:
        assert json.loads(raw.execute("SELECT value FROM settings WHERE key='catalog_version'").fetchone()[0]) \
            == config.CATALOG_VERSION

