"""Saved Workday sign-ins: employer, email, and status; the password only after Touch ID.

The sheet never reads the vault itself. It asks the controller (``reveal_requested``,
``copy_requested``, ``forget_requested``), which checks Touch ID first and hands a
password back with ``show_password``. A shown password hides itself after 30 seconds.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QDialog, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from ...vault import STATUS_LABELS
from ..native_glass import prepare_window
from ..widgets import ElidedLabel, GlassPanel, button, label, paint_backdrop

HIDDEN = "••••••••••••"
SHOW_SECONDS = 30


class _AccountRow(QWidget):
    def __init__(self, sheet: PasswordsSheet, account: dict):
        super().__init__()
        self.tenant = account["tenant"]
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(10)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(ElidedLabel(account.get("company") or self.tenant, "rowTitle"))
        status = STATUS_LABELS.get(account.get("status", ""), "")
        used = (account.get("last_used") or "")[:10]
        note = " · ".join(filter(None, [account.get("email", ""), status, f"Last used {used}" if used else ""]))
        text.addWidget(ElidedLabel(note, "rowNote"))
        self.secret = label(HIDDEN, "rowSecondary", wrap=False)
        text.addWidget(self.secret)
        row.addLayout(text, 1)
        self.show_button = button("Show", "secondary")
        self.show_button.clicked.connect(lambda: sheet.reveal_requested.emit(self.tenant))
        copy = button("Copy", "secondary")
        copy.clicked.connect(lambda: sheet.copy_requested.emit(self.tenant))
        self.forget = button("Forget", "quiet")
        self.forget.clicked.connect(self._forget)
        for control in (self.show_button, copy, self.forget):
            row.addWidget(control)
        self._confirming = False
        self.sheet = sheet

    def _forget(self) -> None:
        if not self._confirming:
            self._confirming = True
            self.forget.setText("Forget it?")
            QTimer.singleShot(4000, self._reset_forget)
            return
        self.sheet.forget_requested.emit(self.tenant)

    def _reset_forget(self) -> None:
        self._confirming = False
        self.forget.setText("Forget")

    def show_password(self, password: str) -> None:
        self.secret.setText(password)
        self.show_button.setText("Hide")
        self.show_button.clicked.disconnect()
        self.show_button.clicked.connect(self.hide_password)
        QTimer.singleShot(SHOW_SECONDS * 1000, self.hide_password)

    def hide_password(self) -> None:
        self.secret.setText(HIDDEN)
        self.show_button.setText("Show")
        try:
            self.show_button.clicked.disconnect()
        except (RuntimeError, TypeError):
            pass
        self.show_button.clicked.connect(lambda: self.sheet.reveal_requested.emit(self.tenant))


class PasswordsSheet(QDialog):
    reveal_requested = Signal(str)
    copy_requested = Signal(str)
    forget_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Workday sign-ins")
        prepare_window(self)
        self.setModal(True)
        self.setMinimumSize(560, 420)
        column = QVBoxLayout(self)
        column.setContentsMargins(24, 22, 24, 20)
        column.setSpacing(10)
        column.addWidget(label("Workday sign-ins", "sheetTitle"))
        column.addWidget(label("Accounts Starployment created or saved for each employer's Workday. They stay on "
                               "this Mac. Showing or copying a password asks for Touch ID or your Mac's password.",
                               "subtitle"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.panel = GlassPanel()
        self.rows = QVBoxLayout(self.panel)
        self.rows.setContentsMargins(0, 4, 0, 4)
        self.rows.setSpacing(0)
        scroll.setWidget(self.panel)
        column.addWidget(scroll, 1)
        self.message = label("", "caption")
        column.addWidget(self.message)
        actions = QHBoxLayout()
        actions.addStretch(1)
        done = button("Done")
        done.clicked.connect(self.accept)
        actions.addWidget(done)
        column.addLayout(actions)
        self._rows: dict[str, _AccountRow] = {}

    def set_accounts(self, accounts: list[dict]) -> None:
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._rows = {}
        if not accounts:
            self.rows.addWidget(label("No Workday sign-ins yet. Turn on Manage Workday sign-ins, and Starployment "
                                      "saves one the first time it applies to each employer.", "rowNote"))
        for account in accounts:
            row = _AccountRow(self, account)
            self._rows[row.tenant] = row
            self.rows.addWidget(row)
        self.rows.addStretch(1)

    def show_password(self, tenant: str, password: str) -> None:
        row = self._rows.get(tenant)
        if row is not None:
            row.show_password(password)

    def set_message(self, text: str) -> None:
        self.message.setText(text)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        paint_backdrop(painter, self)
        painter.end()
