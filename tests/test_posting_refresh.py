"""Current public ATS metadata refresh, with local fixtures only."""
import pytest

from jobhub import config, db, scheduler
from jobhub.models import Job, Profile
from jobhub.server import app
from fastapi.testclient import TestClient


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
    scheduler.STATE.stop = False
    scheduler.STATE.running_apply = None
    monkeypatch.setattr(app.state, "scheduler_enabled", False, raising=False)
    yield
    db.close()


def singlestore_posting(**updates):
    values = {
        "source": "greenhouse", "ats": "greenhouse", "company": "SingleStore",
        "title": "C++ Database Engineer", "location": "United States", "remote": False,
        "url": "https://job-boards.greenhouse.io/singlestore/jobs/8205389",
        "apply_url": "https://job-boards.greenhouse.io/singlestore/jobs/8205389",
        "description": "Maintain a C++ database engine for experienced engineers.",
        "raw": {"gh_id": 8205389, "slug": "singlestore"},
    }
    values.update(updates)
    return Job(**values)


def test_refresh_replaces_stale_metadata_and_preserves_vacancy_history(monkeypatch):
    old = singlestore_posting(id="legacy-vacancy-id")
    db.upsert_jobs([old])
    db.set_job_status(old.id, "skipped")
    app_id = db.reserve_application(old.id)
    db.start_application(app_id, old.id)
    db.finish_application(app_id, "needs_human", [], [], ["fixture question"])
    updated = singlestore_posting(
        title="Helios New Grad 2027", location="Remote, United States", remote=True,
        description="Build Helios services with JavaScript, TypeScript, and Go.",
        raw={"gh_id": 8205389, "slug": "singlestore", "departments": ["Helios"]},
    )
    monkeypatch.setattr("jobhub.sources.import_job.fetch_job", lambda url: updated)

    current = scheduler.enrich_job(db.get_job(old.id))

    assert current.id == old.id == "legacy-vacancy-id"
    assert current.title == "Helios New Grad 2027"
    assert "TypeScript" in current.description
    assert current.location == "Remote, United States" and current.remote is True
    assert current.status == "needs_human"
    assert db.get_application(app_id)["job_id"] == old.id
    assert db.get_application(app_id)["unanswered"] == ["fixture question"]
    assert db.list_jobs(status="all")[0].title == current.title


def test_refresh_failure_stops_apply_before_resume_or_browser(monkeypatch):
    resume = config.RESUMES / "candidate.pdf"
    resume.write_bytes(b"%PDF-1.7\nfictional fixture")
    db.save_profile(Profile(first_name="Ada", last_name="Lovelace", email="ada@example.test",
                            resume_path=str(resume)))
    old = singlestore_posting()
    db.upsert_jobs([old])
    monkeypatch.setattr("jobhub.sources.import_job.fetch_job", lambda url: (_ for _ in ()).throw(
        RuntimeError("fixture endpoint unavailable")))
    monkeypatch.setattr(scheduler, "resume_for_job", lambda *args: pytest.fail("must not draft from stale metadata"))
    monkeypatch.setattr(scheduler, "apply_to_job", lambda *args, **kwargs:
                        pytest.fail("must not open the employer form"))

    result = scheduler.apply_one(old.id)

    assert result["status"] == "needs_human"
    assert result["submission_attempted"] is False
    assert db.get_job(old.id).status == "needs_human"
    assert db.get_application(result["application_id"])["submission_attempted"] == 0
    assert db.get_job(old.id).title == old.title


def test_unattended_apply_rechecks_eligibility_after_stale_new_grad_becomes_senior(monkeypatch):
    from jobhub.readiness import Readiness

    resume = config.RESUMES / "candidate.pdf"
    resume.write_bytes(b"%PDF-1.7\nfictional fixture")
    db.save_profile(Profile(first_name="Ada", last_name="Lovelace", email="ada@example.test",
                            resume_path=str(resume)))
    db.set_settings({"auto_apply": True, "setup_complete": True, "min_score_to_apply": 60,
                     "target_titles": ["Software Engineer New Grad"]})
    stale = singlestore_posting(title="Software Engineer New Grad", description="Entry-level Python role",
                                score=95, score_json={"auto_apply_eligible": True})
    db.upsert_jobs([stale])
    current = singlestore_posting(title="Senior Staff Software Engineer",
                                  description="Lead architecture; 10+ years experience")
    monkeypatch.setattr("jobhub.sources.import_job.fetch_job", lambda url: current)
    monkeypatch.setattr(scheduler, "rescore", lambda: None)
    monkeypatch.setattr(scheduler, "preflight_job", lambda *args: Readiness("ready"))
    monkeypatch.setattr(scheduler, "resume_for_job", lambda *args:
                        pytest.fail("must not draft a resume for the newly ineligible role"))
    monkeypatch.setattr(scheduler, "apply_to_job", lambda *args, **kwargs:
                        pytest.fail("must not open the employer form for the newly ineligible role"))

    results = scheduler.auto_apply_batch()

    assert len(results) == 1
    assert results[0]["status"] == "needs_human"
    assert results[0]["submission_attempted"] is False
    assert "automatic application criteria" in results[0]["message"]
    assert db.get_job(stale.id).title == "Senior Staff Software Engineer"
    assert db.get_job(stale.id).score_json["auto_apply_eligible"] is False
    assert db.get_application(results[0]["application_id"])["submission_attempted"] == 0


def test_mismatched_public_vacancy_is_rejected_without_overwriting_row(monkeypatch):
    old = singlestore_posting()
    db.upsert_jobs([old])
    changed_identity = Job(
        source="greenhouse", ats="greenhouse", company="SingleStore", title="Different vacancy",
        url="https://job-boards.greenhouse.io/singlestore/jobs/9999999",
        description="Replacement fixture posting", raw={"gh_id": 9999999, "slug": "singlestore"},
    )
    monkeypatch.setattr("jobhub.sources.import_job.fetch_job", lambda url: changed_identity)

    with pytest.raises(scheduler.PostingRefreshError, match="no longer matches"):
        scheduler.enrich_job(db.get_job(old.id))

    current = db.get_job(old.id)
    assert current.title == old.title
    assert current.description == old.description


def test_tailor_api_uses_refreshed_metadata(monkeypatch):
    old = singlestore_posting()
    db.upsert_jobs([old])
    resume = config.RESUMES / "candidate.pdf"
    resume.write_bytes(b"%PDF-1.7\nfictional fixture")
    db.save_profile(Profile(first_name="Ada", last_name="Lovelace", email="ada@example.test",
                            resume_path=str(resume)))
    updated = singlestore_posting(title="Helios New Grad 2027", description="JavaScript, TypeScript, Go")
    monkeypatch.setattr("jobhub.sources.import_job.fetch_job", lambda url: updated)
    seen = []

    def draft(profile, job, settings):
        seen.append(job.title)
        result = config.TAILORED / "fixture.pdf"
        result.write_bytes(b"%PDF-1.7\nfictional fixture")
        return result

    monkeypatch.setattr("jobhub.tailor.resume_for_job", draft)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(f"/api/jobs/{old.id}/tailor")

    assert response.status_code == 200, response.text
    assert seen == ["Helios New Grad 2027"]
