"""Immutable per-application upload copies, isolated from candidate data."""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobhub import config, db, scheduler
from jobhub.models import ApplyResult, Job, Profile
from jobhub.server import app
from jobhub.tailor import snapshot_resume_for_application


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
    scheduler.STATE.running_apply = None
    yield
    db.close()


def local_job():
    return Job(source="fixture", ats="unknown", company="Example", title="Engineer",
               url="https://example.test/jobs/role-1", description="Local fixture posting")


def test_future_per_job_draft_cannot_change_existing_application_snapshot():
    job = local_job()
    db.upsert_jobs([job])
    application_id = db.reserve_application(job.id)
    deterministic_draft = config.TAILORED / f"Candidate_Example_{job.id}.pdf"
    deterministic_draft.write_bytes(b"first tailored PDF bytes")

    snapshot = Path(snapshot_resume_for_application(str(deterministic_draft), application_id))
    deterministic_draft.write_bytes(b"later regenerated PDF bytes")
    repeated = Path(snapshot_resume_for_application(str(deterministic_draft), application_id))

    assert repeated == snapshot
    assert snapshot.read_bytes() == b"first tailored PDF bytes"
    if os.name != "nt":
        assert snapshot.stat().st_mode & 0o777 == 0o600
        assert snapshot.parent.stat().st_mode & 0o777 == 0o700


def test_apply_uploads_and_records_snapshot_of_original_resume_fallback(monkeypatch):
    original = config.RESUMES / "candidate.pdf"
    original.write_bytes(b"original resume fixture bytes")
    db.save_profile(Profile(first_name="Ada", last_name="Lovelace", email="ada@example.test",
                            resume_path=str(original)))
    job = local_job()
    db.upsert_jobs([job])
    uploads = []

    def fake_apply(job, profile, settings, resume_path, dry_run=None, assist=False):
        path = Path(resume_path)
        uploads.append((path, path.read_bytes()))
        return ApplyResult(status="dry_run", message="Local fixture filled")

    monkeypatch.setattr(scheduler, "apply_to_job", fake_apply)

    result = scheduler.apply_one(job.id, dry_run=True)

    assert result["status"] == "dry_run"
    snapshot, uploaded_bytes = uploads[0]
    record = db.get_application(result["application_id"])
    assert Path(record["resume_path"]) == snapshot
    assert uploaded_bytes == b"original resume fixture bytes"
    assert snapshot.read_bytes() == uploaded_bytes
    original.write_bytes(b"candidate resume changed later")
    assert snapshot.read_bytes() == uploaded_bytes

    # A later application can regenerate the same deterministic per-job draft
    # path. Its application snapshot must remain independently available.
    tailored_path = config.TAILORED / f"Candidate_Example_{job.id}.pdf"
    tailored_path.write_bytes(b"later application's tailored PDF bytes")
    monkeypatch.setattr(scheduler, "resume_for_job", lambda *args: str(tailored_path))
    later = scheduler.apply_one(job.id, dry_run=True)
    assert later["status"] == "dry_run"
    later_record = db.get_application(later["application_id"])
    later_snapshot = Path(later_record["resume_path"])
    tailored_path.write_bytes(b"a future draft overwrote the per-job source")
    assert later_snapshot.read_bytes() == b"later application's tailored PDF bytes"

    with TestClient(app, base_url="http://127.0.0.1") as client:
        old_resume = client.get("/api/resume/file", params={"path": record["resume_path"]})
        later_resume = client.get("/api/resume/file", params={"path": later_record["resume_path"]})
    assert old_resume.status_code == later_resume.status_code == 200
    assert old_resume.content == b"original resume fixture bytes"
    assert later_resume.content == b"later application's tailored PDF bytes"
    if os.name != "nt":
        assert snapshot.stat().st_mode & 0o777 == 0o600
        assert later_snapshot.stat().st_mode & 0o777 == 0o600
