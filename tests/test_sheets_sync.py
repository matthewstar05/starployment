"""Google Sheets sync: confirmed rows, deltas, and the desktop hook. No network."""
from __future__ import annotations

import json
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("JOBHUB_REDUCED_MOTION", "1")

import httpx
import pytest
from pydantic import ValidationError

from jobhub import config, db, sheets_sync
from jobhub.models import Job

URL = "https://script.google.com/macros/s/AKfycbTestSheetSync0001/exec"
URL_2 = "https://script.google.com/macros/s/AKfycbTestSheetSync0002/exec"
_RESULT = "https://script.googleusercontent.com/macros/echo?id=1"


def _enable(**overrides):
    sheet = {"enabled": True, "web_app_url": URL, "tab": "Applications"}
    sheet.update(overrides)
    return db.set_settings({"google_sheets": sheet})


def _token(store) -> str:
    data = json.loads((store / "secrets.json").read_text(encoding="utf-8"))
    return data["Starployment Google Sheets"]["token"]


class _WebApp:
    """Apps Script stand-in: a POST is redirected, and the following GET returns JSON."""

    def __init__(self, token: str):
        self.token = token
        self.posts: list[dict] = []
        self.urls: list[str] = []
        self.gets = 0
        self.known: set[str] = set()
        self.fail: Exception | None = None
        self.bad_token = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            if self.fail is not None:
                raise self.fail
            self.posts.append(json.loads(request.content.decode()))
            self.urls.append(str(request.url))
            return httpx.Response(302, headers={"Location": _RESULT})
        self.gets += 1
        assert "script.googleusercontent.com" in str(request.url)
        post = self.posts[-1]
        if self.bad_token or post.get("token") != self.token:
            return httpx.Response(200, json={"ok": False, "error": "bad token"})
        if post.get("action") == "ping":
            return httpx.Response(200, json={"ok": True, "title": "Job Search Tracker",
                                             "url": "https://docs.google.com/spreadsheets/d/sheet"})
        rows = post.get("rows") or []
        updated = appended = 0
        id_at = sheets_sync.COLUMNS.index("Starployment ID")
        for row in rows:
            sid = str(row[id_at])
            if sid in self.known:
                updated += 1
            else:
                self.known.add(sid)
                appended += 1
        return httpx.Response(200, json={"ok": True, "updated": updated, "appended": appended,
                                         "title": "Job Search Tracker", "url": "https://docs.google.com/spreadsheets/d/sheet"})


def _install(monkeypatch, app: _WebApp) -> None:
    monkeypatch.setattr(sheets_sync, "_transport", lambda: httpx.MockTransport(app))


def _applied_manual(company: str, title: str, **kwargs) -> int:
    return db.add_manual_application(company=company, title=title, location=kwargs.pop("location", "New York, NY"), **kwargs)


def test_google_sheets_settings_accept_an_exec_url_only():
    base = config.merge_settings(config.DEFAULT_SETTINGS, {})
    assert base["google_sheets"] == {"enabled": False, "web_app_url": "", "tab": "Applications", "target": ""}
    saved = config.validate_settings(base)
    assert saved["google_sheets"]["tab"] == "Applications"
    turned_on = config.merge_settings(base, {"google_sheets": {"enabled": True, "web_app_url": "", "tab": "A" * 100}})
    assert config.validate_settings(turned_on)["google_sheets"]["enabled"] is True
    linked = config.merge_settings(base, {"google_sheets": {"enabled": True, "web_app_url": URL, "tab": "Applications"}})
    assert config.validate_settings(linked)["google_sheets"]["web_app_url"] == URL
    for bad in (
        {"web_app_url": "http://script.google.com/macros/s/Abc/exec"},
        {"web_app_url": "https://script.google.com/macros/s/Abc/exec/"},
        {"web_app_url": "https://example.test/exec"},
        {"tab": ""},
        {"tab": "   "},
        {"tab": "x" * 101},
    ):
        with pytest.raises(ValidationError):
            config.validate_settings(config.merge_settings(base, {"google_sheets": bad}))


def test_sheet_sync_table_remembers_fingerprints(store):
    assert db.sheet_sync_state() == {}
    db.mark_sheet_synced([(3, "abc"), (4, "def")])
    assert db.sheet_sync_state() == {3: "abc", 4: "def"}
    db.mark_sheet_synced([(3, "zzz")])
    assert db.sheet_sync_state() == {3: "zzz", 4: "def"}
    db.clear_sheet_sync()
    assert db.sheet_sync_state() == {}


