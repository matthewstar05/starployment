"""Connections: Luna's AI account, the mailbox, and an optional Google Sheet.

Built for someone who has a ChatGPT or Claude subscription but has never used
a terminal: choose the account, press the one button the status line offers
(Install, Sign in, or Test), and you are done. Secrets go to the system
credential vault, never to the Starployment database.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLineEdit, QSpinBox, QVBoxLayout, QWidget

from ...agents import BACKENDS
from ...email_tracker import PROVIDERS, provider_for
from ..format import relative_time
from ..widgets import FormGroup, StatusDot, ToggleSwitch, button, label

LUNA_CHOICES = (("none", "Off"), ("antigravity_cli", "Antigravity (agy)"), ("claude_cli", "Claude (Pro or Max plan)"), ("codex_cli", "ChatGPT (Plus or Pro plan)"),
                ("anthropic_api", "Anthropic API key"), ("openai_api", "OpenAI API key"))
GREENHOUSE_SENDER = "no-reply@us.greenhouse-mail.io"


class StatusLine(QWidget):
    action_clicked = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self.dot = StatusDot("neutral", 9)
        row.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(1)
        self.title = label("", "rowTitle")
        self.detail = label("", "rowNote")
        text.addWidget(self.title)
        text.addWidget(self.detail)
        row.addLayout(text, 1)
        self.action = button("", "secondary")
        self.action.hide()
        self.action.clicked.connect(lambda: self.action_clicked.emit(str(self.action.property("action") or "")))
        row.addWidget(self.action, 0, Qt.AlignmentFlag.AlignVCenter)
        self._action = ""

    def set_status(self, title: str, detail: str, tone: str, action: str = "", action_text: str = "") -> None:
        self.title.setText(title)
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.dot.set_tone(tone)
        self.action.setProperty("action", action)
        self.action.setText(action_text)
        self.action.setVisible(bool(action))
        self.action.setEnabled(True)

    def busy(self, text: str) -> None:
        self.title.setText(text)
        self.dot.set_tone("accent")
        self.action.setEnabled(False)


class LunaConnection(QWidget):
    """Choose the AI account Luna uses and connect it."""

    changed = Signal()
    status_requested = Signal(str)
    test_requested = Signal(dict)
    login_requested = Signal(str)
    install_requested = Signal(str)
    save_key_requested = Signal(str, str)

    def __init__(self, parent: QWidget | None = None, *, title: str = "Luna, your AI assistant", compact: bool = False):
        super().__init__(parent)
        self._compact = compact
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        self.group = FormGroup(title, "Luna drafts answers to custom questions with your Claude or ChatGPT plan. "
                                      "You review every draft." if compact else
                               "Luna drafts answers to employers' custom questions and can pick which experience "
                               "goes on each resume. She uses an AI account you already have. You review "
                               "everything she writes before it is sent.")
        self.backend = QComboBox()
        for key, text in LUNA_CHOICES:
            self.backend.addItem(text, key)
        self.backend.setAccessibleName("Luna account")
        self.backend.currentIndexChanged.connect(self._backend_changed)
        self.status = StatusLine()
        self.status.action_clicked.connect(self._action)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("Paste your API key")
        self.save_key = button("Save key", "secondary")
        self.save_key.clicked.connect(lambda: self.key.text().strip() and self.save_key_requested.emit(
            str(self.backend.currentData()), self.key.text().strip()))
        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.addWidget(self.key, 1)
        key_layout.addWidget(self.save_key)
        self.model = QComboBox()
        self.model.setAccessibleName("Model")
        self.model.currentIndexChanged.connect(self.changed)
        self.draft_answers = ToggleSwitch()
        self.draft_answers.toggled.connect(self.changed)
        self.resume_select = ToggleSwitch()
        self.resume_select.toggled.connect(self.changed)
        self.remember = ToggleSwitch()
        self.remember.toggled.connect(self.changed)
        self.autofill = ToggleSwitch()
        self.autofill.toggled.connect(self.changed)
        self.group.add_row("Account", self.backend)
        self.group.add_widget(self.status)
        self.key_row = self.group.add_row("API key", key_row, "Stored on this computer, never in the database.")
        self.model_row = self.group.add_row("Model", self.model, "Choose a model, or keep the default.")
        options = [self.group.add_row("Draft custom answers", self.draft_answers, "When preparing applications."),
                   self.group.add_row("Autofill prepared applications", self.autofill,
                                      "AI Autofill answers every question so each is ready to send. "
                                      "Assumptions are labeled."),
                   self.group.add_row("Choose resume content", self.resume_select,
                                      "Luna picks the most relevant entries for each one-page resume."),
                   self.group.add_row("Remember approved answers", self.remember,
                                      "Short, general answers you approve join your question bank.")]
        for row in options:
            row.setVisible(not compact)   # the wizard keeps defaults; Settings shows every option
        column.addWidget(self.group)
        self._backend_changed()

    def _backend_changed(self, *_args) -> None:
        backend = str(self.backend.currentData() or "none")
        spec = BACKENDS.get(backend)
        self.key_row.setVisible(backend in ("anthropic_api", "openai_api"))
        self.model_row.setVisible(backend != "none" and not self._compact)

        target = getattr(self, "_pending_model", None)
        if target is None:
            target = str(self.model.currentData() or "")
        self._pending_model = None

        self.model.blockSignals(True)
        self.model.clear()
        if spec:
            default_label = f"Default ({spec.default_model})" if spec.default_model else "Default (auto)"
            self.model.addItem(default_label, "")
            models = getattr(spec, "models", ()) or ()
            for model_id, label_text in models:
                self.model.addItem(label_text, model_id)
            if target:
                idx = self.model.findData(target)
                if idx >= 0:
                    self.model.setCurrentIndex(idx)
                else:
                    self.model.addItem(f"Custom ({target})", target)
                    self.model.setCurrentIndex(self.model.count() - 1)
            else:
                self.model.setCurrentIndex(0)
        self.model.blockSignals(False)

        for toggle in (self.draft_answers, self.resume_select, self.remember, self.autofill):
            toggle.setEnabled(backend != "none")
        if backend == "none":
            self.status.set_status("Luna is off", "Applications still prepare; open questions wait for your answers.",
                                   "neutral")
        else:
            self.status.busy("Checking connection…")
            self.status_requested.emit(backend)
        self.changed.emit()

    def set_status(self, backend: str, ready: bool, title: str, detail: str, action: str) -> None:
        if backend != self.backend.currentData():
            return
        text = {"install": "Get the app", "login": "Sign in", "key": "", "test": "Test"}.get(action, "")
        self.status.set_status(title, detail, "green" if ready else "amber", action if text else "", text)

    def set_test_result(self, ok: bool, message: str) -> None:
        self.status.set_status("Luna is ready" if ok else "Connection problem", message,
                               "green" if ok else "red", "test", "Test again")

    def _action(self, action: str) -> None:
        backend = str(self.backend.currentData() or "none")
        if action == "install":
            self.install_requested.emit(backend)
        elif action == "login":
            self.login_requested.emit(backend)
        elif action == "test":
            self.status.busy("Asking Luna to reply…")
            self.test_requested.emit(self.values())

    def set_values(self, settings: dict) -> None:
        agent = settings.get("agent") or {}
        self.backend.blockSignals(True)
        self.backend.setCurrentIndex(max(0, self.backend.findData(agent.get("backend", "none"))))
        self.backend.blockSignals(False)
        self._pending_model = str(agent.get("model") or "")
        luna = settings.get("luna") or {}
        self.draft_answers.setChecked(bool(luna.get("draft_answers", True)))
        self.remember.setChecked(bool(luna.get("remember_approved", True)))
        self.autofill.setChecked(bool(luna.get("autofill_on_prepare", True)))
        self.resume_select.setChecked(bool((settings.get("resume_builder") or {}).get("luna_select", False)))
        self._backend_changed()

    def values(self) -> dict:
        return {"agent": {"backend": str(self.backend.currentData() or "none"), "model": str(self.model.currentData() or "").strip()},
                "luna": {"draft_answers": self.draft_answers.isChecked(), "remember_approved": self.remember.isChecked(),
                         "autofill_on_prepare": self.autofill.isChecked()},
                "resume_builder": {"luna_select": self.resume_select.isChecked()}}


class EmailConnection(QWidget):
    """A mailbox for reply tracking and Greenhouse email codes, read-only unless rejections are archived."""

    changed = Signal()
    test_requested = Signal(dict)
    help_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None, *, compact: bool = False):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        self.group = FormGroup("Email", "Read-only. Moves applications to Assessment, Interview, Offer, or Rejected "
                                        "from replies; keeps only each message's sender, subject, and date." if compact else
                               "Starployment reads your inbox (read-only) to move applications to Assessment, "
                               "Interview, Offer, or Rejected, and optionally to complete email verification codes and links. "
                               "It keeps only each message's sender, subject, and date, and changes nothing in your mailbox "
                               "unless you turn on Archive rejections.")
        self.provider = QComboBox()
        for key, (title, *_rest) in PROVIDERS.items():
            self.provider.addItem(title, key)
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self.address = QLineEdit()
        self.address.setPlaceholderText("you@gmail.com")
        self.address.setAccessibleName("Mailbox address")
        self.address.textChanged.connect(self.changed)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Leave blank to keep the saved app password")
        self.password.setAccessibleName("App password")
        self.password.textChanged.connect(self.changed)
        self.help = button("How to get an app password", "quiet", icon="external")
        self.help.clicked.connect(lambda: self.help_requested.emit(
            PROVIDERS.get(str(self.provider.currentData()), ("", "", 0, ""))[3]))
        self.host = QLineEdit()
        self.host.setPlaceholderText("imap.example.com")
        self.host.textChanged.connect(self.changed)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(993)
        self.port.setFixedWidth(96)
        self.port.valueChanged.connect(self.changed)
        self.status = StatusLine()
        self.status.set_status("Not tested", "Save, then test to confirm Starployment can read your inbox.", "neutral",
                               "test", "Test")
        self.status.action_clicked.connect(lambda _a: (self.status.busy("Connecting…"), self.test_requested.emit(self.values())))
        self.track = ToggleSwitch()
        self.track.toggled.connect(self._track_changed)
        self.archive = ToggleSwitch()
        self.archive.toggled.connect(self.changed)
        self.luna_mail = ToggleSwitch()
        self.luna_mail.setAccessibleName("Ask Luna about unclear emails")
        self.luna_mail.toggled.connect(self.changed)
        self.codes = ToggleSwitch()
        self.codes.toggled.connect(self._codes_changed)
        self.sender = QLineEdit()
        self.sender.setPlaceholderText("Leave blank to auto-detect employer or ATS")
        self.sender.textChanged.connect(self.changed)
        self.forget = ToggleSwitch()
        self.forget.toggled.connect(self.changed)
        self.group.add_row("Provider", self.provider)
        self.group.add_row("Email address", self.address)
        self.group.add_row("App password", self.password, "A separate password your provider creates for apps.")
        self.help_row = self.group.add_widget(self.help)
        self.host_row = self.group.add_row("IMAP server", self.host)
        self.port_row = self.group.add_row("Port (TLS)", self.port)
        self.group.add_widget(self.status)
        self.group.add_row("Track replies", self.track, "Update Tracker stages from recruiting emails.")
        self.luna_mail_row = self.group.add_row(
            "Ask Luna about unclear emails", self.luna_mail,
            "Luna reads only the emails the rules can't settle, many per request, and never the same email twice.")
        self.archive_row = self.group.add_row(
            "Archive rejections", self.archive,
            "Takes a rejection out of your Inbox once its application is marked Rejected. Nothing is deleted; "
            "Gmail labels it Starployment/Rejected.")
        codes_row = self.group.add_row("Finish email verification", self.codes,
                                       "Completes verification codes and links from employer emails.")
        self.sender_row = self.group.add_row("Trusted sender filter", self.sender,
                                             "Optional. Leave blank to match employer domains automatically.")
        forget_row = self.group.add_row("Forget saved password", self.forget)
        self._compact = compact
        codes_row.setVisible(not compact)
        forget_row.setVisible(not compact)
        column.addWidget(self.group)
        self._provider_changed()
        self._codes_changed()
        self._track_changed()

    def _track_changed(self, *_args) -> None:
        self.archive_row.setVisible(self.track.isChecked() and not getattr(self, "_compact", False))
        self.luna_mail_row.setVisible(self.track.isChecked() and not getattr(self, "_compact", False))
        self.changed.emit()

    def _provider_changed(self, *_args) -> None:
        key = str(self.provider.currentData() or "other")
        _, host, port, help_url = PROVIDERS.get(key, PROVIDERS["other"])
        custom = key == "other"
        self.host_row.setVisible(custom)
        self.port_row.setVisible(custom)
        self.help_row.setVisible(bool(help_url))
        if not custom:
            self.host.setText(host)
            self.port.setValue(port)
        self.address.setPlaceholderText({"gmail": "you@gmail.com", "outlook": "you@outlook.com",
                                         "icloud": "you@icloud.com", "yahoo": "you@yahoo.com"}.get(key, "you@example.com"))
        self.changed.emit()

    def _codes_changed(self, *_args) -> None:
        self.sender_row.setVisible(self.codes.isChecked() and not getattr(self, "_compact", False))
        self.changed.emit()

    def set_values(self, settings: dict) -> None:
        verification = settings.get("email_verification") or {}
        tracking = settings.get("email_tracking") or {}
        self.provider.blockSignals(True)
        self.provider.setCurrentIndex(max(0, self.provider.findData(provider_for(verification.get("imap_host", "")))))
        self.provider.blockSignals(False)
        self._provider_changed()
        if verification.get("imap_host"):
            self.host.setText(str(verification.get("imap_host")))
        self.port.setValue(int(verification.get("imap_port") or 993))
        self.address.setText(str(verification.get("imap_username") or ""))
        self.sender.setText(str(verification.get("sender_address") or ""))
        self.codes.setChecked(bool(verification.get("enabled")))
        self.track.setChecked(bool(tracking.get("enabled")))
        self.archive.setChecked(bool(tracking.get("archive_rejections")))
        self.luna_mail.setChecked(bool(tracking.get("use_luna", True)))
        self._track_changed()
        self.password.clear()
        self.forget.setChecked(False)
        self._codes_changed()

    def set_test_result(self, ok: bool, message: str) -> None:
        self.status.set_status("Inbox connected" if ok else "Could not connect", message,
                               "green" if ok else "red", "test", "Test again")

    def values(self) -> dict:
        patch = {
            "email_verification": {
                "enabled": self.codes.isChecked(), "imap_host": self.host.text().strip(),
                "imap_port": self.port.value(), "imap_username": self.address.text().strip(),
                "sender_address": self.sender.text().strip() if self.codes.isChecked() else "",
            },
            "email_tracking": {"enabled": self.track.isChecked(),
                               "archive_rejections": self.track.isChecked() and self.archive.isChecked(),
                               "use_luna": self.luna_mail.isChecked()},
        }
        if self.password.text():
            patch["_imap_password"] = self.password.text()
        if self.forget.isChecked():
            patch["_forget_imap_password"] = True
        return patch

    def clear_secrets(self) -> None:
        self.password.clear()
        self.forget.setChecked(False)


def _actions(*buttons: QWidget) -> QWidget:
    """Left-aligned capsules. A bare button in a form row stretches edge to edge."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    for item in buttons:
        layout.addWidget(item)
    layout.addStretch(1)
    return row


