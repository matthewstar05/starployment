"""Saved Workday sign-ins: employer, email, and status; the password only after Touch ID.

The sheet never reads the vault itself. It asks the controller (``reveal_requested``,
``copy_requested``, ``forget_requested``, ``save_requested``, ``export_requested``,
``open_requested``), which checks Touch ID before any password leaves the vault and
hands one back with ``show_password``. A shown password hides itself after 30 seconds.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLineEdit, QScrollArea, QVBoxLayout, QWidget

from ...vault import STATUS_LABELS
from ..native_glass import prepare_window
from ..widgets import ElidedLabel, GlassPanel, IconBadge, button, label, paint_backdrop

HIDDEN = "••••••••••••"
SHOW_SECONDS = 30
# What each status asks of the candidate, in this sheet.
SHEET_STATUS = {
    "active": "Signed in and working",
    "creating": "Being created by Starployment",
    "needs_verification": "Waiting for you to open Workday's verification email",
    "exists": "You already had an account here. Choose Edit and enter its password",
    "failed": "Workday refused the saved password. Choose Edit and enter the current one",
}
STATUS_TONES = {"active": "green", "creating": "accent", "needs_verification": "amber", "exists": "amber",
                "failed": "red"}


class _AccountRow(QWidget):
    def __init__(self, sheet: PasswordsSheet, account: dict):
        super().__init__()
        self.tenant = account["tenant"]
        self.account = account
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(10)
        row.addWidget(IconBadge("plug", STATUS_TONES.get(account.get("status", ""), "neutral"), 30))
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(ElidedLabel(account.get("company") or self.tenant, "rowTitle"))
        used = (account.get("last_used") or "")[:10]
        text.addWidget(ElidedLabel(" · ".join(filter(None, [account.get("email", ""),
                                                            f"Last used {used}" if used else ""])), "rowNote"))
        self.status = label(SHEET_STATUS.get(account.get("status", ""), STATUS_LABELS.get(account.get("status", ""), "")),
                            "rowNote")
        text.addWidget(self.status)
        self.secret = label(HIDDEN, "rowSecondary", wrap=False)
        text.addWidget(self.secret)
        row.addLayout(text, 1)
        self.show_button = button("Show", "secondary")
        self.show_button.clicked.connect(lambda: sheet.reveal_requested.emit(self.tenant))
        copy = button("Copy", "secondary")
        copy.clicked.connect(lambda: sheet.copy_requested.emit(self.tenant))
        self.edit = button("Edit", "quiet")
        self.edit.clicked.connect(lambda: sheet.edit_account(account))
        self.open = button("Open", "quiet", icon="external")
        self.open.setToolTip("Open this employer's Workday sign-in page in your browser")
        self.open.clicked.connect(lambda: sheet.open_requested.emit(self.tenant))
        self.open.setVisible(bool(account.get("host")))
        self.forget = button("Forget", "quiet")
        self.forget.clicked.connect(self._forget)
        for control in (self.show_button, copy, self.edit, self.open, self.forget):
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


class _SignInForm(GlassPanel):
    """Add a sign-in you already have, or change a saved one's password."""

    def __init__(self, sheet: PasswordsSheet):
        super().__init__(radius=14)
        self.sheet = sheet
        column = QVBoxLayout(self)
        column.setContentsMargins(16, 14, 16, 14)
        column.setSpacing(8)
        self.heading = label("Add a sign-in", "groupTitle")
        column.addWidget(self.heading)
        column.addWidget(label("For an employer where you already have a Workday account. Starployment signs "
                               "in with it instead of trying to create one.", "rowNote"))
        self.address = QLineEdit()
        self.address.setPlaceholderText("Workday address, e.g. https://citi.wd5.myworkdayjobs.com")
        self.company = QLineEdit()
        self.company.setPlaceholderText("Employer name (optional)")
        self.email = QLineEdit()
        self.email.setPlaceholderText("Email you sign in with")
        self.password = QLineEdit()
        self.password.setPlaceholderText("Password")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        for field in (self.address, self.company, self.email, self.password):
            field.setClearButtonEnabled(True)
            column.addWidget(field)
        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = button("Cancel", "quiet")
        cancel.clicked.connect(self.close_form)
        self.save = button("Save sign-in")
        self.save.clicked.connect(self._save)
        self.password.returnPressed.connect(self._save)
        actions.addWidget(cancel)
        actions.addWidget(self.save)
        column.addLayout(actions)

    def open_for(self, account: dict | None, default_email: str = "") -> None:
        editing = account is not None
        self.heading.setText(f"Change the password for {account.get('company') or account['tenant']}"
                             if editing else "Add a sign-in")
        self.address.setText(f"https://{account['host']}" if editing and account.get("host") else "")
        self.address.setReadOnly(editing and bool(account.get("host")))
        self.company.setText(account.get("company", "") if editing else "")
        self.email.setText(account.get("email", "") if editing else default_email)
        self.password.clear()
        self.show()
        (self.password if editing else self.address).setFocus()

    def close_form(self) -> None:
        self.password.clear()
        self.hide()

    def _save(self) -> None:
        details = {"address": self.address.text().strip(), "company": self.company.text().strip(),
                   "email": self.email.text().strip(), "password": self.password.text()}
        if not details["address"] or not details["email"] or not details["password"]:
            self.sheet.set_message("Enter the Workday address, your email, and the password.")
            return
        self.sheet.save_requested.emit(details)


