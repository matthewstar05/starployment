"""Opt-in, read-only IMAP lookup for a fresh Greenhouse application code.

The mailbox password lives in the operating system credential vault when
Keychain protection is on, otherwise in an owner-only secrets file. No
message body or code is persisted, logged, or sent to an LLM. A code is usable
only when it arrived after the application began and came from the exact sender
the candidate configured for this mailbox.
"""
from __future__ import annotations

import dis
import imaplib
import inspect
import json
import os
import re
import ssl
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

# Kept from the JobHub name so passwords saved before the rename keep working.
SERVICE = "JobHub IMAP verification"
_NATIVE_BACKENDS = ("keyring.backends.macOS", "keyring.backends.Windows", "keyring.backends.SecretService")

URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.I)
EXCLUDE_DOMAINS = {
    "facebook.com", "twitter.com", "x.com", "linkedin.com", "instagram.com",
    "youtube.com", "github.com",
}
EXCLUDE_TERMS = re.compile(r"unsubscribe|opt[-_]?out|privacy|terms|policy|help|support|contact|manage[-_]?preferences", re.I)
VERIFY_TERMS = re.compile(r"verify|verif|confirm|token|activate|security|application", re.I)


@dataclass
class VerificationToken:
    kind: str  # 'code' | 'link'
    value: str
    sender: str = ""
    subject: str = ""


class Baseline(tuple):
    """3-tuple (uidvalidity, max_uid, started_at) with 2-tuple backwards-compatibility."""

    def __new__(cls, uidvalidity: int | None, max_uid: int, started_at: datetime):
        return super().__new__(cls, (uidvalidity, max_uid, started_at))

    @property
    def uidvalidity(self) -> int | None:
        return self[0]

    @property
    def max_uid(self) -> int:
        return self[1]

    @property
    def started_at(self) -> datetime:
        return self[2]

    def __iter__(self):
        try:
            frame = inspect.currentframe().f_back
            if frame:
                instructions = list(dis.get_instructions(frame.f_code))
                for instr in instructions:
                    if instr.offset >= frame.f_lasti and instr.opname == "UNPACK_SEQUENCE":
                        if instr.argval == 2:
                            return iter((self[1], self[2]))
                        break
        except Exception:
            pass
        return super().__iter__()


def redact_token(value: str, kind: str = "code") -> str:
    """Sanitize tokens for logs by masking middle of codes or stripping URL secrets."""
    if not value:
        return ""
    val = value.strip()
    if kind == "code":
        if len(val) <= 2:
            return "*" * len(val)
        if len(val) <= 4:
            return val[0] + "*" * (len(val) - 2) + val[-1]
        keep = 2 if len(val) >= 6 else 1
        return val[:keep] + "*" * (len(val) - 2 * keep) + val[-keep:]
    if kind == "link":
        try:
            parts = urllib.parse.urlsplit(val)
            # Mask path segments that look like UUIDs, hashes, or alphanumeric secrets (contain digits and letters, or hex/UUID)
            path = re.sub(
                r"/(?:[0-9a-fA-F-]{20,}|(?:[0-9]+[a-zA-Z]+|[a-zA-Z]+[0-9]+)[0-9a-zA-Z_-]{6,})(?=/|$)",
                "/[redacted]",
                parts.path,
            )
            return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))
        except Exception:
            return "[redacted-link]"
    return "[redacted]"


class _FileVault:
    """Owner-only secrets file for when Keychain protection is off.

    The installed app is ad-hoc signed, so every rebuild looks like a new app
    to Keychain and asks again. This file needs no prompt. A secret saved in
    Keychain earlier is copied here the first time it is read.
    """

    class errors:
        class PasswordDeleteError(Exception):
            pass

    @staticmethod
    def _path():
        from .. import config
        return config.DATA / "secrets.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        from .. import config
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            config.secure_file(temporary)
            json.dump(data, handle, indent=2)
        os.replace(temporary, path)
        if not data:
            path.unlink(missing_ok=True)

    def get_password(self, service: str, username: str) -> str | None:
        value = self._load().get(service, {}).get(username)
        if value:
            return value
        try:
            value = _system_vault().get_password(service, username)
        except Exception:  # noqa: BLE001 - no vault or a locked one means nothing to copy
            return None
        if value:
            self.set_password(service, username, value)
        return value

    def set_password(self, service: str, username: str, password: str) -> None:
        data = self._load()
        data.setdefault(service, {})[username] = password
        self._save(data)

    def delete_password(self, service: str, username: str) -> None:
        data = self._load()
        found = data.get(service, {}).pop(username, None) is not None
        if not data.get(service):
            data.pop(service, None)
        self._save(data)
        try:
            vault = _system_vault()
            vault.delete_password(service, username)
            found = True
        except Exception:  # noqa: BLE001 - a copy left in Keychain is optional to clear
            pass
        if not found:
            raise self.errors.PasswordDeleteError(username)

    def move_to_system_vault(self) -> None:
        """Turning protection on: hand every saved secret to Keychain."""
        data = self._load()
        vault = _system_vault()
        for service, entries in data.items():
            for username, password in entries.items():
                vault.set_password(service, username, password)
        self._save({})


