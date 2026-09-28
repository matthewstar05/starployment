"""The packaged app may import its checkout's data without bundling private state."""

from __future__ import annotations

import json
import os
import stat
import sqlite3
from pathlib import Path

from jobhub import db
from jobhub.migration import bundled_checkout_data, migrate_source_checkout
from jobhub.models import Profile


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "checkout" / "data"
    resume = source / "resumes" / "candidate.pdf"
    resume.parent.mkdir(parents=True)
    resume.write_bytes(b"%PDF-1.4\nlocal fixture")
    screenshot = source / "screenshots" / "confirmation.png"
    screenshot.parent.mkdir()
    screenshot.write_bytes(b"PNG fixture")
    (source / "browser_profile").mkdir()
    (source / "browser_profile" / "cookies.db").write_bytes(b"private")
    (source / ".env").write_text("PRIVATE=value\n")
    with sqlite3.connect(source / "jobhub.db") as con:
        con.executescript(db.SCHEMA)
        profile = Profile(first_name="Test", last_name="Candidate", email="test@example.org",
                          city="San Luis Obispo", country="United States", resume_path=str(resume))
        con.execute("INSERT INTO profile(id,data) VALUES(1,?)", (profile.model_dump_json(),))
        con.execute("INSERT INTO settings(key,value) VALUES('target_titles',?)",
                    (json.dumps(["software engineer"]),))
        con.execute("INSERT INTO jobs(id,source,ats,company,title,url,status) VALUES(?,?,?,?,?,?,?)",
                    ("job-1", "greenhouse", "greenhouse", "Fixture", "Engineer", "https://example.org/job", "applied"))
        con.execute("INSERT INTO applications(job_id,status,resume_path,screenshots) VALUES(?,?,?,?)",
                    ("job-1", "applied", str(resume), json.dumps([str(screenshot)])))
    return source


def test_checkout_detection_requires_bundle_inside_dist(tmp_path: Path):
    source = _source(tmp_path)
    checkout = source.parent
    (checkout / "pyproject.toml").write_text("[project]\nname='jobhub'\n")
    (checkout / "jobhub").mkdir()
    (checkout / "jobhub" / "setup.py").touch()
    exe = checkout / "dist" / "JobHub.app" / "Contents" / "MacOS" / "JobHub"
    exe.parent.mkdir(parents=True)
    exe.touch()
    assert bundled_checkout_data(exe) == source.resolve()
    assert bundled_checkout_data(tmp_path / "Applications" / "JobHub.app" / "Contents" / "MacOS" / "JobHub") is None


def test_one_time_import_keeps_history_and_relocates_artifacts(tmp_path: Path):
    source = _source(tmp_path)
    target = tmp_path / "installed"
    target.mkdir()
    with sqlite3.connect(target / "jobhub.db") as con:
        con.executescript(db.SCHEMA)
        con.execute("INSERT INTO profile(id,data) VALUES(1,?)", (Profile(country="United States").model_dump_json(),))
    assert migrate_source_checkout(source, target)
    assert not migrate_source_checkout(source, target)
    assert (target / "resumes" / "candidate.pdf").is_file()
    assert (target / "screenshots" / "confirmation.png").is_file()
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o700
        assert stat.S_IMODE((target / "resumes" / "candidate.pdf").stat().st_mode) == 0o600
        assert stat.S_IMODE((target / "screenshots" / "confirmation.png").stat().st_mode) == 0o600
    assert not (target / "browser_profile").exists()
    assert not (target / ".env").exists()
    assert list(target.glob("jobhub-before-import-*.db"))
    with sqlite3.connect(target / "jobhub.db") as con:
        profile = Profile.model_validate_json(con.execute("SELECT data FROM profile").fetchone()[0])
        assert profile.resume_path == str(target / "resumes" / "candidate.pdf")
        assert con.execute("SELECT status FROM jobs").fetchone()[0] == "applied"
        resume_path, screenshots = con.execute("SELECT resume_path,screenshots FROM applications").fetchone()
        assert resume_path == profile.resume_path
        assert json.loads(screenshots) == [str(target / "screenshots" / "confirmation.png")]


def test_existing_identity_is_never_overwritten(tmp_path: Path):
    source = _source(tmp_path)
    target = tmp_path / "installed"
    target.mkdir()
    with sqlite3.connect(target / "jobhub.db") as con:
        con.executescript(db.SCHEMA)
        con.execute("INSERT INTO profile(id,data) VALUES(1,?)", (Profile(first_name="Existing").model_dump_json(),))
    assert not migrate_source_checkout(source, target)
    with sqlite3.connect(target / "jobhub.db") as con:
        profile = Profile.model_validate_json(con.execute("SELECT data FROM profile").fetchone()[0])
        assert profile.first_name == "Existing"
