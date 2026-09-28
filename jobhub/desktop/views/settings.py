"""Settings page: appearance (applies instantly), automation, resume, email verification."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QLineEdit, QScrollArea, QSpinBox, QVBoxLayout, QWidget

from PySide6.QtWidgets import QAbstractSpinBox

from ..widgets import DirtyTracker, FormGroup, PageHeader, SegmentedControl, Stepper, ToggleSwitch, button

DEFAULT_SUBTITLE = "Automation, resume, appearance, and email verification."


class SettingsView(QWidget):
    save_requested = Signal(dict)
    scan_requested = Signal()
    appearance_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.tracker = DirtyTracker(self)
        column = QVBoxLayout(self)
        column.setContentsMargins(30, 26, 30, 0)
        column.setSpacing(14)
        self.header = PageHeader("Settings", DEFAULT_SUBTITLE)
        self.save_button = button("Save settings")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save)
        self.header.add_action(self.save_button)
        column.addWidget(self.header)
        self.tracker.changed.connect(self._dirty_changed)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        column.addWidget(scroll, 1)
        page = QWidget()
        page.setObjectName("page")
        scroll.setWidget(page)
        groups = QVBoxLayout(page)
        groups.setContentsMargins(0, 2, 6, 26)
        groups.setSpacing(22)

        appearance = FormGroup("Appearance")
        self.appearance = SegmentedControl()
        for text, value in (("Automatic", "System"), ("Light", "Light"), ("Dark", "Dark")):
            self.appearance.addItem(text, value)
        self.appearance.activated.connect(lambda _index: self.appearance_changed.emit(str(self.appearance.currentData())))
        appearance.add_row("Theme", self.appearance, "Automatic follows your system.")
        groups.addWidget(appearance)

        automation = FormGroup("Automation", "JobHub scans while the app is open and the computer is awake.")
        self.auto_apply = ToggleSwitch()
        self.auto_apply.setAccessibleName("Automatic applications")
        self.dry_run = ToggleSwitch()
        self.dry_run.setAccessibleName("Practice mode")
        self.interval = QSpinBox()
        self.interval.setRange(5, 10080)
        self.interval.setSuffix(" min")
        self.max_apply = QSpinBox()
        self.max_apply.setRange(1, 25)
        self.min_score = QSpinBox()
        self.min_score.setRange(0, 100)
        automation.add_row("Automatic applications", self.auto_apply, "Apply to eligible matches after each scan.")
        automation.add_row("Practice mode", self.dry_run, "Fill forms but never click Submit.")
        self.interval.setSingleStep(5)
        automation.add_row("Scan every", Stepper(self.interval, width=104))
        automation.add_row("Applications per run", Stepper(self.max_apply), "At most one per employer each run.")
        automation.add_row("Minimum match score", Stepper(self.min_score), "Only apply at or above this score.")
        groups.addWidget(automation)

        resume = FormGroup("Resume")
        self.strategy = SegmentedControl()
        self.strategy.addItem("Tailored", "tailored")
        self.strategy.addItem("Original", "original")
        resume.add_row("Resume to send", self.strategy, "Tailored reorders your own facts per role.")
        groups.addWidget(resume)

        email = FormGroup(
            "Email verification",
            "Optional, for Greenhouse email codes. JobHub reads only fresh messages from the trusted "
            "sender. The app password stays in your system credential vault.",
        )
        self.verification_enabled = ToggleSwitch()
        self.verification_enabled.setAccessibleName("Finish Greenhouse email challenges automatically")
        self.imap_host = QLineEdit()
        self.imap_host.setPlaceholderText("imap.example.com")
        self.imap_port = QSpinBox()
        self.imap_port.setRange(1, 65535)
        self.imap_port.setValue(993)
        self.imap_port.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.imap_port.setProperty("stepper", True)
        self.imap_port.setFixedWidth(96)
        self.imap_username = QLineEdit()
        self.imap_username.setPlaceholderText("Mailbox sign-in email")
        self.imap_sender = QLineEdit()
        self.imap_sender.setPlaceholderText("Exact sender of verification emails")
        self.imap_password = QLineEdit()
        self.imap_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.imap_password.setPlaceholderText("Leave blank to keep the saved password")
        self.forget_imap_password = ToggleSwitch()
        self.forget_imap_password.setAccessibleName("Remove the saved password from this device")
        email.add_row("Finish email challenges", self.verification_enabled)
        email.add_row("IMAP server", self.imap_host)
        email.add_row("Port (TLS)", self.imap_port)
        email.add_row("Mailbox", self.imap_username)
        email.add_row("Trusted sender", self.imap_sender)
        email.add_row("App password", self.imap_password)
        email.add_row("Forget saved password", self.forget_imap_password)
        groups.addWidget(email)
        groups.addStretch(1)

        self.tracker.watch(
            self.auto_apply, self.dry_run, self.interval, self.max_apply, self.min_score, self.strategy,
            self.verification_enabled, self.imap_host, self.imap_port, self.imap_username, self.imap_sender,
            self.imap_password, self.forget_imap_password,
        )

    @property
    def dirty(self) -> bool:
        return self.tracker.dirty

    def _dirty_changed(self, dirty: bool) -> None:
        self.save_button.setEnabled(dirty)
        self.header.set_subtitle("You have unsaved changes." if dirty else DEFAULT_SUBTITLE)

    def set_settings(self, s: dict) -> None:
        with self.tracker.suspended():
            appearance = str(s.get("appearance", "System")).title()
            self.appearance.setCurrentIndex(max(0, self.appearance.findData(appearance)))
            self.auto_apply.setChecked(bool(s.get("auto_apply", False)))
            self.dry_run.setChecked(bool(s.get("dry_run", False)))
            self.interval.setValue(int(s.get("scan_interval_min", 60)))
            self.max_apply.setValue(int(s.get("max_apply_per_run", 3)))
            self.min_score.setValue(int(s.get("min_score_to_apply", 60)))
            self.strategy.setCurrentIndex(max(0, self.strategy.findData(str(s.get("resume_strategy", "tailored")))))
            verification = s.get("email_verification", {})
            self.verification_enabled.setChecked(bool(verification.get("enabled", False)))
            self.imap_host.setText(str(verification.get("imap_host", "")))
            self.imap_port.setValue(int(verification.get("imap_port", 993)))
            self.imap_username.setText(str(verification.get("imap_username", "")))
            self.imap_sender.setText(str(verification.get("sender_address", "")))
            self.imap_password.clear()
            self.forget_imap_password.setChecked(False)
        self.tracker.reset()

    def _save(self) -> None:
        patch = {
            "appearance": self.appearance.currentData(),
            "auto_apply": self.auto_apply.isChecked(),
            "dry_run": self.dry_run.isChecked(),
            "scan_interval_min": self.interval.value(),
            "max_apply_per_run": self.max_apply.value(),
            "min_score_to_apply": float(self.min_score.value()),
            "resume_strategy": self.strategy.currentData(),
            "email_verification": {
                "enabled": self.verification_enabled.isChecked(),
                "imap_host": self.imap_host.text().strip(),
                "imap_port": self.imap_port.value(),
                "imap_username": self.imap_username.text().strip(),
                "sender_address": self.imap_sender.text().strip(),
            },
        }
        if self.imap_password.text():
            patch["_imap_password"] = self.imap_password.text()
        if self.forget_imap_password.isChecked():
            patch["_forget_imap_password"] = True
        self.save_requested.emit(patch)
        with self.tracker.suspended():
            self.imap_password.clear()
            self.forget_imap_password.setChecked(False)
