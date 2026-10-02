"""Opt-in, read-only IMAP lookup for a fresh Greenhouse application code.

The mailbox password lives only in the operating system credential vault. No
message body or code is persisted, logged, or sent to an LLM. A code is usable
only when it arrived after the application began and came from the exact sender
the candidate configured for this mailbox.
"""
from __future__ import annotations

import imaplib
import re
import ssl
import time
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

# Kept from the JobHub name so passwords saved before the rename keep working.
SERVICE = "JobHub IMAP verification"
_NATIVE_BACKENDS = ("keyring.backends.macOS", "keyring.backends.Windows", "keyring.backends.SecretService")


def _keyring():
    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError("The system credential vault is unavailable") from exc
    backend = keyring.get_keyring()
    if type(backend).__module__ not in _NATIVE_BACKENDS:
        raise RuntimeError("A supported operating system credential vault is unavailable")
    return keyring


def store_password(username: str, password: str) -> None:
    """Save an app-specific IMAP password in the native OS vault, never SQLite."""
    if not username.strip() or not password:
        raise ValueError("A mailbox username and password are required")
    _keyring().set_password(SERVICE, username.strip().casefold(), password)


def delete_password(username: str) -> None:
    if username.strip():
        vault = _keyring()
        try:
            vault.delete_password(SERVICE, username.strip().casefold())
        except vault.errors.PasswordDeleteError:
            pass  # Clearing an already-empty credential slot is idempotent.


def has_password(username: str) -> bool:
    return bool(username.strip() and _keyring().get_password(SERVICE, username.strip().casefold()))


def _addresses(message, names: tuple[str, ...]) -> set[str]:
    headers = [str(message[name]) for name in names if message.get(name)]
    return {address.casefold() for _, address in getaddresses(headers) if address}


