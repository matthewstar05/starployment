"""Local mailbox fixtures for the opt-in Greenhouse email-code flow."""
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime

import pytest

from jobhub import config
from jobhub.apply import email_verification as verification


class FakeMailbox:
    def __init__(self, messages):
        self.messages = messages
        self.commands = []

    def login(self, username, password):
        assert username == "ada@example.test"
        assert password == "app-password"
        return "OK", [b"ok"]

    def select(self, name, readonly=False):
        assert (name, readonly) == ("INBOX", True)
        return "OK", [b"ok"]

    def uid(self, command, *args):
        self.commands.append((command, args))
        if command == "SEARCH":
            if args == (None, "ALL"):
                uids = list(self.messages)
            else:
                assert args[:2] == (None, "UID") and args[2].endswith(":*")
                first_uid = int(args[2].split(":")[0])
                uids = [uid for uid in self.messages if uid >= first_uid]
                if not uids and self.messages:
                    uids = [max(self.messages)]  # IMAP '*' empty-range behavior
            return "OK", [b" ".join(str(uid).encode() for uid in uids)]
        assert command == "FETCH" and args[1] == "(BODY.PEEK[])"
        uid = int(args[0])
        return "OK", [(b"RFC822", self.messages[uid])]

    def logout(self):
        return "BYE", [b"ok"]


def email(*, sender="codes@greenhouse.io", recipient="ada@example.test",
          subject="Your Greenhouse verification code", body="Your verification code is ABCD1234",
          sent_at=None):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = format_datetime(sent_at or datetime.now(timezone.utc))
    message.set_content(body)
    return message.as_bytes()


@pytest.fixture
def inbox(monkeypatch):
    class Vault:
        def get_password(self, service, username):
            assert service == verification.SERVICE
            assert username == "ada@example.test"
            return "app-password"

    monkeypatch.setattr(verification, "_keyring", lambda: Vault())
    settings = {"imap_host": "mail.example.test", "imap_port": 993,
                "imap_username": "ada@example.test", "sender_address": "codes@greenhouse.io"}
    return verification.EmailVerificationInbox(settings, "ada@example.test")


def test_pre_submit_uid_baseline_and_read_only_fetch(monkeypatch, inbox):
    messages = {41: email(), 42: email(body="Your verification code is ZXCV9876")}
    fake = FakeMailbox(messages)
    monkeypatch.setattr(verification.imaplib, "IMAP4_SSL", lambda *args, **kwargs: fake)
    baseline, started = inbox.baseline()
    assert baseline == 42
    assert started.tzinfo == timezone.utc
    messages[43] = email(body="Your verification code is TEST4321")
    assert inbox.wait_for_code(baseline, started, timeout_sec=0) == "TEST4321"
    assert ("SEARCH", (None, "UID", "43:*")) in fake.commands
    assert ("FETCH", ("43", "(BODY.PEEK[])")) in fake.commands
    assert not any(command == "FETCH" and args[0] in ("41", "42") for command, args in fake.commands)


def test_wrong_sender_recipient_and_old_date_are_ignored(monkeypatch, inbox):
    now = datetime.now(timezone.utc)
    messages = {
        11: email(sender="attacker@example.test", body="Your verification code is ATTACK99"),
        12: email(recipient="someone@example.test", body="Your verification code is WRONG123"),
        13: email(sent_at=now - timedelta(hours=1), body="Your verification code is OLD12345"),
    }
    fake = FakeMailbox(messages)
    monkeypatch.setattr(verification.imaplib, "IMAP4_SSL", lambda *args, **kwargs: fake)
    assert inbox.wait_for_code(10, now, timeout_sec=0) is None


def test_multiple_fresh_codes_do_not_choose_one(monkeypatch, inbox):
    messages = {11: email(body="Your verification code is ABCD1234"),
                12: email(body="Your verification code is WXYZ9876")}
    monkeypatch.setattr(verification.imaplib, "IMAP4_SSL", lambda *args, **kwargs: FakeMailbox(messages))
    with pytest.raises(RuntimeError, match="Multiple fresh"):
        inbox.wait_for_code(10, datetime.now(timezone.utc), timeout_sec=0)


def test_mixed_case_code_is_preserved_and_case_variants_are_ambiguous(monkeypatch, inbox):
    messages = {11: email(body="Your verification code is aBcD1234")}
    monkeypatch.setattr(verification.imaplib, "IMAP4_SSL", lambda *args, **kwargs: FakeMailbox(messages))
    assert inbox.wait_for_code(10, datetime.now(timezone.utc), timeout_sec=0) == "aBcD1234"
    messages[12] = email(body="Your verification code is ABCD1234")
    with pytest.raises(RuntimeError, match="Multiple fresh"):
        inbox.wait_for_code(10, datetime.now(timezone.utc), timeout_sec=0)


def test_html_only_message_can_supply_visible_code(monkeypatch, inbox):
    message = EmailMessage()
    message["From"] = "codes@greenhouse.io"
    message["To"] = "ada@example.test"
    message["Subject"] = "Verify your Greenhouse application"
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message.set_content("<style>.code { display: block }</style><p>Your verification code is <strong>ZXCV1234</strong>.</p>",
                        subtype="html")
    fake = FakeMailbox({11: message.as_bytes()})
    monkeypatch.setattr(verification.imaplib, "IMAP4_SSL", lambda *args, **kwargs: fake)
    assert inbox.wait_for_code(10, datetime.now(timezone.utc), timeout_sec=0) == "ZXCV1234"


def test_only_native_keyring_backend_is_accepted(monkeypatch):
    class Vault:
        def get_keyring(self):
            return type("PlaintextKeyring", (), {"__module__": "keyrings.alt.file"})()

        def set_password(self, *args):
            raise AssertionError("must not save to plaintext backend")

    monkeypatch.setitem(__import__("sys").modules, "keyring", Vault())
    with pytest.raises(RuntimeError, match="operating system credential vault"):
        verification.store_password("ada@example.test", "app-password")


def test_delete_missing_password_is_idempotent_but_locked_vault_is_not(monkeypatch):
    class PasswordDeleteError(Exception):
        pass

    class LockedVaultError(Exception):
        pass

    class Vault:
        errors = type("Errors", (), {"PasswordDeleteError": PasswordDeleteError})

        def __init__(self, error):
            self.error = error

        def delete_password(self, service, username):
            assert (service, username) == (verification.SERVICE, "ada@example.test")
            raise self.error()

    monkeypatch.setattr(verification, "_keyring", lambda: Vault(PasswordDeleteError))
    verification.delete_password("ada@example.test")
    monkeypatch.setattr(verification, "_keyring", lambda: Vault(LockedVaultError))
    with pytest.raises(LockedVaultError):
        verification.delete_password("ada@example.test")


def test_settings_never_include_mailbox_password():
    settings = config.validate_settings(config.merge_settings(config.DEFAULT_SETTINGS, {
        "email_verification": {"enabled": True, "imap_host": "mail.example.test",
                               "imap_username": "ada@example.test", "sender_address": "codes@greenhouse.io"}}))
    assert "password" not in settings["email_verification"]
    with pytest.raises(Exception):
        config.validate_settings(config.merge_settings(settings, {"email_verification": {"password": "secret"}}))
