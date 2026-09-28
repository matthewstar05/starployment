"""Controller behavior behind the redesigned desktop UI."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def controller(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from jobhub import config, db, scheduler
    from jobhub.desktop.controller import DesktopController
    from jobhub.desktop.ui import JobHubWindow

    monkeypatch.setenv("JOBHUB_REDUCED_MOTION", "1")
    db.close()
    for name, value in {
        "DATA": tmp_path, "DB_PATH": tmp_path / "test.db", "RESUMES": tmp_path / "resumes",
        "TAILORED": tmp_path / "tailored", "SCREENSHOTS": tmp_path / "screenshots",
    }.items():
        monkeypatch.setattr(config, name, value)
    db.init()
    rescored = []
    monkeypatch.setattr(scheduler, "rescore", lambda: rescored.append(True))
    app = QApplication.instance() or QApplication([])
    window = JobHubWindow()
    instance = DesktopController(window, scheduler_enabled=False, show_setup_on_start=False)
    instance.rescored = rescored
    yield instance
    instance.stop()
    window.close()
    db.close()
    scheduler.STATE.stop = False
    from jobhub.desktop.styles import apply_appearance

    apply_appearance(app, "Light")


def test_appearance_applies_and_persists_immediately(controller):
    from PySide6.QtWidgets import QApplication

    from jobhub import db

    controller.set_appearance("Dark")
    assert db.get_settings()["appearance"] == "Dark"
    assert QApplication.instance().property("jobhubResolvedAppearance") == "Dark"
    assert controller.rescored == []


def test_rescore_runs_only_when_ranking_inputs_change(controller):
    controller.save_settings({"dry_run": True, "scan_interval_min": 30})
    assert controller.rescored == []
    controller.save_settings({"target_titles": ["Data Engineer"]})
    assert controller.rescored == [True]


def test_skip_and_restore_from_the_jobs_menu(controller):
    from jobhub import db
    from jobhub.models import Job

    job = Job(source="fixture", company="Example", title="Analyst", url="https://example.test/jobs/1")
    db.upsert_jobs([job])
    controller.set_job_status(job.id, "skipped")
    assert db.get_job(job.id).status == "skipped"
    assert controller.window.toast.tone == "success"
    controller.set_job_status(job.id, "new")
    assert db.get_job(job.id).status == "new"


def test_status_messages_use_toasts_with_tones(controller):
    controller._status("Could not refresh", tone="error")
    assert controller.window.toast.text == "Could not refresh"
    assert controller.window.toast.tone == "error"
