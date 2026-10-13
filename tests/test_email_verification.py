"""Local mailbox fixtures for the opt-in Greenhouse email-code flow."""
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime

import pytest

from jobhub import config
from jobhub.apply import email_verification as verification


class FakeMailbox:
    def __init__(self, messages, uidvalidity=None):
        self.messages = messages
        self.commands = []
        self.uidvalidity = uidvalidity

    def login(self, username, password):
        assert username == "ada@example.test"
        assert password == "app-password"
        return "OK", [b"ok"]

    def select(self, name, readonly=False):
        assert (name, readonly) == ("INBOX", True)
        return "OK", [b"ok"]

    def response(self, code):
        if code == "UIDVALIDITY" and self.uidvalidity is not None:
            return "UIDVALIDITY", [str(self.uidvalidity).encode()]
        return code, [None]

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
          html_body=None, sent_at=None):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = format_datetime(sent_at or datetime.now(timezone.utc))
    if html_body:
        message.set_content(html_body, subtype="html")
    else:
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
    fake = FakeMailbox(messages, uidvalidity=999)
    monkeypatch.setattr(verification.imaplib, "IMAP4_SSL", lambda *args, **kwargs: fake)
    baseline_result = inbox.baseline()
    assert baseline_result.max_uid == 42
    assert baseline_result.uidvalidity == 999
    # Test 2-tuple unpacking backward-compatibility
    baseline, started = baseline_result
    assert baseline == 42
    assert started.tzinfo == timezone.utc
    # Test 3-tuple unpacking
    validity, max_uid, started_at = baseline_result
    assert validity == 999 and max_uid == 42
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
    monkeypatch.setattr(verification, "keychain_protection", lambda: True)
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
    with pytest.raises(ValueError):
        config.validate_settings(config.merge_settings(settings, {"email_verification": {"password": "secret"}}))


def test_code_with_leading_zeros_and_custom_length(monkeypatch, inbox):
    messages = {11: email(body="Your one-time security code is 012345")}
    monkeypatch.setattr(verification.imaplib, "IMAP4_SSL", lambda *args, **kwargs: FakeMailbox(messages))
    token = inbox.wait_for_token(10, datetime.now(timezone.utc), length=6, timeout_sec=0)
    assert token is not None
    assert token.kind == "code"
    assert token.value == "012345"
    assert inbox.wait_for_code(10, datetime.now(timezone.utc), length=6, timeout_sec=0) == "012345"


def test_link_extraction_from_html_href_and_plain_text(monkeypatch, inbox):
    html = ('<p>Welcome to Acme!</p>'
            '<p><a href="https://boards.greenhouse.io/confirm_email?token=sec123">Confirm your application</a></p>')
    plain = 'Confirm here: https://jobs.ashbyhq.com/verify/token987 for your application.'

    # Test HTML href link
    fake_html = FakeMailbox({11: email(html_body=html, subject="Confirm your application")})
    monkeypatch.setattr(verification.imaplib, "IMAP4_SSL", lambda *args, **kwargs: fake_html)
    link = inbox.wait_for_link(10, datetime.now(timezone.utc), timeout_sec=0)
    assert link == "https://boards.greenhouse.io/confirm_email?token=sec123"

    # Test plain text link
    fake_plain = FakeMailbox({12: email(body=plain, subject="Confirm application token")})
    monkeypatch.setattr(verification.imaplib, "IMAP4_SSL", lambda *args, **kwargs: fake_plain)
    token = inbox.wait_for_token(10, datetime.now(timezone.utc), timeout_sec=0)
    assert token is not None
    assert token.kind == "link"
    assert token.value == "https://jobs.ashbyhq.com/verify/token987"


def test_link_rejection_for_untrusted_hosts_and_unsubscribe_privacy_links(monkeypatch, inbox):
    body = (
        'Thank you for applying.\n'
        'Unsubscribe: https://boards.greenhouse.io/unsubscribe?token=123\n'
        'Privacy Policy: https://boards.greenhouse.io/privacy\n'
        'Help: https://boards.greenhouse.io/help\n'
        'Social: https://linkedin.com/verify/acme\n'
        'Untrusted host: https://phishing.example.com/confirm?token=123\n'
    )
    fake = FakeMailbox({11: email(body=body, subject="Application confirmation")})
    monkeypatch.setattr(verification.imaplib, "IMAP4_SSL", lambda *args, **kwargs: fake)
    # With allowed_hosts specified, phishing.example.com and linkedin are excluded,
    # and unsubscribe/privacy/help are excluded by keywords.
    assert inbox.wait_for_link(10, datetime.now(timezone.utc), allowed_hosts=["greenhouse.io"], timeout_sec=0) is None


def test_uidvalidity_change_handling(monkeypatch, inbox):
    fake = FakeMailbox({11: email()}, uidvalidity=500)
    monkeypatch.setattr(verification.imaplib, "IMAP4_SSL", lambda *args, **kwargs: fake)
    # If baseline UIDVALIDITY was 100, but mailbox returns 500, RuntimeError must be raised.
    with pytest.raises(RuntimeError, match="UIDVALIDITY changed"):
        inbox.wait_for_token(10, datetime.now(timezone.utc), baseline_uidvalidity=100, timeout_sec=0)


