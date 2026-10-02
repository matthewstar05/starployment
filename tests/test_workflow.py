"""Isolated workflow regressions. These tests never contact employers or run a scheduler."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobhub import config, db, scheduler
from jobhub.models import ApplyResult, Job, Profile
from jobhub.server import app


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    db.close()
    for name, value in {
        "DATA": tmp_path, "DB_PATH": tmp_path / "test.db", "RESUMES": tmp_path / "resumes",
        "TAILORED": tmp_path / "tailored", "SCREENSHOTS": tmp_path / "screenshots",
    }.items():
        monkeypatch.setattr(config, name, value)
    config.RESUMES.mkdir()
    config.TAILORED.mkdir()
    config.SCREENSHOTS.mkdir()
    db.init()
    # Workflow behavior tests use fictional local postings. Current-public
    # metadata refresh has dedicated mocked coverage in test_posting_refresh.
    monkeypatch.setattr(scheduler, "enrich_job", lambda job: job)
    scheduler.STATE.stop = False
    scheduler.STATE.running_scan = False
    scheduler.STATE.running_apply = None
    scheduler.STATE.last_error = ""
    monkeypatch.setattr(app.state, "scheduler_enabled", False, raising=False)
    monkeypatch.setattr("jobhub.server.get_llm", lambda: type("NoLLM", (), {"describe": lambda self: "none"})())
    yield
    db.close()


def posting(url="https://jobs.lever.co/acme/123", **kw):
    return Job(source="lever", ats="lever", company="Acme", title="Software Engineer New Grad",
               url=url, location="United States", description="Python entry level", **kw)


def save_candidate():
    resume = config.RESUMES / "candidate.pdf"
    resume.write_bytes(b"%PDF-1.7\nfixture")
    db.save_profile(Profile(first_name="Ada", last_name="Lovelace", email="ada@example.com", resume_path=str(resume)))
    return str(resume)


def test_nested_settings_merge_and_legacy_answers_stay_unconfirmed():
    db.set_settings({"answers": {"work_authorized_us": "Yes"}})
    current = db.set_settings({"answers": {"salary_expectation": "125000"}})
    assert current["answers"]["work_authorized_us"] == "Yes"
    assert current["answers"]["confirmed_fields"] == []
    assert current["answers"]["requires_sponsorship"] == ""
    assert current["sources"]["lever"] is True
    with pytest.raises(ValueError):
        db.set_settings({"scan_interval_min": -2})
    with pytest.raises(ValueError):
        db.set_settings({"auto_apply": "false"})
    with pytest.raises(ValueError):
        db.set_settings({"companies": {"lever": ["../../secrets"]}})
    assert db.get_settings()["scan_interval_min"] == 60


def test_canonical_dedupe_keeps_old_id_and_applied_history():
    old = posting(url="https://jobs.lever.co/acme/123?utm_source=old", id="legacy-id")
    db.upsert_jobs([old])
    application = db.reserve_application(old.id)
    db.start_application(application, old.id)
    db.finish_application(application, "applied", [], [], [], submission_attempted=True)
    fresh = posting()
    assert db.upsert_jobs([fresh]) == 0
    assert fresh.id == "legacy-id"
    assert db.job_stats()["total"] == 1
    with pytest.raises(ValueError, match="submission record"):
        db.reserve_application(fresh.id)
    with pytest.raises(ValueError, match="cannot be reset"):
        db.set_job_status(fresh.id, "new")


def test_concurrent_reservations_only_create_one_attempt():
    job = posting()
    db.upsert_jobs([job])
    def reserve():
        try:
            return db.reserve_application(job.id)
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        reservations = list(pool.map(lambda _: reserve(), range(12)))
    assert len([r for r in reservations if r is not None]) == 1
    assert len(db.list_applications()) == 1
    assert db.get_job(job.id).status == "queued"


def test_submit_exception_is_durable_and_never_retried(monkeypatch):
    resume = save_candidate()
    job = posting()
    db.upsert_jobs([job])
    monkeypatch.setattr(scheduler, "resume_for_job", lambda *args: resume)
    calls = []
    def uncertain(*args, **kwargs):
        calls.append(True)
        db.mark_submission_attempted(job.id)
        raise RuntimeError("browser disconnected after submit")
    monkeypatch.setattr(scheduler, "apply_to_job", uncertain)
    result = scheduler.apply_one(job.id)
    assert result["status"] == "needs_human"
    assert result["submission_attempted"] is True
    assert db.get_job(job.id).status == "needs_human"
    assert db.get_application(result["application_id"])["submission_attempted"] == 1
    assert scheduler.apply_one(job.id)["status"] == "blocked"
    assert len(calls) == 1
    assert scheduler.STATE.running_apply is None


def test_unmarked_engine_success_is_never_recorded_as_applied(monkeypatch):
    resume = save_candidate()
    job = posting()
    db.upsert_jobs([job])
    monkeypatch.setattr(scheduler, "resume_for_job", lambda *args: resume)
    monkeypatch.setattr(
        scheduler, "apply_to_job",
        lambda *args, **kwargs: ApplyResult(status="applied", message="unverified engine result"),
    )

    result = scheduler.apply_one(job.id)

    assert result["status"] == "needs_human"
    assert result["submission_attempted"] is False
    assert db.get_job(job.id).status == "needs_human"
    record = db.get_application(result["application_id"])
    assert record["status"] == "needs_human"
    assert record["submission_attempted"] == 0


def test_database_rejects_applied_status_without_submit_marker():
    job = posting()
    db.upsert_jobs([job])
    app_id = db.reserve_application(job.id)
    db.start_application(app_id, job.id)

    with pytest.raises(ValueError, match="durable submission attempt"):
        db.finish_application(app_id, "applied", [], [], [])

    assert db.get_job(job.id).status == "applying"


def test_dry_run_leaves_job_available_and_stores_resume(monkeypatch):
    resume = save_candidate()
    job = posting()
    db.upsert_jobs([job])
    monkeypatch.setattr(scheduler, "resume_for_job", lambda *args: resume)
    monkeypatch.setattr(scheduler, "apply_to_job", lambda *args, **kwargs: ApplyResult(status="dry_run", log=["Filled fixture"]))
    result = scheduler.apply_one(job.id, dry_run=True)
    assert result["status"] == "dry_run"
    assert db.get_job(job.id).status == "new"
    snapshot = db.get_application(result["application_id"])["resume_path"]
    assert snapshot != resume
    assert Path(snapshot).read_bytes() == Path(resume).read_bytes()


def test_restart_recovers_dead_worker_without_losing_submit_boundary():
    job = posting()
    db.upsert_jobs([job])
    application = db.reserve_application(job.id)
    db.start_application(application, job.id)
    db.mark_submission_attempted(job.id)
    with db.conn() as c:
        c.execute("UPDATE applications SET worker_pid=2147483647 WHERE id=?", (application,))
    db.recover_interrupted()
    assert db.get_job(job.id).status == "needs_human"
    assert db.get_application(application)["status"] == "needs_human"
    with pytest.raises(ValueError, match="submission record"):
        db.reserve_application(job.id)


def test_source_errors_visible_and_scan_unlocks(monkeypatch):
    def broken(settings):
        raise RuntimeError("rate limited")
    monkeypatch.setattr(scheduler, "SOURCES", {"lever": broken})
    result = scheduler.scan_once()
    assert result["status"] == "error"
    assert "rate limited" in scheduler.STATE.last_error
    assert db.list_runs()[0]["status"] == "error"
    assert scheduler.STATE.running_scan is False
    assert scheduler.scan_once()["status"] == "error"


def test_partial_source_warnings_preserve_jobs(monkeypatch):
    class SourceJobs(list):
        warnings = ["One company board timed out"]
    monkeypatch.setattr(scheduler, "SOURCES", {"lever": lambda settings: SourceJobs([posting()])})
    result = scheduler.scan_once()
    assert result["status"] == "partial"
    assert result["new"] == 1
    assert "timed out" in scheduler.STATE.last_error


def test_api_blocks_cross_origin_and_file_escape():
    sibling = config.DATA.parent / (config.DATA.name + "-private.pdf")
    sibling.write_bytes(b"%PDF-1.7")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.put("/api/settings", json={"auto_apply": True}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.get("/api/profile", headers={"Host": "evil.example"}).status_code == 403
        assert client.get("/api/resume/file", params={"path": str(sibling)}).status_code == 404
        assert client.get("/api/resume/file", params={"path": str(config.DB_PATH)}).status_code == 404
        assert client.put("/api/settings", json={"max_apply_per_run": 0}).status_code == 422
        assert client.put("/api/profile", json={"resume_path": "/etc/passwd"}).status_code == 422
        assert client.post("/api/profile/resume", files={"file": ("resume.pdf", b"not a pdf", "application/pdf")}).status_code == 422
        assert client.get("/api/jobs?limit=-1").status_code == 422
        assert client.get("/api/settings").status_code == 200


def test_api_queue_returns_attempt_and_rejects_duplicate(monkeypatch):
    resume = save_candidate()
    job = posting()
    db.upsert_jobs([job])
    monkeypatch.setattr(scheduler, "resume_for_job", lambda *args: resume)
    monkeypatch.setattr(scheduler, "apply_to_job", lambda *args, **kwargs: ApplyResult(status="applied", submission_attempted=True))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        first = client.post(f"/api/jobs/{job.id}/apply")
        assert first.status_code == 200
        assert first.json()["application_id"] > 0
        assert client.post(f"/api/jobs/{job.id}/apply").status_code == 409
    assert len(db.list_applications()) == 1


def test_preflight_validation_failure_can_be_retried(monkeypatch):
    resume = save_candidate()
    job = posting()
    db.upsert_jobs([job])
    monkeypatch.setattr(scheduler, "resume_for_job", lambda *args: resume)
    results = iter([
        ApplyResult(status="needs_human", message="Missing required answer", unanswered=["Citizenship"]),
        ApplyResult(status="dry_run", message="Required answer supplied"),
    ])
    monkeypatch.setattr(scheduler, "apply_to_job", lambda *args, **kwargs: next(results))
    assert scheduler.apply_one(job.id)["status"] == "needs_human"
    assert scheduler.apply_one(job.id, dry_run=True)["status"] == "dry_run"
    assert len(db.list_applications()) == 2


def test_browser_reserved_across_different_jobs():
    first, second = posting(), posting(url="https://jobs.lever.co/acme/456")
    db.upsert_jobs([first, second])
    db.reserve_application(first.id)
    with pytest.raises(ValueError, match="already queued"):
        db.reserve_application(second.id)
    assert db.get_job(second.id).status == "new"


def test_legacy_duplicate_vacancies_are_hidden_but_history_preserved():
    job = posting()
    db.upsert_jobs([job])
    with db.conn() as c:
        row = dict(c.execute("SELECT * FROM jobs WHERE id=?", (job.id,)).fetchone())
        row.update(id="old-applied-id", url=job.url + "/apply", status="applied")
        columns = ",".join(row)
        placeholders = ",".join("?" for _ in row)
        c.execute(f"INSERT INTO jobs({columns}) VALUES ({placeholders})", list(row.values()))
    assert db.job_stats() == {"applied": 1, "total": 1}
    assert len(db.list_jobs(status="all")) == 1
    assert db.list_jobs(status="new") == []
    assert db.get_job(job.id) is not None
    assert db.get_job("old-applied-id") is not None
    with pytest.raises(ValueError, match="submission record"):
        db.reserve_application(job.id)


def test_auto_apply_respects_eligibility_and_current_toggle(monkeypatch):
    blocked = posting(url="https://jobs.lever.co/acme/blocked", score=100, score_json={"auto_apply_eligible": False})
    eligible = posting(url="https://jobs.lever.co/acme/eligible", score=90, score_json={"auto_apply_eligible": True})
    db.upsert_jobs([blocked, eligible])
    db.set_settings({"auto_apply": True, "setup_complete": True})
    monkeypatch.setattr(scheduler, "rescore", lambda: None)
    seen = []
    def apply(job_id, **kwargs):
        seen.append(job_id)
        return {"status": "dry_run"}
    monkeypatch.setattr(scheduler, "apply_one", apply)
    assert len(scheduler.auto_apply_batch()) == 1
    assert seen == [eligible.id]
    db.set_settings({"auto_apply": False})
    assert scheduler.auto_apply_batch() == []


def test_auto_apply_waits_for_candidate_setup(monkeypatch):
    db.upsert_jobs([posting(score=90, score_json={"auto_apply_eligible": True})])
    db.set_settings({"auto_apply": True, "setup_complete": False})
    monkeypatch.setattr(scheduler, "rescore", lambda: None)
    assert scheduler.auto_apply_batch() == []
    assert db.list_applications() == []


def test_auto_apply_preflight_skips_missing_facts_and_uses_next_slot(monkeypatch):
    from jobhub.readiness import Readiness
    blocked = Job(source="greenhouse", ats="greenhouse", company="Acme", title="Software Engineer New Grad",
                  url="https://job-boards.greenhouse.io/acme/jobs/101", description="Python entry level",
                  location="United States", score=100, score_json={"auto_apply_eligible": True})
    ready = Job(source="greenhouse", ats="greenhouse", company="Acme", title="Software Engineer New Grad",
                url="https://job-boards.greenhouse.io/acme/jobs/102", description="Python entry level",
                location="United States", score=90, score_json={"auto_apply_eligible": True})
    db.upsert_jobs([blocked, ready])
    db.set_settings({"auto_apply": True, "setup_complete": True, "max_apply_per_run": 1})
    monkeypatch.setattr(scheduler, "rescore", lambda: None)
    checked = []
    def preflight(job, profile, settings):
        checked.append(job.id)
        return Readiness("missing_facts", ("Work authorization",)) if job.id == blocked.id else Readiness("ready")
    monkeypatch.setattr(scheduler, "preflight_job", preflight)
    submitted = []
    monkeypatch.setattr(scheduler, "apply_one", lambda job_id, **kwargs: submitted.append(job_id) or {"status": "dry_run"})
    assert scheduler.auto_apply_batch() == [{"status": "dry_run"}]
    assert checked == [blocked.id, ready.id]
    assert submitted == [ready.id]
    assert db.list_applications() == []


def test_auto_apply_rotates_bounded_preflight_window_across_scans(monkeypatch):
    from jobhub.readiness import Readiness

    jobs = [
        Job(source="lever", ats="lever", company=f"Company {index}", title="Software Engineer",
            url=f"https://jobs.lever.co/company-{index}/{index}", location="United States",
            score=100 - index, score_json={"auto_apply_eligible": True})
        for index in range(25)
    ]
    db.upsert_jobs(jobs)
    db.set_settings({"auto_apply": True, "setup_complete": True, "max_apply_per_run": 1})
    monkeypatch.setattr(scheduler, "rescore", lambda: None)
    checked = []

    def preflight(job, profile, settings):
        checked.append(job.id)
        return Readiness("ready") if job.id == jobs[24].id else Readiness("missing_facts", ("Work authorization",))

    monkeypatch.setattr(scheduler, "preflight_job", preflight)
    submitted = []
    monkeypatch.setattr(scheduler, "apply_one", lambda job_id, **kwargs:
                        submitted.append(job_id) or {"status": "dry_run"})

    first_run = db.create_run()
    assert scheduler.auto_apply_batch() == []
    db.finish_run(first_run, 0, 0, 0, "ok", [])
    assert checked == [job.id for job in jobs[:24]]

    second_run = db.create_run()
    assert scheduler.auto_apply_batch() == [{"status": "dry_run"}]
    db.finish_run(second_run, 0, 0, 0, "ok", [])
    assert jobs[24].id in checked[24:]
    assert submitted == [jobs[24].id]
    assert db.list_applications() == []


def test_auto_apply_batch_tries_distinct_employers_before_repeating_one(monkeypatch):
    jobs = [
        Job(source="lever", ats="lever", company=company, title="Software Engineer",
            url=f"https://jobs.lever.co/{company.lower()}/{index}", location="United States",
            score=score, score_json={"auto_apply_eligible": True})
        for index, (company, score) in enumerate((
            ("Example", 100), ("EXAMPLE", 99), ("Another", 98), ("Third", 97)
        ))
    ]
    db.upsert_jobs(jobs)
    db.set_settings({"auto_apply": True, "setup_complete": True, "max_apply_per_run": 3})
    monkeypatch.setattr(scheduler, "rescore", lambda: None)
    attempted = []
    monkeypatch.setattr(scheduler, "apply_one", lambda job_id, **kwargs:
                        attempted.append(job_id) or {"status": "needs_human"})

    assert len(scheduler.auto_apply_batch()) == 3
    assert attempted == [jobs[0].id, jobs[2].id, jobs[3].id]


def test_scan_records_source_notices_without_partial_failure(monkeypatch):
    from jobhub.sources.base import SourceJobs
    monkeypatch.setattr(scheduler, "SOURCES", {
        "greenhouse": lambda _settings: SourceJobs(notices=["1 bundled board in retry window"]),
    })
    monkeypatch.setattr(scheduler, "rescore", lambda: None)
    summary = scheduler.scan_once()
    assert summary["status"] == "ok"
    assert "greenhouse: INFO 1 bundled board in retry window" in summary["log"]


def test_scheduler_runs_one_cycle_and_stops_cleanly(monkeypatch):
    import asyncio
    from datetime import datetime
    calls = []
    clock = [1000.0]
    scheduled = []
    monkeypatch.setattr(scheduler.time, "time", lambda: clock[0])
    async def fake_sleep(seconds):
        calls.append(("sleep", seconds))
        if len([call for call in calls if call[0] == "sleep"]) == 2:
            scheduled.append(scheduler.STATE.next_scan_at)
            scheduler.STATE.stop = True
    def scan_cycle():
        calls.append(("scan", None))
        clock[0] += 30
        return {"status": "ok"}
    def apply_cycle():
        calls.append(("auto", None))
        clock[0] += 60
        return []
    monkeypatch.setattr(scheduler.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(scheduler, "scan_once", scan_cycle)
    monkeypatch.setattr(scheduler, "auto_apply_batch", apply_cycle)
    asyncio.run(scheduler.loop())
    assert ("scan", None) in calls and ("auto", None) in calls
    assert datetime.fromisoformat(scheduled[0]).timestamp() == 4600
    assert scheduler.STATE.next_scan_at is None


def test_custom_answer_map_can_be_cleared():
    db.set_settings({"answers": {"field_answers": {"Citizenship": "Example answer"}}})
    result = db.set_settings({"answers": {"field_answers": {}}})
    assert result["answers"]["field_answers"] == {}


def test_import_immediately_ranks_job_and_updates_fresh_description(monkeypatch):
    from jobhub.sources import import_job as importer
    initial = posting().model_copy(update={'description': 'Old long description: Python Python Python', 'remote': True})
    db.upsert_jobs([initial])
    updated = posting().model_copy(update={'description': 'SQL role', 'location': 'New York', 'remote': False})
    monkeypatch.setattr(importer, 'fetch_job', lambda url: updated)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response = client.post('/api/jobs/import', json={'url': initial.url})
        assert response.status_code == 200, response.text
        result = response.json()
        assert not result['created']
        assert result['job']['id'] == initial.id
        assert result['job']['description'] == 'SQL role'
        assert result['job']['score'] > 0
        assert result['job']['remote'] is False