def test_setup_script_keeps_the_token_in_the_vault_only(store):
    first = sheets_sync.setup_script()
    assert sheets_sync.setup_script() == first
    token = _token(store)
    assert token in first and "{{TOKEN}}" not in first
    assert sheets_sync.APPS_SCRIPT.count("{{TOKEN}}") == 1
    for phrase in ("function doPost", "LockService.getScriptLock", "setValues", "Starployment ID",
                   'body.action === "ping"'):
        assert phrase in sheets_sync.APPS_SCRIPT
    assert token.encode() not in (store / "test.db").read_bytes()
    assert token not in json.dumps(db.get_settings())


def test_rows_are_confirmed_applications_and_formula_safe(store):
    manual = _applied_manual("@Initech", "=Analyst", salary_min=120_000, salary_max=150_000)
    db.set_application_note(manual, "+ping")
    _applied_manual("Hooli", "A+B Analyst", url="https://example.test/jobs/plain")
    scored = Job(source="greenhouse", ats="greenhouse", company="Hooli", title="-Researcher",
                 location="Chicago, IL", url="https://example.test/jobs/hooli", score=82.4)
    db.upsert_jobs([scored])
    applied = db.create_application(scored.id, "engine")
    db.mark_submission_attempted(scored.id)
    db.finish_application(applied, "applied", ["submitted"], [], [], submission_attempted=True)
    pending = Job(source="greenhouse", ats="greenhouse", company="Pending Co", title="Associate",
                  location="Chicago, IL", url="https://example.test/jobs/pending")
    db.upsert_jobs([pending])
    attempt = db.create_application(pending.id, "engine")
    db.mark_submission_attempted(pending.id)
    db.finish_application(attempt, "needs_human", ["stopped"], [], [], submission_attempted=True)

    found = {row[1]: row for row in sheets_sync.rows()}
    assert "'=Analyst" in found and "A+B Analyst" in found and "'-Researcher" in found
    assert "Associate" not in found and not any(row[0] == "Pending Co" for row in found.values())
    initech = found["'=Analyst"]
    assert initech[0] == "'@Initech"
    assert initech[3] == "$120k\u2013$150k"
    assert initech[7] == "manual"
    assert initech[9] == ""
    assert initech[10] == "'+ping"
    assert initech[-1] == str(manual)
    plain = found["A+B Analyst"]
    assert plain[0] == "Hooli"
    assert plain[9] == "https://example.test/jobs/plain"
    researcher = found["'-Researcher"]
    assert researcher[7] == "greenhouse"
    assert researcher[8] == "82"
    assert researcher[9] == "https://example.test/jobs/hooli"
    assert sheets_sync.COLUMNS == (
        "Company", "Role", "Location", "Salary", "Applied", "Stage", "Stage updated",
        "Source", "Match", "Posting URL", "Note", "Starployment ID",
    )


