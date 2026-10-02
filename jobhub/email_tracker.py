"""Read-only inbox tracking: notice assessments, interviews, offers, and rejections.

The mailbox is opened read-only with an app password kept in the operating
system credential vault (the same one used for Greenhouse email codes). For
each recent message, only the sender, subject, date, and the first few
thousand characters of text are read in memory to classify it; Starployment
stores the sender, subject, date, and the stage it inferred, never the body.

A message updates an application only when it names exactly one tracked
employer, and only forward (applied -> assessment -> interview -> offer, or a
rejection). Anything uncertain is listed for the candidate instead.
"""
from __future__ import annotations

import hashlib
import imaplib
import re
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from . import db

PROVIDERS: dict[str, tuple[str, str, int, str]] = {
    # key: (label, IMAP host, port, where to create an app password)
    "gmail": ("Gmail", "imap.gmail.com", 993, "https://myaccount.google.com/apppasswords"),
    "outlook": ("Outlook / Microsoft 365", "outlook.office365.com", 993,
                "https://account.live.com/proofs/AppPassword"),
    "icloud": ("iCloud Mail", "imap.mail.me.com", 993, "https://account.apple.com/account/manage"),
    "yahoo": ("Yahoo Mail", "imap.mail.yahoo.com", 993, "https://login.yahoo.com/myaccount/security/"),
    "other": ("Other (IMAP)", "", 993, ""),
}


def provider_for(host: str) -> str:
    host = (host or "").strip().lower()
    for key, (_, provider_host, _, _) in PROVIDERS.items():
        if provider_host and host == provider_host:
            return key
    return "other" if host else "gmail"


# ------------------------------------------------------------------ classification
_SIGNALS: dict[str, tuple[tuple[str, float], ...]] = {
    "offer": ((r"\boffer letter\b", 4), (r"pleased to (?:extend|offer)", 4), (r"extend (?:you )?an offer", 4),
              (r"\bverbal offer\b", 3), (r"offer of employment", 4)),
    "rejected": ((r"unfortunately", 2.2), (r"not (?:be )?moving forward", 3.5), (r"decided (?:not )?to (?:move|proceed) forward with other",
                  3.5), (r"(?:pursue|proceed with|move forward with) other candidates", 3.5), (r"not (?:been )?selected", 3),
                 (r"regret to inform", 3.5), (r"position has been filled", 3), (r"no longer (?:under )?consider", 3),
                 (r"will not be (?:moving|proceeding)", 3.5), (r"other applicants whose", 3)),
    "interview": ((r"\binterview(?:s|ing)?\b", 1.6), (r"schedule (?:a|an|your|some) (?:time|call|interview|chat)", 3),
                  (r"phone screen", 3), (r"(?:recruiter|technical) (?:call|screen)", 2.5), (r"\bonsite\b|on-site interview", 2.5),
                  (r"\bsuperday\b", 3.5), (r"calendly\.com|goodtime\.io|availability for", 2.5),
                  (r"invite you to (?:a |an )?(?:interview|call|chat|meet)", 3.5), (r"next round", 2)),
    "assessment": ((r"online assessment|\boa\b", 3), (r"coding (?:challenge|assessment|test)", 3.2),
                   (r"hackerrank|codesignal|codility|hirevue|karat|testgorilla|coderpad|qualified\.io", 3.5),
                   (r"take[- ]home", 3), (r"technical (?:assessment|challenge|exercise)", 3), (r"complete the assessment", 3)),
    "applied": ((r"thank you for (?:applying|your application|your interest)", 2.4),
                (r"(?:application|submission) (?:has been |was )?(?:received|submitted)", 2.6),
                (r"we(?:'ve| have) received your application", 3), (r"thanks for applying", 2.8)),
}
_PRIORITY = ("offer", "rejected", "interview", "assessment", "applied")
_RECRUITING_HINT = re.compile(
    r"application|applying|candidate|recruit|interview|assessment|position|role|opportunit|offer|hiring|talent|"
    r"greenhouse|lever\.co|ashbyhq|workday|hackerrank|codesignal|codility", re.I)