def keychain_protection() -> bool:
    try:
        from .. import db
        return bool(db.get_settings().get("keychain_protection", False))
    except Exception:  # noqa: BLE001 - no database yet means the default
        return False


def _keyring():
    """The vault Starployment's secrets use, chosen by Settings → Security."""
    return _system_vault() if keychain_protection() else _FileVault()


def _system_vault():
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


def _raw_message_bodies(message) -> tuple[str, str]:
    """Return concatenated (plain_text, html_text) from a message."""
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
    return "\n".join(text)[:30000], "\n".join(html)[:60000]


def _plain_text(message) -> str:
    plain, html = _raw_message_bodies(message)
    if plain:
        return plain[:20000]

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
    parser.feed(html)
    return " ".join(parser.chunks)[:20000]


def _is_verification_link(url: str, anchor_text: str = "", allowed_hosts: list[str] | set[str] | None = None) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if any(host == d or host.endswith("." + d) for d in EXCLUDE_DOMAINS):
        return False
    if allowed_hosts:
        allowed = {h.strip().lower() for h in allowed_hosts if h.strip()}
        if not any(host == h or host.endswith("." + h) for h in allowed):
            return False
    target = f"{parsed.path}?{parsed.query} {anchor_text}"
    if EXCLUDE_TERMS.search(target):
        return False
    return bool(VERIFY_TERMS.search(target))


