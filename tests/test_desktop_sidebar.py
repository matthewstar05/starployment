"""Sidebar resizing and hiding, JARVIS from the sidebar, role-family presets, and refresh caching."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("JOBHUB_REDUCED_MOTION", "1")

import pytest

from jobhub import config, db, scheduler
from jobhub.desktop.ui import JobHubWindow, Sidebar


@pytest.fixture
def controller(app, store, monkeypatch):
    from jobhub.desktop.controller import DesktopController
    monkeypatch.setattr(scheduler, "rescore", lambda *a, **kw: None)
    window = JobHubWindow()
    instance = DesktopController(window, scheduler_enabled=False, show_setup_on_start=False)
    yield instance
    instance.stop()
    window.close()
    scheduler.STATE.stop = False


def test_sidebar_resizes_within_limits_and_hides_when_dragged_narrow(app):
    window = JobHubWindow()
    window.show()
    window._drop_sidebar(380)
    assert window.sidebar.width() == 380
    window._drop_sidebar(9999)
    assert window.sidebar.width() == Sidebar.MAX_WIDTH
    window._drop_sidebar(Sidebar.MIN_WIDTH - 20)
    assert window.sidebar.width() == Sidebar.MIN_WIDTH
    window._drop_sidebar(40)
    assert window.sidebar.isHidden() and window.sidebar_reveal.isVisible()
    window.toggle_sidebar()
    assert window.sidebar.isVisible() and not window.sidebar_reveal.isVisible()
    assert window.sidebar.width() == Sidebar.MIN_WIDTH     # the last width is kept
    window.toggle_sidebar_action.trigger()
    assert window.sidebar.isHidden()
    window.close()


def test_jarvis_mode_changes_from_the_sidebar_and_all_asks_first(app, monkeypatch):
    window = JobHubWindow()
    chosen: list[str] = []
    window.jarvis_mode_requested.connect(chosen.append)
    window.choose_jarvis_mode("new")
    assert chosen == ["new"] and "NEW" in window.sidebar.jarvis_status.text()
    assert window.settings.jarvis_mode.currentData() == "new" and not window.settings.dirty
    monkeypatch.setattr(window, "_confirm_jarvis_all", lambda: False)
    window.choose_jarvis_mode("all")
    assert chosen == ["new"]
    monkeypatch.setattr(window, "_confirm_jarvis_all", lambda: True)
    window.choose_jarvis_mode("all")
    assert chosen == ["new", "all"] and "ALL" in window.sidebar.jarvis_status.text()
    window.close()


def test_controller_saves_only_the_jarvis_mode(controller):
    controller.window.settings.target_titles.set_values(["unsaved edit"])
    controller.set_jarvis_mode("all")
    saved = db.get_settings()
    assert saved["jarvis_mode"] == "all" and "unsaved edit" not in saved["target_titles"]


def test_role_family_presets_append_titles_without_duplicates(app):
    window = JobHubWindow()
    view = window.settings
    view.set_settings(config.merge_settings(config.DEFAULT_SETTINGS, {"target_titles": ["financial analyst"]}))
    view.add_role_family("Accounting")
    view.add_role_family("Finance & FP&A")
    titles = view.target_titles.values()
    assert "staff accountant" in titles and titles.count("financial analyst") == 1
    assert view.dirty
    view.company_cap.setValue(2)
    patches: list[dict] = []
    view.save_requested.connect(patches.append)
    view._save()
    assert patches[0]["company_cap"] == {"count": 2} and patches[0]["mix_role_families"] is True
    assert patches[0]["prune"] == {"enabled": True}
    window.close()


def test_refresh_skips_unchanged_applications_drafts_and_tracker(controller, monkeypatch):
    calls: list[str] = []
    for name in ("list_applications", "list_drafts", "list_tracker"):
        real = getattr(db, name)
        monkeypatch.setattr(db, name, lambda *a, _real=real, _name=name, **kw: (calls.append(_name), _real(*a, **kw))[1])
    controller.refresh(include_account=True)      # a full refresh always reads everything
    assert calls == ["list_applications", "list_drafts", "list_tracker"]
    calls.clear()
    controller.refresh()
    controller.refresh()
    assert calls == []
    db.add_manual_application(company="Northwind", title="Analyst")
    calls.clear()
    controller.refresh()
    assert "list_applications" in calls and "list_tracker" in calls


def test_background_refresh_keeps_the_page_and_reloads_jobs_off_the_window_thread(controller):
    import time as _time
    from PySide6.QtWidgets import QApplication
    from jobhub.models import Job
    db.upsert_jobs([Job(company="Northwind", title="Analyst", source="greenhouse", ats="greenhouse",
                        url="https://job-boards.greenhouse.io/northwind/jobs/1")])
    controller.refresh()
    assert controller.window.jobs.total_jobs == 1
    db.upsert_jobs([Job(company="Bluebird", title="Analyst", source="greenhouse", ats="greenhouse",
                        url="https://job-boards.greenhouse.io/bluebird/jobs/2")])
    controller.refresh(wait=False)
    assert controller.window.jobs.total_jobs == 1          # the current page stays while it reloads
    deadline = _time.monotonic() + 5
    while controller.window.jobs.total_jobs != 2 and _time.monotonic() < deadline:
        QApplication.processEvents()
        _time.sleep(0.02)
    assert controller.window.jobs.total_jobs == 2


def test_quitting_waits_only_for_an_application_being_sent(store):
    from jobhub.desktop.__main__ import sending_now
    from jobhub.models import Job
    assert sending_now() is False
    job = Job(company="Northwind", title="Analyst", source="greenhouse", ats="greenhouse",
              url="https://job-boards.greenhouse.io/northwind/jobs/9")
    db.upsert_jobs([job])
    db.reserve_application(job.id)
    assert sending_now() is True