class PasswordsSheet(QDialog):
    reveal_requested = Signal(str)
    copy_requested = Signal(str)
    forget_requested = Signal(str)
    open_requested = Signal(str)
    save_requested = Signal(dict)       # {address, company, email, password}
    export_requested = Signal()

    def __init__(self, parent: QWidget | None = None, default_email: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Workday sign-ins")
        prepare_window(self)
        self.setModal(True)
        self.setMinimumSize(760, 560)
        self.default_email = default_email
        column = QVBoxLayout(self)
        column.setContentsMargins(24, 22, 24, 20)
        column.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(12)
        title = QVBoxLayout()
        title.setSpacing(4)
        title.addWidget(label("Workday sign-ins", "sheetTitle"))
        title.addWidget(label("The account Starployment uses at each employer's Workday. Passwords stay on this "
                              "Mac; showing, copying, or exporting one asks for Touch ID.", "subtitle"))
        head.addLayout(title, 1)
        column.addLayout(head)
        tools = QHBoxLayout()
        tools.setSpacing(8)
        self.add_button = button("Add sign-in", "secondary", icon="plus")
        self.add_button.clicked.connect(lambda: self.edit_account(None))
        self.export_button = button("Export to Passwords…", "secondary", icon="download")
        self.export_button.setToolTip("Add these sign-ins to the macOS Passwords app, which syncs them with "
                                      "iCloud Keychain and fills them in Safari")
        self.export_button.clicked.connect(self.export_requested)
        tools.addWidget(self.add_button)
        tools.addWidget(self.export_button)
        tools.addStretch(1)
        column.addLayout(tools)
        self.form = _SignInForm(self)
        self.form.hide()
        column.addWidget(self.form)
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

    def edit_account(self, account: dict | None) -> None:
        self.form.open_for(account, self.default_email)

    def saved(self) -> None:
        """The controller stored the form's sign-in."""
        self.form.close_form()

    def set_accounts(self, accounts: list[dict]) -> None:
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._rows = {}
        self.export_button.setEnabled(bool(accounts))
        if not accounts:
            self.rows.addWidget(label("No Workday sign-ins yet. With Manage Workday sign-ins on, Starployment "
                                      "saves one the first time it applies to each employer. Add one you "
                                      "already have with Add sign-in.", "rowNote"))
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
