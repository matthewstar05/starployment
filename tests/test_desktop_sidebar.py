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
    monkeypatch.setattr(window, "_confirm_jarvis_all", lambda *a: False)
    window.choose_jarvis_mode("all")
    assert chosen == ["new"]
    monkeypatch.setattr(window, "_confirm_jarvis_all", lambda *a: True)
    window.choose_jarvis_mode("all")
    assert chosen == ["new", "all"] and "ALL" in window.sidebar.jarvis_status.text()
    window.close()


def test_all_plus_asks_with_its_own_sheet_and_all_to_all_plus_still_asks(app, monkeypatch):
    window = JobHubWindow()
    asked: list[str] = []
    chosen: list[str] = []
    window.jarvis_mode_requested.connect(chosen.append)
    monkeypatch.setattr(window, "_confirm_jarvis_all", lambda mode="all": asked.append(mode) or True)
    window.choose_jarvis_mode("all")
    window.choose_jarvis_mode("all_plus")          # a faster, wider mode asks again
    window.choose_jarvis_mode("all")               # stepping back down keeps the permission
    assert asked == ["all", "all_plus"] and chosen == ["all", "all_plus", "all"]
    window.choose_jarvis_mode("all_plus")
    assert "ALL+" in window.sidebar.jarvis_status.text()
    window.close()


def test_jarvis_confirm_sheet_defaults_to_cancel(app):
    from jobhub.desktop.views.jarvis_sheet import JarvisConfirmSheet
    for mode, rows in (("all", 4), ("all_plus", 5)):
        sheet = JarvisConfirmSheet(mode)
        assert len(sheet.rows) == rows and sheet.cancel.isDefault() and not sheet.confirm.autoDefault()
        assert ("ALL+" in sheet.title.text()) == (mode == "all_plus")
        sheet.close()


def test_jarvis_pill_opens_a_glass_popover_not_a_native_menu(app, monkeypatch):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    window = JobHubWindow()
    window.show()
    chosen: list[str] = []
    window.jarvis_mode_requested.connect(chosen.append)
    window.sidebar.jarvis_status.clicked.emit()
    popover = window._jarvis_popover
    assert popover.isVisible() and popover.windowFlags() & Qt.WindowType.Popup
    assert len(popover._rows) == 4 and popover._hover == 0         # the current mode (Off) is highlighted
    popover.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier))
    popover.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier))
    assert chosen == ["new"] and not popover.isVisible()
    opened: list[bool] = []
    monkeypatch.setattr(window, "show_jarvis_settings", lambda: opened.append(True))
    window.sidebar.jarvis_status.clicked.emit()
    window._jarvis_popover.activate(4)                              # the footer row
    assert opened == [True] and chosen == ["new"]
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


def test_quitting_during_a_send_exits_once_it_finishes(monkeypatch):
    from jobhub.desktop import __main__ as entry
    answers = iter([True, True, False])
    exits: list[int] = []
    monkeypatch.setattr(entry, "sending_now", lambda: next(answers))
    monkeypatch.setattr(entry.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(entry.os, "_exit", exits.append)
    entry.exit_promptly(0)
    assert exits == [0]     # it never falls back to waiting on idle worker threads


def test_assessment_emails_without_an_application_show_in_progress_and_open_the_email(app):
    from PySide6.QtWidgets import QLabel
    from jobhub.desktop.ui import TRACKER
    from jobhub.desktop.widgets import ActionRow, NAV_BADGE_ROLE
    window = JobHubWindow()
    window.dashboard.set_settings({"email_verification": {"imap_host": "imap.gmail.com",
                                                          "imap_username": "ada@example.test"}})
    inbox = [{"id": 7, "received_at": "2026-10-28T10:00:00+00:00", "sender": "noreply@litmus.build",
              "subject": "[Litmus] Assessment invitation from Northwind", "stage": "assessment",
              "message_id": "abc@litmus.build", "employer": "Northwind"}]
    opened: list[str] = []
    window.dashboard.open_url_requested.connect(opened.append)
    window.set_data(drafts=[], tracker=[], inbox=inbox)
    card = window.dashboard.focus_card
    assert not card.isHidden()
    texts = " ".join(label.text() for label in card.findChildren(QLabel))
    assert "Northwind" in texts and "not linked to an application" in texts
    assert window.nav.item(TRACKER).data(NAV_BADGE_ROLE) == "1"
    card.findChildren(ActionRow)[0].clicked.emit()
    assert opened and "mail.google.com" in opened[0]
    window.close()


def test_the_quit_path_works_when_run_as_the_bundle_entry_script():
    """In the app bundle __main__.py is the top-level script, so relative imports fail there."""
    import ast
    import inspect
    from jobhub.desktop import __main__ as entry
    tree = ast.parse(inspect.getsource(entry))
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level]