def test_employer_context_disambiguation(monkeypatch, inbox):
    now = datetime.now(timezone.utc)
    # Two messages from greenhouse: one for Acme Corp, one for Beta Inc
    messages = {
        11: email(subject="Your Acme Corp verification code", body="Your code is 11112222"),
        12: email(subject="Your Beta Inc verification code", body="Your code is 33334444"),
    }
    monkeypatch.setattr(verification.imaplib, "IMAP4_SSL", lambda *args, **kwargs: FakeMailbox(messages))
    # Without employer context, multiple fresh codes conflict and raise error
    with pytest.raises(RuntimeError, match="Multiple fresh"):
        inbox.wait_for_token(10, now, timeout_sec=0)

    # Disambiguating by employer 'Acme Corp'
    token_acme = inbox.wait_for_token(10, now, employer="Acme Corp", timeout_sec=0)
    assert token_acme is not None and token_acme.value == "11112222"

    # Disambiguating by employer 'Beta Inc'
    token_beta = inbox.wait_for_token(10, now, employer="Beta Inc", timeout_sec=0)
    assert token_beta is not None and token_beta.value == "33334444"


def test_token_redaction_helper():
    assert verification.redact_token("") == ""
    assert verification.redact_token("12", "code") == "**"
    assert verification.redact_token("1234", "code") == "1**4"
    assert verification.redact_token("012345", "code") == "01**45"
    assert verification.redact_token("ABCD1234", "code") == "AB****34"

    # Link redaction
    link = "https://boards.greenhouse.io/confirm_email?token=secret123456&user=ada"
    redacted = verification.redact_token(link, "link")
    assert "secret123456" not in redacted
    assert "user=ada" not in redacted
    assert redacted == "https://boards.greenhouse.io/confirm_email"

    long_path = "https://jobs.ashbyhq.com/verify/12345678-abcd-1234-ef00-1234567890ab"
    redacted_path = verification.redact_token(long_path, "link")
    assert "12345678-abcd" not in redacted_path
    assert redacted_path == "https://jobs.ashbyhq.com/verify/[redacted]"


def test_empty_sender_auto_discovery(monkeypatch):
    class Vault:
        def get_password(self, service, username):
            return "app-password"

    monkeypatch.setattr(verification, "_keyring", lambda: Vault())
    now = datetime.now(timezone.utc)
    settings = {
        "imap_host": "imap.example.test",
        "imap_port": 993,
        "imap_username": "ada@example.test",
        "sender_address": "",
    }
    auto_inbox = verification.EmailVerificationInbox(settings, "ada@example.test")
    messages = {
        51: email(sender="no-reply@jobs.lever.co", recipient="ada@example.test",
                  body="Your Lever verification code is LEVER777"),
    }
    monkeypatch.setattr(verification.imaplib, "IMAP4_SSL", lambda *args, **kwargs: FakeMailbox(messages))
    token = auto_inbox.wait_for_token(50, now, timeout_sec=0)
    assert token is not None and token.value == "LEVER777"



def test_secrets_file_is_owner_only_and_copies_old_keychain_secrets(monkeypatch, tmp_path):
    from jobhub import config

    class Keychain:
        def __init__(self):
            self.items = {(verification.SERVICE, "ada@example.test"): "old-password"}

        def get_password(self, service, username):
            return self.items.get((service, username))

        def set_password(self, service, username, password):
            self.items[(service, username)] = password

        def delete_password(self, service, username):
            del self.items[(service, username)]

    keychain = Keychain()
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(verification, "_system_vault", lambda: keychain)
    monkeypatch.setattr(verification, "keychain_protection", lambda: False)
    assert verification.has_password("ada@example.test")
    secrets = tmp_path / "secrets.json"
    assert secrets.stat().st_mode & 0o777 == 0o600
    del keychain.items[(verification.SERVICE, "ada@example.test")]
    assert verification.has_password("ada@example.test")   # read from the file now

    verification.store_password("ada@example.test", "new-password")
    assert keychain.items == {}
    verification._FileVault().move_to_system_vault()
    assert keychain.items == {(verification.SERVICE, "ada@example.test"): "new-password"}
    assert not secrets.exists()


def test_greenhouse_wording_with_the_code_on_its_own_line_is_read():
    message = EmailMessage()
    message["Subject"] = "Security code for your application to Example Co"
    message.set_content("Copy and paste this code into the security code field on your application:\r\n\r\n"
                        " 7kQm2xWb \r\n\r\n After you enter the code, resubmit your application.\r\n"
                        "© 2026 Greenhouse · 18 West 18th Street, 11th Floor, New York, NY 10011")
    assert verification._code_from_message(message, 8) == "7kQm2xWb"
    assert verification._code_from_message(message, None) == "7kQm2xWb"