def test_sync_sends_changes_only_and_resets_when_the_sheet_changes(store, monkeypatch):
    sheets_sync.setup_script()
    app = _WebApp(_token(store))
    _install(monkeypatch, app)
    manual = _applied_manual("Initech", "Analyst", salary_min=80_000, salary_max=80_000)
    db.set_application_note(manual, "first")
    _applied_manual("Hooli", "Researcher", url="https://example.test/jobs/hooli")
    pending = Job(source="fixture", company="Pending Co", title="Associate", location="Chicago, IL",
                  url="https://example.test/jobs/pending")
    db.upsert_jobs([pending])
    attempt = db.create_application(pending.id, "engine")
    db.mark_submission_attempted(pending.id)
    db.finish_application(attempt, "needs_human", ["stopped"], [], [], submission_attempted=True)
    _enable()

    first = sheets_sync.sync(db.get_settings())
    assert first.sent == 2 and first.appended == 2 and first.updated == 0 and first.total == 2
    assert "added 2" in first.summary
    assert len(app.posts) == 1 and app.gets == 1
    sent_rows = app.posts[0]["rows"]
    assert app.posts[0]["columns"] == list(sheets_sync.COLUMNS)
    assert app.posts[0]["tab"] == "Applications"
    assert {row[0] for row in sent_rows} == {"Initech", "Hooli"}
    assert "Pending Co" not in {row[0] for row in sent_rows}
    initech = next(row for row in sent_rows if row[0] == "Initech")
    assert initech[3] == "$80k"
    state = db.sheet_sync_state()
    assert set(state) == {int(row[-1]) for row in sheets_sync.rows()}
    assert all(state[int(row[-1])] == sheets_sync.fingerprint(row) for row in sheets_sync.rows())

    again = sheets_sync.sync(db.get_settings())
    assert again.sent == 0 and len(app.posts) == 1
    assert again.summary.startswith("Google Sheet is up to date")

    db.set_application_note(manual, "called back")
    changed = sheets_sync.sync(db.get_settings())
    assert changed.sent == 1 and changed.updated == 1 and changed.appended == 0
    assert app.posts[-1]["rows"][0][10] == "called back"
    assert len(app.posts) == 2

    previous = db.get_settings()["google_sheets"]["target"]
    assert len(previous) == 64
    _enable(web_app_url=URL_2)
    assert db.get_settings()["google_sheets"]["target"] == previous  # the hash updates when the sync runs
    reset = sheets_sync.sync(db.get_settings())
    assert reset.sent == 2
    assert app.urls[-1] == URL_2
    assert len(app.posts) == 3
    assert db.get_settings()["google_sheets"]["target"] != previous

    full = sheets_sync.sync(db.get_settings(), full=True)
    assert full.sent == 2 and len(app.posts) == 4

    _enable(web_app_url=URL_2, tab="Tracker")
    retab = sheets_sync.sync(db.get_settings())
    assert retab.sent == 2 and app.posts[-1]["tab"] == "Tracker"
    assert app.gets == len(app.posts)


def test_sync_posts_rows_in_batches_of_500(store, monkeypatch):
    assert sheets_sync.BATCH == 500
    monkeypatch.setattr(sheets_sync, "BATCH", 2)
    sheets_sync.setup_script()
    app = _WebApp(_token(store))
    _install(monkeypatch, app)
    for number in range(5):
        _applied_manual(f"Company {number}", f"Role {number}", url=f"https://example.test/jobs/{number}")
    _enable()
    report = sheets_sync.sync(db.get_settings())
    assert report.sent == 5 and report.total == 5 and report.appended == 5
    assert [len(post["rows"]) for post in app.posts] == [2, 2, 1]


def test_bad_token_and_offline_are_distinct(store, monkeypatch):
    sheets_sync.setup_script()
    _applied_manual("Initech", "Analyst")
    _enable()
    app = _WebApp(_token(store))
    app.bad_token = True
    _install(monkeypatch, app)
    ok, message = sheets_sync.test(db.get_settings())
    assert ok is False and "bad token" in message and not message.startswith(sheets_sync.OFFLINE)
    with pytest.raises(sheets_sync.SheetSyncError, match="bad token") as refused:
        sheets_sync.sync(db.get_settings())
    assert not str(refused.value).startswith(sheets_sync.OFFLINE)
    assert db.sheet_sync_state() == {}

    app.bad_token = False
    app.fail = httpx.ConnectError("connection refused")
    ok, message = sheets_sync.test(db.get_settings())
    assert ok is False and message.startswith(sheets_sync.OFFLINE)
    with pytest.raises(sheets_sync.SheetSyncError) as offline:
        sheets_sync.sync(db.get_settings())
    assert str(offline.value).startswith(sheets_sync.OFFLINE)
    assert "bad token" not in str(offline.value)