class SheetsConnection(QWidget):
    """Copy confirmed applications into a Google Sheet the candidate owns.

    The sheet's own Apps Script does the writing. This panel only collects the
    web app URL and asks the controller to copy, test, or sync.
    """

    changed = Signal()
    copy_script_requested = Signal()
    test_requested = Signal(dict)
    sync_requested = Signal(bool)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        self.group = FormGroup(
            "Google Sheets",
            "Optional, and off until you turn it on. Confirmed applications are copied into a Google Sheet "
            "you own. The script runs as you, so Starployment never signs in to Google.")
        self.enabled = ToggleSwitch()
        self.enabled.setAccessibleName("Copy confirmed applications to a Google Sheet")
        self.enabled.toggled.connect(self.changed)
        self.steps = label(
            "1. Copy the setup script.\n"
            "2. In your sheet: Extensions → Apps Script, paste, then Deploy → New deployment → Web app. "
            "Execute as Me, Who has access Anyone. Authorize it and copy the /exec URL.\n"
            "3. Paste it below, press Test, then Save.",
            "callout")
        self.copy_script = button("Copy setup script", "secondary")
        self.copy_script.clicked.connect(self.copy_script_requested)
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://script.google.com/macros/s/.../exec")
        self.url.setAccessibleName("Web app URL")
        self.url.textChanged.connect(self.changed)
        self.tab = QLineEdit("Applications")
        self.tab.setPlaceholderText("Applications")
        self.tab.setAccessibleName("Tab name")
        self.tab.textChanged.connect(self.changed)
        self.status = StatusLine()
        self.status.set_status("Not tested", "Paste the web app URL, then test.", "neutral", "test", "Test")
        self.status.action_clicked.connect(self._test)
        self.sync_now = button("Sync now", "secondary")
        self.sync_now.clicked.connect(lambda: self.sync_requested.emit(False))
        self.resend = button("Resend everything", "secondary")
        self.resend.clicked.connect(lambda: self.sync_requested.emit(True))
        self.caption = label("Not synced yet", "caption")
        self.group.add_row("Copy confirmed applications to a Google Sheet", self.enabled,
                           "Only applications an employer has confirmed. Rows already in the sheet are updated, never removed.")
        self.group.add_widget(self.steps)
        self.group.add_widget(_actions(self.copy_script))
        self.group.add_row("Web app URL", self.url)
        self.group.add_row("Tab name", self.tab, "Created in the sheet if it is missing.")
        self.group.add_widget(self.status)
        self.group.add_widget(_actions(self.sync_now, self.resend))
        self.group.add_widget(self.caption)
        column.addWidget(self.group)

    def _test(self, _action: str) -> None:
        self.status.busy("Reaching your sheet…")
        self.test_requested.emit(self.values())

    def set_values(self, settings: dict) -> None:
        sheets = settings.get("google_sheets") or {}
        self.enabled.blockSignals(True)
        self.url.blockSignals(True)
        self.tab.blockSignals(True)
        self.enabled.setChecked(bool(sheets.get("enabled", False)))
        self.url.setText(str(sheets.get("web_app_url") or ""))
        self.tab.setText(str(sheets.get("tab") or "").strip() or "Applications")
        self.enabled.blockSignals(False)
        self.url.blockSignals(False)
        self.tab.blockSignals(False)

    def values(self) -> dict:
        return {"google_sheets": {
            "enabled": self.enabled.isChecked(),
            "web_app_url": self.url.text().strip(),
            "tab": self.tab.text().strip() or "Applications",
        }}

    def set_test_result(self, ok: bool, message: str) -> None:
        self.status.set_status("Sheet connected" if ok else "Could not reach the sheet", message,
                               "green" if ok else "red", "test", "Test again")

    def set_sync_state(self, last_synced: str | None, count: int) -> None:
        if not last_synced:
            self.caption.setText("Not synced yet")
            return
        self.caption.setText(f"Last synced {relative_time(last_synced)} · {int(count)} applications")