def _links_from_message(message, allowed_hosts: list[str] | set[str] | None = None) -> set[str]:
    plain, html = _raw_message_bodies(message)
    candidates: list[tuple[str, str]] = []

    if html:
        class LinkParser(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.links: list[tuple[str, str]] = []
                self.current_href: str | None = None
                self.current_text: list[str] = []

            def handle_starttag(self, tag, attrs):
                if tag.lower() == "a":
                    d = dict(attrs)
                    href = d.get("href")
                    if href:
                        self.current_href = href.strip()
                        self.current_text = []

            def handle_data(self, data):
                if self.current_href:
                    self.current_text.append(data)

            def handle_endtag(self, tag):
                if tag.lower() == "a" and self.current_href:
                    self.links.append((self.current_href, " ".join(self.current_text).strip()))
                    self.current_href = None
                    self.current_text = []

        parser = LinkParser()
        try:
            parser.feed(html)
            candidates.extend(parser.links)
        except Exception:
            pass

    for text_block in (plain, html):
        if text_block:
            for match in URL_RE.finditer(text_block):
                cleaned = match.group(0).rstrip(".,;:!?)>]}'")
                candidates.append((cleaned, ""))

    valid_links: set[str] = set()
    for url, anchor in candidates:
        if _is_verification_link(url, anchor, allowed_hosts):
            valid_links.add(url.strip())
    return valid_links


def _code_from_message(message, length: int | None = None) -> str | None:
    body = _plain_text(message)
    if not body:
        return None
    # Require a label that actually describes the token. An arbitrary
    # string in a resume or footer is not a verification code.
    len_pat = str(length) if length is not None else "4,12"
    pattern = re.compile(
        rf"\b(?:verification|security|confirmation|authentication|one[- ]time)?\s*"
        rf"code\b(?:\s+(?:is|for|to|verify|your|application)){{0,4}}\s*[:#-]?\s*"
        rf"([A-Za-z0-9]{{{len_pat}}})\b",
        re.I,
    )
    # Greenhouse: "Copy and paste this code into the security code field on your
    # application:" with the code on its own line after the colon.
    labeled = re.compile(rf"\bcode\b[^\n:.]{{0,80}}:\s*([A-Za-z0-9]{{{len_pat}}})\b", re.I)
    # Preserve the exact characters. A form may treat codes as case-sensitive.
    matches = {match.group(1) for match in pattern.finditer(body)} | {
        match.group(1) for match in labeled.finditer(body)}
    # The words after "code" ("this code into the field") are not codes.
    matches = {m for m in matches if not re.fullmatch(r"[a-z]+", m)}
    return next(iter(matches)) if len(matches) == 1 else None


class EmailVerificationInbox:
    """Fetch only fresh messages from one configured sender and recipient."""

    def __init__(self, settings: dict, recipient: str):
        self.host = settings.get("imap_host", "").strip()
        self.port = settings.get("imap_port", 993)
        self.username = settings.get("imap_username", "").strip().casefold()
        self.sender = settings.get("sender_address", "").strip().casefold()
        self.recipient = recipient.strip().casefold()
        if not all((self.host, self.username, self.recipient)):
            raise RuntimeError("Email verification needs a mailbox host, username, and candidate email")
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
    def _uidvalidity(mailbox) -> int | None:
        try:
            res = mailbox.response("UIDVALIDITY")
            if res and len(res) > 1 and res[1] and res[1][0]:
                raw = res[1][0]
                if isinstance(raw, bytes):
                    raw = raw.decode()
                return int(str(raw).strip())
        except Exception:
            pass
        return None

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

    def baseline(self) -> Baseline:
        """Capture the mailbox position immediately before the first Submit."""
        mailbox = self._open()
        try:
            uidvalidity = self._uidvalidity(mailbox)
            uids = self._uids(mailbox)
            return Baseline(uidvalidity, max(uids, default=0), datetime.now(timezone.utc))
        finally:
            mailbox.logout()

    def _fresh_messages(self, baseline_uid: int, started: datetime,
                        baseline_uidvalidity: int | None = None,
                        employer: str | None = None) -> list[tuple[int, object]]:
        mailbox = self._open()
        try:
            curr_validity = self._uidvalidity(mailbox)
            if baseline_uidvalidity is not None and curr_validity is not None and curr_validity != baseline_uidvalidity:
                raise RuntimeError("IMAP UIDVALIDITY changed during verification")
            fresh_uids = self._uids(mailbox, after_uid=baseline_uid)
            if len(fresh_uids) > 20:
                raise RuntimeError("Too many new messages to identify one verification code")
            matched: list[tuple[int, object]] = []
            for uid in fresh_uids:
                result, rows = mailbox.uid("FETCH", str(uid), "(BODY.PEEK[])")
                if result != "OK":
                    continue
                raw = next((item[1] for item in rows if isinstance(item, tuple) and isinstance(item[1], bytes)), None)
                if not raw:
                    continue
                message = BytesParser(policy=policy.default).parsebytes(raw)
                from_addrs = _addresses(message, ("From",))
                if self.sender:
                    if self.sender not in from_addrs:
                        continue
                else:
                    from_str = " ".join(from_addrs).lower()
                    if employer and employer.strip().lower() in from_str:
                        pass
                    elif re.search(r"greenhouse|ashby|lever|workday|jobs|careers|talent|recruit|verify|verification|confirm|no-?reply", from_str):
                        pass
                    else:
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
                subject = str(message["Subject"] or "")
                body = _plain_text(message)
                combined = f"{subject} {body}"
                if employer and employer.strip():
                    # "Shieldai" in the job list is "Shield AI" in the email.
                    squash = lambda text: re.sub(r"[^a-z0-9]", "", text.lower())  # noqa: E731
                    if squash(employer) not in squash(combined):
                        continue
                if not re.search(r"greenhouse|verif|activat|security|code|confirm|token|application", subject, re.I):
                    continue
                matched.append((uid, message))
            return matched
        finally:
            mailbox.logout()

    def wait_for_token(self, baseline_uid: int, started: datetime,
                       baseline_uidvalidity: int | None = None,
                       length: int | None = None,
                       employer: str | None = None,
                       allowed_hosts: list[str] | set[str] | None = None,
                       timeout_sec: float = 30, poll_sec: float = 2,
                       kind: str | None = None) -> VerificationToken | None:
        """Return one unique fresh verification token (code or link); ``kind`` keeps only one sort,
        so a code meant for another application never counts against a link (or the reverse)."""
        deadline = time.monotonic() + min(max(timeout_sec, 0), 35)
        while True:
            messages = self._fresh_messages(baseline_uid, started, baseline_uidvalidity, employer)
            tokens: list[VerificationToken] = []
            for _, msg in messages:
                sender = str(msg["From"] or "")
                subject = str(msg["Subject"] or "")
                code = _code_from_message(msg, length) if kind != "link" else None
                if code:
                    tokens.append(VerificationToken(kind="code", value=code, sender=sender, subject=subject))
                links = _links_from_message(msg, allowed_hosts) if kind != "code" else set()
                for link in links:
                    tokens.append(VerificationToken(kind="link", value=link, sender=sender, subject=subject))

            # Deduplicate identical tokens across messages if any
            unique_tokens = {(t.kind, t.value): t for t in tokens}
            if len(unique_tokens) > 1:
                raise RuntimeError("Multiple fresh verification codes arrived; choose one manually")
            if len(unique_tokens) == 1:
                return next(iter(unique_tokens.values()))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(poll_sec, remaining))

    def wait_for_code(self, baseline_uid: int, started: datetime, length: int = 8,
                      timeout_sec: float = 30, poll_sec: float = 2,
                      baseline_uidvalidity: int | None = None,
                      employer: str | None = None) -> str | None:
        """Return one unique fresh code; backwards-compatible helper."""
        token = self.wait_for_token(baseline_uid, started, baseline_uidvalidity=baseline_uidvalidity,
                                   length=length, employer=employer, timeout_sec=timeout_sec, poll_sec=poll_sec)
        if token and token.kind == "code":
            return token.value
        return None

    def wait_for_link(self, baseline_uid: int, started: datetime,
                      allowed_hosts: list[str] | set[str] | None = None,
                      timeout_sec: float = 30, poll_sec: float = 2,
                      baseline_uidvalidity: int | None = None,
                      employer: str | None = None) -> str | None:
        """Return one unique fresh verification link; backwards-compatible helper."""
        token = self.wait_for_token(baseline_uid, started, baseline_uidvalidity=baseline_uidvalidity,
                                   allowed_hosts=allowed_hosts, employer=employer,
                                   timeout_sec=timeout_sec, poll_sec=poll_sec, kind="link")
        if token and token.kind == "link":
            return token.value
        return None
