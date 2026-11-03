from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_settings_view_emits_schema_compatible_appearance():
    from PySide6.QtWidgets import QApplication
    from jobhub.config import DEFAULT_SETTINGS, merge_settings, validate_settings
    from jobhub.desktop.ui import SettingsView

    app = QApplication.instance() or QApplication([])
    view = SettingsView()
    emitted = []
    view.save_requested.connect(emitted.append)

    for appearance in ("System", "Light", "Dark"):
        view.set_settings({"appearance": appearance})
        view._save()
        assert emitted[-1]["appearance"] == appearance
        validate_settings(merge_settings(DEFAULT_SETTINGS, emitted[-1]))
    view.close()
    view.deleteLater()


def test_mailbox_password_is_transient_in_settings_view():
    from PySide6.QtWidgets import QApplication
    from jobhub.config import DEFAULT_SETTINGS, merge_settings, validate_settings
    from jobhub.desktop.ui import SettingsView

    app = QApplication.instance() or QApplication([])
    view = SettingsView()
    emitted = []
    view.save_requested.connect(emitted.append)
    view.imap_host.setText("imap.example.test")
    view.imap_username.setText("candidate@example.test")
    view.imap_sender.setText("codes@example.test")
    view.imap_password.setText("local-test-secret")
    view.verification_enabled.setChecked(True)
    view._save()
    patch = dict(emitted[-1])
    assert patch.pop("_imap_password") == "local-test-secret"
    assert view.imap_password.text() == ""
    settings = validate_settings(merge_settings(DEFAULT_SETTINGS, patch))
    assert "password" not in settings["email_verification"]
    view.close()
    view.deleteLater()


def test_controller_saves_mailbox_secret_only_to_vault(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from jobhub import config, db, scheduler
    from jobhub.apply import email_verification
    from jobhub.desktop.controller import DesktopController
    from jobhub.desktop.ui import JobHubWindow

    db.close()
    for name, value in {
        "DATA": tmp_path, "DB_PATH": tmp_path / "test.db", "RESUMES": tmp_path / "resumes",
        "TAILORED": tmp_path / "tailored", "SCREENSHOTS": tmp_path / "screenshots",
    }.items():
        monkeypatch.setattr(config, name, value)
    db.init()
    stored = []
    monkeypatch.setattr(email_verification, "store_password", lambda user, password: stored.append((user, password)))
    monkeypatch.setattr(email_verification, "has_password", lambda user: False)
    monkeypatch.setattr(scheduler, "rescore", lambda: None)

    app = QApplication.instance() or QApplication([])
    window = JobHubWindow()
    controller = DesktopController(window, scheduler_enabled=False, show_setup_on_start=False)
    controller.save_settings({
        "email_verification": {
            "enabled": True,
            "imap_host": "imap.example.test",
            "imap_port": 993,
            "imap_username": "candidate@example.test",
            "sender_address": "codes@example.test",
        },
        "_imap_password": "local-test-secret",
    })
    assert stored == [("candidate@example.test", "local-test-secret")]
    assert db.get_settings()["email_verification"]["enabled"] is True
    assert "local-test-secret" not in (tmp_path / "test.db").read_bytes().decode("latin1")
    controller.stop()
    window.close()
    window.deleteLater()
    db.close()
    scheduler.STATE.stop = False


def test_luna_connection_model_dropdown_for_all_providers():
    from PySide6.QtWidgets import QApplication
    from jobhub.agents import BACKENDS
    from jobhub.desktop.views.connections import LunaConnection

    app = QApplication.instance() or QApplication([])
    conn = LunaConnection()

    for backend, spec in BACKENDS.items():
        # Verify default option and spec.models population
        conn.set_values({"agent": {"backend": backend, "model": ""}})
        assert conn.model.count() == 1 + len(spec.models)
        assert conn.model.itemData(0) == ""
        for i, (model_id, _label) in enumerate(spec.models, start=1):
            assert conn.model.itemData(i) == model_id

        # Verify setting a specific model from spec.models
        if spec.models:
            chosen_model = spec.models[0][0]
            conn.set_values({"agent": {"backend": backend, "model": chosen_model}})
            assert conn.model.currentData() == chosen_model
            assert conn.values()["agent"]["model"] == chosen_model

        # Verify setting a custom model not in spec.models
        custom_model = "my-custom-model-9000"
        conn.set_values({"agent": {"backend": backend, "model": custom_model}})
        assert conn.model.currentData() == custom_model
        assert conn.values()["agent"]["model"] == custom_model

    conn.close()
    conn.deleteLater()



def test_jarvis_modes_round_trip_and_mark_settings_dirty(app):
    from jobhub.config import DEFAULT_SETTINGS, merge_settings, validate_settings
    from jobhub.desktop.views.settings import SettingsView
    view = SettingsView()
    emitted = []
    view.save_requested.connect(emitted.append)
    for mode in ("off", "new", "all", "all_plus"):
        view.set_settings(merge_settings(DEFAULT_SETTINGS, {"jarvis_mode": mode}))
        assert view.jarvis_mode.currentData() == mode
        view._save()
        assert validate_settings(merge_settings(DEFAULT_SETTINGS, emitted[-1]))["jarvis_mode"] == mode
    view.jarvis_mode.setCurrentIndex(0)
    assert view.dirty