@pytest.fixture
def controller(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

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
    app = QApplication.instance() or QApplication([])
    window = JobHubWindow()
    instance = DesktopController(window, scheduler_enabled=False, show_setup_on_start=False)
    yield instance
    instance.stop()
    window.close()
    window.deleteLater()
    db.close()
    from jobhub import scheduler
    scheduler.STATE.stop = False
    from jobhub.desktop.styles import apply_appearance
    apply_appearance(app, "Light")


def test_sheet_test_uses_the_quick_pool():
    from jobhub.desktop.controller import QUICK_TASKS
    assert "sheet_test" in QUICK_TASKS


def test_scan_waits_until_mail_and_workday_finish(controller, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from jobhub import scheduler

    monkeypatch.setattr(scheduler, "scan_once", lambda: {"status": "deferred", "found": 0, "new": 0})
    monkeypatch.setattr(controller, "sync_email", lambda manual=False: controller._mail_syncing.set())
    monkeypatch.setattr(controller, "check_workday", lambda manual=False: controller._portal_checking.set())
    started = []
    monkeypatch.setattr(controller, "sync_sheet", lambda manual=False, full=False: started.append((manual, full)) or True)
    db.set_settings({"setup_complete": True, "email_tracking": {"enabled": True},
                     "google_sheets": {"enabled": True, "web_app_url": URL, "tab": "Applications"}})
    controller.scan()
    assert controller._sheet_after_checks is True
    assert started == []
    controller._mail_syncing.clear()
    controller._maybe_sync_sheet()
    assert started == []  # Workday is still reading
    controller._portal_checking.clear()
    controller._maybe_sync_sheet()
    assert started == [(False, False)]
    deadline = time.time() + 5
    while controller._pending_scan and time.time() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)


def test_sheet_results_are_quiet_unless_the_sync_was_asked_for_or_sent_rows(controller):
    panel_state = {}

    class Panel:
        def set_sync_state(self, stamp, total):
            panel_state["stamp"] = stamp
            panel_state["total"] = total

    controller.refresh = lambda *args, **kwargs: None
    toasts = []
    controller._status = lambda message, timeout=6000, tone="info": toasts.append((message, tone))
    controller.window.settings.sheets = Panel()
    quiet = sheets_sync.SheetSyncReport(0, 0, 0, 4, "Google Sheet is up to date · 4 applications")
    controller._assist_finished("sheet_sync", False, quiet)
    assert panel_state["total"] == 4 and panel_state["stamp"]
    assert toasts == []
    assert not [event for event in db.list_events(20) if "Google Sheet" in event["msg"]]

    controller._assist_finished("sheet_sync", True, quiet)
    assert toasts[-1][0] == quiet.summary and toasts[-1][1] == "info"
    assert any(event["msg"] == quiet.summary and event["level"] == "info" for event in db.list_events(20))

    sent = sheets_sync.SheetSyncReport(2, 1, 1, 4, "Google Sheet added 1 and updated 1 · 4 applications")
    controller._assist_finished("sheet_sync", False, sent)
    assert toasts[-1] == (sent.summary, "success")

    controller._assist_failed("sheet_sync", False, f"{sheets_sync.OFFLINE} (connection refused)")
    assert toasts[-1][0] == sent.summary  # no new toast
    failure = [event for event in db.list_events(20) if event["msg"].startswith("Google Sheet sync failed")]
    assert failure and failure[0]["level"] == "warn"
    assert not any(event["level"] == "error" and "Google Sheet" in event["msg"] for event in db.list_events(20))

    controller._assist_failed("sheet_sync", True, "bad token")
    assert toasts[-1][1] == "warning"
    assert "bad token" in toasts[-1][0]


def test_settings_signals_copy_the_script_test_and_sync(controller, monkeypatch):
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication

    class Panel(QObject):
        copy_script_requested = Signal()
        test_requested = Signal(dict)
        sync_requested = Signal(bool)

        def __init__(self):
            super().__init__()
            self.result = None

        def set_test_result(self, ok, message):
            self.result = (ok, message)

    panel = Panel()
    controller._wire_sheets(panel)
    seen = {}

    def fake_test(settings):
        seen["url"] = (settings.get("google_sheets") or {}).get("web_app_url")
        seen["has_titles"] = "target_titles" in settings
        return True, "Connected to Job Search Tracker"

    monkeypatch.setattr(sheets_sync, "test", fake_test)
    panel.test_requested.emit({"google_sheets": {"web_app_url": URL, "tab": "Applications", "enabled": True}})
    deadline = time.time() + 5
    while panel.result is None and time.time() < deadline:
        QApplication.processEvents()
        time.sleep(0.02)
    assert panel.result == (True, "Connected to Job Search Tracker")
    assert seen == {"url": URL, "has_titles": True}

    calls = []
    controller.sync_sheet = lambda manual=False, full=False: calls.append((manual, full)) or True
    panel.sync_requested.emit(True)
    panel.sync_requested.emit(False)
    assert calls == [(True, True), (True, False)]

    panel.copy_script_requested.emit()
    text = QApplication.clipboard().text()
    secret = json.loads((config.DATA / "secrets.json").read_text(encoding="utf-8"))
    token = secret["Starployment Google Sheets"]["token"]
    assert "function doPost" in text and "{{TOKEN}}" not in text and token in text
    assert token.encode() not in config.DB_PATH.read_bytes()