def classify(subject: str, body: str) -> tuple[str | None, float]:
    """Return ``(stage, confidence)`` for a recruiting email, or ``(None, 0)``."""
    text = f"{subject}\n{body}".lower()
    scores = {stage: sum(weight for pattern, weight in rules if re.search(pattern, text))
              for stage, rules in _SIGNALS.items()}
    # A rejection or offer usually thanks the candidate too; that is not a new application.
    if scores["rejected"] >= 3 or scores["offer"] >= 4:
        scores["applied"] = 0
    # "As the next step in our interview process, complete this assessment" is an assessment.
    if scores["assessment"] >= 3 and scores["interview"] < 3:
        scores["interview"] = 0
    best = max(_PRIORITY, key=lambda stage: (scores[stage] >= 2.4, scores[stage], -_PRIORITY.index(stage)))
    if scores[best] < 2.4:
        return None, 0.0
    return best, min(1.0, scores[best] / 5.0)


# ------------------------------------------------------------------ matching
_COMPANY_SUFFIX = re.compile(r"\b(?:inc|llc|ltd|corp|corporation|co|company|technologies|technology|labs?|group|"
                             r"holdings|hq|the)\b\.?", re.I)


def _norm_company(name: str) -> str:
    name = _COMPANY_SUFFIX.sub(" ", (name or "").lower())
    return " ".join(re.findall(r"[a-z0-9]+", name))


def match_application(sender: str, subject: str, body: str, applications: list[dict]) -> dict | None:
    """The single tracked application this message is about, or None when unsure."""
    sender_l, subject_l, body_l = sender.lower(), subject.lower(), body[:6000].lower()
    domain = sender_l.rpartition("@")[-1]
    ranked: list[tuple[float, dict]] = []
    for app in applications:
        company = _norm_company(str(app.get("company") or ""))
        if len(company) < 2:
            continue
        compact = company.replace(" ", "")
        pattern = re.compile(rf"\b{re.escape(company)}\b")
        score = 0.0
        if pattern.search(sender_l) or (len(compact) >= 4 and compact in domain.replace("-", "")):
            score += 3
        if pattern.search(subject_l):
            score += 2.5
        if pattern.search(body_l):
            score += 1
        title = str(app.get("title") or "").lower()
        if title and len(title) > 6 and title in f"{subject_l} {body_l}":
            score += 1.5
        if score:
            ranked.append((score, app))
    if not ranked:
        return None
    ranked.sort(key=lambda item: -item[0])
    best_score, best = ranked[0]
    if best_score < 2.5:
        return None
    # Several applications at the same employer: prefer a title match, else the newest.
    same = [app for score, app in ranked if _norm_company(app.get("company") or "") == _norm_company(best.get("company") or "")]
    if len(same) > 1:
        titled = [app for app in same if str(app.get("title") or "").lower() in f"{subject_l} {body_l}"]
        if len(titled) == 1:
            return titled[0]
        return max(same, key=lambda app: str(app.get("finished_at") or app.get("started_at") or ""))
    others = [score for score, app in ranked[1:] if _norm_company(app.get("company") or "") != _norm_company(best.get("company") or "")]
    if others and others[0] >= best_score:
        return None   # names two employers equally; let the candidate decide
    return best


# ------------------------------------------------------------------ mailbox
@dataclass
class SyncReport:
    scanned: int = 0
    recruiting: int = 0
    updates: list[dict] = field(default_factory=list)      # {application_id, company, title, stage}
    unmatched: list[dict] = field(default_factory=list)    # {sender, subject, stage}

    @property
    def summary(self) -> str:
        if not self.updates:
            return f"Checked {self.scanned} recent emails. No new application updates."
        parts = [f"{u['company']}: {u['stage']}" for u in self.updates[:4]]
        return f"{len(self.updates)} application update(s) · " + ", ".join(parts)


def _text(message) -> str:
    from .apply.email_verification import _plain_text
    try:
        return _plain_text(message)[:8000]
    except Exception:  # noqa: BLE001
        return ""


def _message_key(message, uid: bytes) -> str:
    raw = str(message.get("Message-ID") or "") or f"uid:{uid!r}:{message.get('Date')}:{message.get('Subject')}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]