def _plain_text(message) -> str:
    parts = message.walk() if message.is_multipart() else [message]
    text = []
    html = []
    for part in parts:
        if part.get_content_type() not in ("text/plain", "text/html") or part.get_content_disposition() == "attachment":
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError, ValueError):
            continue
        if part.get_content_type() == "text/plain":
            text.append(content)
        else:
            html.append(content)
    if text:
        return "\n".join(text)[:20000]

    class VisibleText(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.hidden = 0
            self.chunks = []

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.hidden += 1

        def handle_endtag(self, tag):
            if tag in ("script", "style") and self.hidden:
                self.hidden -= 1

        def handle_data(self, data):
            if not self.hidden:
                self.chunks.append(data)

    parser = VisibleText()
    parser.feed("\n".join(html)[:50000])
    return " ".join(parser.chunks)[:20000]


def _code_from_message(message, length: int) -> str | None:
    body = _plain_text(message)
    if not body:
        return None
    # Require a label that actually describes the token. An arbitrary eight
    # character string in a resume or footer is not a verification code.
    pattern = re.compile(
        rf"\b(?:verification|security|confirmation|authentication|one[- ]time)?\s*"
        rf"code\b(?:\s+(?:is|for|to|verify|your|application)){{0,4}}\s*[:#-]?\s*"
        rf"([A-Z0-9]{{{length}}})\b",
        re.I,
    )
    # Preserve the exact characters. A form may treat codes as case-sensitive.
    matches = {match.group(1) for match in pattern.finditer(body)}
    return next(iter(matches)) if len(matches) == 1 else None


class EmailVerificationInbox:
    """Fetch only fresh messages from one configured sender and recipient."""

    def __init__(self, settings: dict, recipient: str):
        self.host = settings.get("imap_host", "").strip()
        self.port = settings.get("imap_port", 993)
        self.username = settings.get("imap_username", "").strip().casefold()
        self.sender = settings.get("sender_address", "").strip().casefold()
        self.recipient = recipient.strip().casefold()
        if not all((self.host, self.username, self.sender, self.recipient)):
            raise RuntimeError("Email verification needs a mailbox, exact sender, and candidate email")
        try:
            self.password = _keyring().get_password(SERVICE, self.username)
        except Exception as exc:  # Keychain and Secret Service may be locked.
            raise RuntimeError("The system credential vault could not be opened") from exc
        if not self.password:
            raise RuntimeError("No IMAP password is saved in the system credential vault")

    def _open(self):
        mailbox = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ssl.create_default_context(), timeout=6)
        try:
            result, _ = mailbox.login(self.username, self.password)
            if result != "OK":
                raise RuntimeError("IMAP login was rejected")
            result, _ = mailbox.select("INBOX", readonly=True)
            if result != "OK":
                raise RuntimeError("IMAP inbox could not be opened read-only")
            return mailbox
        except Exception:
            try:
                mailbox.logout()
            except Exception:
                pass
            raise

    @staticmethod
    def _uids(mailbox, after_uid: int | None = None) -> list[int]:
        if after_uid is None:
            result, data = mailbox.uid("SEARCH", None, "ALL")
        else:
            # IMAP's '*' can include the previous highest UID when the range
            # is empty, so the numeric post-filter below is still required.
            result, data = mailbox.uid("SEARCH", None, "UID", f"{after_uid + 1}:*")
        if result != "OK" or not data:
            raise RuntimeError("IMAP UID search failed")
        uids = [int(uid) for uid in data[0].split()]
        return [uid for uid in uids if after_uid is None or uid > after_uid]

    def baseline(self) -> tuple[int, datetime]:
        """Capture the mailbox position immediately before the first Submit."""
        mailbox = self._open()
        try:
            uids = self._uids(mailbox)
            return (max(uids, default=0), datetime.now(timezone.utc))
        finally:
            mailbox.logout()

    def _fresh_codes(self, baseline_uid: int, started: datetime, length: int) -> set[str]:
        mailbox = self._open()
        try:
            fresh = self._uids(mailbox, after_uid=baseline_uid)
            # A flood of unrelated messages is ambiguous. Do not silently
            # ignore older fresh messages to make one code look unique.
            if len(fresh) > 20:
                raise RuntimeError("Too many new messages to identify one verification code")
            codes: set[str] = set()
            for uid in fresh:
                result, rows = mailbox.uid("FETCH", str(uid), "(BODY.PEEK[])")
                if result != "OK":
                    continue
                raw = next((item[1] for item in rows if isinstance(item, tuple) and isinstance(item[1], bytes)), None)
                if not raw:
                    continue
                message = BytesParser(policy=policy.default).parsebytes(raw)
                if self.sender not in _addresses(message, ("From",)):
                    continue
                if self.recipient not in _addresses(message, ("To", "Delivered-To", "X-Original-To")):
                    continue
                try:
                    sent_at = parsedate_to_datetime(str(message["Date"]))
                    if sent_at.tzinfo is None:
                        continue
                    sent_at = sent_at.astimezone(timezone.utc)
                except (TypeError, ValueError, OverflowError):
                    continue
                now = datetime.now(timezone.utc)
                if not started - timedelta(seconds=60) <= sent_at <= now + timedelta(seconds=60):
                    continue
                if not re.search(r"greenhouse|verif|security|code|application", str(message["Subject"]), re.I):
                    continue
                code = _code_from_message(message, length)
                if code:
                    codes.add(code)
            return codes
        finally:
            mailbox.logout()

    def wait_for_code(self, baseline_uid: int, started: datetime, length: int = 8,
                      timeout_sec: float = 30, poll_sec: float = 2) -> str | None:
        """Return one unique fresh code; ambiguous or missing messages stop."""
        deadline = time.monotonic() + min(max(timeout_sec, 0), 35)
        while True:
            codes = self._fresh_codes(baseline_uid, started, length)
            if len(codes) > 1:
                raise RuntimeError("Multiple fresh verification codes arrived; choose one manually")
            if codes:
                return next(iter(codes))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(poll_sec, remaining))
