"""Google Sheets connection panel: values, buttons, and the sync caption."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_sheets_connection_round_trip(app):
    from jobhub.desktop.views.connections import SheetsConnection

    widget = SheetsConnection()
    assert widget.values() == {"google_sheets": {"enabled": False, "web_app_url": "", "tab": "Applications"}}
    widget.set_values({"google_sheets": {
        "enabled": True,
        "web_app_url": "  https://script.google.com/macros/s/abc/exec  ",
        "tab": " Roles ",
        "target": "kept-in-the-vault-side-only",
    }})
    assert widget.values() == {"google_sheets": {
        "enabled": True,
        "web_app_url": "https://script.google.com/macros/s/abc/exec",
        "tab": "Roles",
    }}
    widget.set_values({})
    assert widget.values()["google_sheets"] == {"enabled": False, "web_app_url": "", "tab": "Applications"}
    widget.close()
    widget.deleteLater()
    app.processEvents()


def test_sheets_buttons_emit_their_signals(app):
    from jobhub.desktop.views.connections import SheetsConnection

    widget = SheetsConnection()
    copied: list[bool] = []
    tested: list[dict] = []
    synced: list[bool] = []
    edited: list[bool] = []
    widget.copy_script_requested.connect(lambda: copied.append(True))
    widget.test_requested.connect(tested.append)
    widget.sync_requested.connect(synced.append)
    widget.changed.connect(lambda: edited.append(True))
    widget.url.setText("https://script.google.com/macros/s/abc/exec")
    assert edited == [True]
    widget.copy_script.click()
    widget.status.action.click()
    widget.sync_now.click()
    widget.resend.click()
    assert copied == [True]
    assert tested == [{"google_sheets": {
        "enabled": False,
        "web_app_url": "https://script.google.com/macros/s/abc/exec",
        "tab": "Applications",
    }}]
    assert synced == [False, True]
    assert edited == [True]
    widget.close()
    widget.deleteLater()
    app.processEvents()


def test_sheets_test_result_and_sync_caption(app):
    from jobhub.desktop.format import relative_time
    from jobhub.desktop.views.connections import SheetsConnection

    widget = SheetsConnection()
    assert widget.caption.text() == "Not synced yet"
    widget.set_test_result(True, "Job Search Tracker")
    assert widget.status.title.text() == "Sheet connected"
    assert widget.status.detail.text() == "Job Search Tracker"
    assert widget.status.action.text() == "Test again"
    assert widget.status.dot._tone == "green"
    widget.set_test_result(False, "The sheet did not answer.")
    assert widget.status.title.text() == "Could not reach the sheet"
    assert widget.status.detail.text() == "The sheet did not answer."
    assert widget.status.dot._tone == "red"
    when = (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat()
    widget.set_sync_state(when, 4)
    assert widget.caption.text() == f"Last synced {relative_time(when)} · 4 applications"
    widget.set_sync_state(None, 0)
    assert widget.caption.text() == "Not synced yet"
    widget.close()
    widget.deleteLater()
    app.processEvents()


def test_settings_saves_sheets_with_the_form(app):
    from jobhub.desktop.views.settings import SettingsView

    view = SettingsView()
    view.set_settings({"google_sheets": {
        "enabled": True,
        "web_app_url": "https://script.google.com/macros/s/abc/exec",
        "tab": "Applications",
        "target": "not-part-of-the-form",
    }})
    assert not view.dirty
    assert view.sheets.enabled.isChecked()
    view.sheets.enabled.setChecked(False)
    assert view.dirty
    emitted: list[dict] = []
    view.save_requested.connect(emitted.append)
    view._save()
    assert emitted[-1]["google_sheets"] == {
        "enabled": False,
        "web_app_url": "https://script.google.com/macros/s/abc/exec",
        "tab": "Applications",
    }
    view.close()
    view.deleteLater()
    app.processEvents()