class Mailbox:
    """A read-only IMAP session using the vault-stored app password."""

    def __init__(self, verification_settings: dict):
        from .apply.email_verification import SERVICE, _keyring
        self.host = str(verification_settings.get("imap_host") or "").strip()
        self.port = int(verification_settings.get("imap_port") or 993)
        self.username = str(verification_settings.get("imap_username") or "").strip().casefold()
        if not self.host or not self.username:
            raise RuntimeError("Add your mailbox in Settings → Connections first")
        try:
            self.password = _keyring().get_password(SERVICE, self.username)
        except Exception as error:  # noqa: BLE001
            raise RuntimeError("The system credential vault could not be opened") from error
        if not self.password:
            raise RuntimeError("No app password is saved for this mailbox")

    def open(self):
        mailbox = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ssl.create_default_context(), timeout=20)
        try:
            mailbox.login(self.username, self.password)
            result, _ = mailbox.select("INBOX", readonly=True)
            if result != "OK":
                raise RuntimeError("The inbox could not be opened read-only")
            return mailbox
        except Exception:
            try:
                mailbox.logout()
            except Exception:  # noqa: BLE001
                pass
            raise

    def recent(self, days: int, limit: int = 400):
        """Yield ``(uid, message)`` for recent mail, newest first, headers then a body excerpt."""
        mailbox = self.open()
        try:
            since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
            result, data = mailbox.uid("SEARCH", None, "SINCE", since)
            if result != "OK" or not data:
                return
            uids = data[0].split()[-limit:][::-1]
            for uid in uids:
                result, rows = mailbox.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
                header = next((item[1] for item in rows or [] if isinstance(item, tuple)), b"")
                message = BytesParser(policy=policy.default).parsebytes(header or b"")
                if not _RECRUITING_HINT.search(f"{message.get('Subject', '')} {message.get('From', '')}"):
                    yield uid, message, None
                    continue
                result, rows = mailbox.uid("FETCH", uid, "(BODY.PEEK[])")
                raw = next((item[1] for item in rows or [] if isinstance(item, tuple)), b"")
                full = BytesParser(policy=policy.default).parsebytes(raw[:400_000]) if raw else message
                yield uid, full, _text(full)
        finally:
            try:
                mailbox.logout()
            except Exception:  # noqa: BLE001
                pass


def test_mailbox(verification_settings: dict) -> tuple[bool, str]:
    try:
        mailbox = Mailbox(verification_settings).open()
        mailbox.logout()
        return True, "Connected. Starployment can read your inbox (read-only)."
    except imaplib.IMAP4.error as error:
        return False, f"The mail server rejected the sign-in. Check the address and app password. ({str(error)[:120]})"
    except Exception as error:  # noqa: BLE001
        return False, str(error)[:240]


def sync(settings: dict | None = None, *, messages=None) -> SyncReport:
    """Classify recent mail and advance matching applications. ``messages`` is for tests."""
    settings = settings or db.get_settings()
    tracking = settings.get("email_tracking") or {}
    days = int(tracking.get("lookback_days", 30))
    applications = db.list_tracker()
    known = db.known_mail_keys()
    report = SyncReport()
    source = messages if messages is not None else Mailbox(settings.get("email_verification") or {}).recent(days)
    for uid, message, body in source:
        report.scanned += 1
        if body is None:
            continue
        key = _message_key(message, uid)
        if key in known:
            continue
        subject = str(message.get("Subject") or "")
        sender_header = str(message.get("From") or "")
        senders = getaddresses([sender_header])
        sender = " ".join(part for pair in senders for part in pair if part) or sender_header
        stage, confidence = classify(subject, body)
        if stage is None and tracking.get("use_luna") and _RECRUITING_HINT.search(subject):
            try:
                from .luna import classify_email
                stage = classify_email(subject, body, settings)
                confidence = 0.6 if stage else 0.0
            except Exception:  # noqa: BLE001 - optional second opinion
                stage = None
        if stage is None:
            continue
        report.recruiting += 1
        try:
            received = parsedate_to_datetime(str(message.get("Date"))).astimezone(timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError, OverflowError):
            received = datetime.now(timezone.utc).isoformat(timespec="seconds")
        app = match_application(sender, subject, body, applications)
        db.record_mail_event(message_key=key, received_at=received, sender=sender, subject=subject, stage=stage,
                             application_id=app["id"] if app else None, confidence=confidence)
        known.add(key)
        if app is None:
            report.unmatched.append({"sender": sender, "subject": subject, "stage": stage})
            continue
        try:
            changed = db.set_application_stage(int(app["id"]), stage, "email")
        except ValueError:
            changed = False
        if changed:
            app["stage"] = stage
            report.updates.append({"application_id": app["id"], "company": app.get("company"),
                                   "title": app.get("title"), "stage": stage})
            db.add_event(f"Inbox update: {app.get('company')} · {app.get('title')} → {stage}", "success"
                         if stage in ("interview", "offer", "assessment") else "info", app.get("job_id"))
    return report
