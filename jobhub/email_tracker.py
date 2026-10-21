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
import time
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
    "rejected": ((r"unfortunately", 2.2), (r"not (?:to )?(?:be )?(?:move|moving|proceed|proceeding) forward", 3.5),
                 (r"(?:pursue|proceed with|move forward|moving forward)[^.]{0,40}\bwith (?:other|another) (?:candidate|applicant)",
                  3.5), (r"(?:other|another) candidates? (?:has been|have been|was|were) (?:offered|selected|chosen)", 3.5),
                 (r"candidates? whose (?:background|experience|skills|qualifications)[^.]{0,80}(?:better|more closely)", 3.5),
                 (r"not (?:been )?selected", 3), (r"regret to inform", 3.5), (r"position has been filled", 3),
                 (r"no longer (?:under )?consider", 3), (r"will not be (?:moving|proceeding|advancing)", 3.5),
                 (r"other applicants whose", 3), (r"(?:do|does) not qualify", 3), (r"decided to (?:pursue|go with|move ahead with) other", 3.5), (r"unable to (?:offer|move forward)", 3)),
    "interview": ((r"\binterview(?:s|ing)?\b", 1.6), (r"schedule (?:a|an|your|some) (?:time|call|interview|chat)", 3),
                  (r"phone screen", 3), (r"(?:recruiter|technical) (?:call|screen)", 2.5), (r"\bonsite\b|on-site interview", 2.5),
                  (r"\bsuperday\b", 3.5), (r"calendly\.com|goodtime\.io|availability for", 2.5),
                  (r"invite you to (?:a |an )?(?:interview|call|chat|meet)", 3.5), (r"next round", 2)),
    "assessment": ((r"online assessment|\boa\b", 3), (r"coding (?:challenge|assessment|test)", 3.2),
                   (r"hackerrank|codesignal|codility|hirevue|karat|testgorilla|coderpad|qualified\.io", 3.5),
                   (r"take[- ]home", 3), (r"technical (?:assessment|challenge|exercise)", 3), (r"complete the assessment", 3)),
    "applied": ((r"thank you for (?:applying|your application|your interest)", 2.4),
                (r"(?:application|submission) (?:has been |was )?(?:received|submitted)", 2.6),
                (r"received your application", 3), (r"thanks for applying", 2.8)),
}
_PRIORITY = ("offer", "rejected", "interview", "assessment", "applied")
_RECRUITING_HINT = re.compile(
    r"application|applying|applied|candida|recruit|interview|assessment|position|role|opportunit|offer|hiring|talent|"
    r"your interest|update|careers|greenhouse|lever\.co|ashbyhq|workday|hackerrank|codesignal|codility", re.I)
# "If you move forward, the next stage is an assessment" describes the process; it is not an invitation.
_CONDITIONAL_CLAUSE = re.compile(r"\b(?:if|should|whether|in case|once|when)\b[^.!?\n]{0,160}")
# What an actual invitation asks the candidate to do. An acknowledgement ("thanks for applying")
# moves an application forward only with one of these, not by describing later stages.
_INVITATION = {
    "assessment": re.compile(r"hackerrank|codesignal|codility|hirevue|karat|testgorilla|coderpad|qualified\.io|"
                             r"(?:complete|start|take|begin|access)\s+(?:the|this|your|an?|our)\s[^.\n]{0,40}"
                             r"(?:assessment|challenge|test|exercise)|(?:assessment|challenge) link|invited to (?:complete|take)"),
    "interview": re.compile(r"schedule (?:a|an|your|some) (?:time|call|interview|chat)|phone screen|calendly\.com|"
                            r"goodtime\.io|availability for|invite you to|\bsuperday\b|book (?:a|your) (?:time|slot)"),
    "offer": re.compile(r"offer letter|pleased to (?:extend|offer)|extend (?:you )?an offer|offer of employment"),
}


def classify(subject: str, body: str) -> tuple[str | None, float]:
    """Return ``(stage, confidence)`` for a recruiting email, or ``(None, 0)``."""
    text = f"{subject}\n{body}".lower().replace("\u2019", "'")
    # Conditional clauses ("If we decide not to move forward, we'll let you know") are not news;
    # only the acknowledgement itself is read from the full text.
    settled = _CONDITIONAL_CLAUSE.sub(" ", text)
    scores = {stage: sum(weight for pattern, weight in rules if re.search(pattern, text if stage == "applied" else settled))
              for stage, rules in _SIGNALS.items()}
    # A rejection or offer usually thanks the candidate too; that is not a new application.
    if scores["rejected"] >= 3 or scores["offer"] >= 4:
        scores["applied"] = 0
    # "As the next step in our interview process, complete this assessment" is an assessment.
    if scores["assessment"] >= 3 and scores["interview"] < 3:
        scores["interview"] = 0
    # An acknowledgement that only describes what may come later stays "applied".
    # One that also invites the candidate to the next step is that step.
    if scores["applied"] >= 2.4:
        for stage, invitation in _INVITATION.items():
            if not invitation.search(settled):
                scores[stage] = 0
        if any(scores[stage] >= 2.4 for stage in _INVITATION):
            scores["applied"] = 0
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


# Leading words too common to identify an employer on their own ("General Matter", "Applied Intuition").
_GENERIC_LEAD = frozenset("""general global digital american united national first advanced applied data software systems
    health capital energy financial international new north south east west bay city open smart blue red green""".split())


def _words(text: str) -> str:
    """Lowercase words separated by single spaces, padded, so "Alarm.com" reads as " alarm com "."""
    return " " + " ".join(re.findall(r"[a-z0-9]+", (text or "").lower())) + " "


def match_application(sender: str, subject: str, body: str, applications: list[dict]) -> dict | None:
    """The single tracked application this message is about, or None when unsure."""
    sender_l, subject_l, body_l = sender.lower(), subject.lower(), body[:6000].lower()
    domain = sender_l.rpartition("@")[-1]
    domain_labels = set(re.findall(r"[a-z0-9]+", domain.replace("-", "")))
    sender_w, subject_w, body_w = _words(sender_l), _words(subject_l), _words(body_l)
    sender_compact = sender_w.replace(" ", "")
    ranked: list[tuple[float, dict]] = []
    for app in applications:
        company = _norm_company(re.sub(r"\s+-\s+external\b.*$", "", str(app.get("company") or ""), flags=re.I))
        if len(company) < 2:
            continue
        compact = company.replace(" ", "")
        needle = f" {company} "
        lead = company.split()[0]
        # A distinctive first word ("Toshiba" for "Toshiba Global Commerce Solutions") is a weaker signal.
        lead_ok = " " in company and len(lead) >= 5 and lead not in _GENERIC_LEAD
        score = 0.0
        if (needle in sender_w or (len(compact) >= 4 and compact in domain.replace("-", ""))
                or (len(compact) >= 6 and compact in sender_compact) or (lead_ok and lead in domain_labels)):
            score += 3
        if needle in subject_w:
            score += 2.5
        elif lead_ok and f" {lead} " in subject_w:
            score += 2
        if needle in body_w:
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
    archived: int = 0
    archive_error: str = ""

    @property
    def summary(self) -> str:
        archived = f" · archived {self.archived} rejection email{'s' if self.archived != 1 else ''}" if self.archived else ""
        if not self.updates:
            return f"Checked {self.scanned} recent emails. No new application updates{archived}."
        parts = [f"{u['company']}: {u['stage']}" for u in self.updates[:4]]
        return f"{len(self.updates)} application update(s) · " + ", ".join(parts) + archived


def _text(message) -> str:
    from .apply.email_verification import _plain_text
    try:
        return _plain_text(message)[:8000]
    except Exception:  # noqa: BLE001
        return ""


def _message_key(message, uid: bytes) -> str:
    raw = str(message.get("Message-ID") or "") or f"uid:{uid!r}:{message.get('Date')}:{message.get('Subject')}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]


_HEADER_BATCH = 200
_RECONNECTS = 3
_CONNECTION_ERRORS = (imaplib.IMAP4.abort, OSError)   # includes timeouts, SSL, DNS, and broken pipes
# Bump when ``classify``, ``_RECRUITING_HINT`` or the Luna reading changes, so mail
# already read and set aside (``db.mark_mail_seen``) is read again under the new rules.
RULES_VERSION = 1
OFFLINE = "Couldn't reach your mail server"


class MailUnavailable(RuntimeError):
    """The mail server could not be reached or stopped responding (offline, sleep, VPN changes)."""


ARCHIVE_LABEL = "Starployment/Rejected"


class Mailbox:
    """An IMAP session using the vault-stored app password. Reading is always read-only."""

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
        mailbox = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ssl.create_default_context(), timeout=30)
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

    def archive(self, items: list[tuple[bytes, str]]) -> int:
        """Take these ``(uid, Message-ID)`` messages out of the Inbox. Returns how many moved.

        The only change Starployment ever makes to a mailbox, and only when the
        candidate turned on "Archive rejections". Nothing is deleted: Gmail keeps
        the message in All Mail with the label ``ARCHIVE_LABEL``; other servers
        move it to their Archive folder. Each message's Message-ID is checked
        again first, so a changed Inbox can never archive the wrong email."""
        if not items:
            return 0
        mailbox = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ssl.create_default_context(), timeout=30)
        try:
            mailbox.login(self.username, self.password)
            result, _ = mailbox.select("INBOX")
            if result != "OK":
                raise RuntimeError("The inbox could not be opened to archive messages")
            try:   # many servers list MOVE only after login
                mailbox.capabilities = tuple(mailbox.capability()[1][-1].decode().upper().split())
            except Exception:  # noqa: BLE001 - keep the greeting's list
                pass
            capabilities = {str(c).upper() for c in mailbox.capabilities}
            gmail = "X-GM-EXT-1" in capabilities
            folder = None if gmail else self._archive_folder(mailbox, capabilities)
            moved = 0
            for uid, message_id in items:
                result, rows = mailbox.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                header = next((item[1] for item in rows or [] if isinstance(item, tuple)), b"")
                current = str(BytesParser(policy=policy.default).parsebytes(header or b"").get("Message-ID") or "")
                if not message_id or current.strip().strip("<>") != message_id:
                    continue
                if gmail:
                    labelled, _ = mailbox.uid("STORE", uid, "+X-GM-LABELS", f'("{ARCHIVE_LABEL}")')
                    result, _ = mailbox.uid("STORE", uid, "-X-GM-LABELS", "(\\Inbox)") if labelled == "OK" else (labelled, None)
                else:
                    result, _ = mailbox.uid("MOVE", uid, folder)
                moved += result == "OK"
            return moved
        finally:
            try:
                mailbox.logout()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _archive_folder(mailbox, capabilities: set[str]) -> str:
        """The server's own Archive folder (RFC 6154 ``\\Archive``). Never created or guessed."""
        if "MOVE" not in capabilities:
            raise RuntimeError("This mail server cannot move messages, so rejections stay in the Inbox")
        result, rows = mailbox.list()
        for row in rows or []:
            line = row.decode("utf-8", "replace") if isinstance(row, bytes) else str(row)
            found = re.match(r'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', line)
            if found and "\\archive" in found.group("flags").lower():
                return found.group("name")
        raise RuntimeError("This mailbox has no Archive folder, so rejections stay in the Inbox")

    def recent(self, days: int, limit: int = 3000, skip=None):
        """Yield ``(uid, message, body)`` for recent mail, newest first.

        Headers are fetched in batches; only messages whose subject or sender
        looks like recruiting mail are fetched in full (``body`` is None otherwise),
        and not those whose key ``skip(key)`` says were already read.
        A dropped connection (timeouts, sleep, Wi-Fi changes) is reopened and the
        read resumes after the last message already handed out. The reconnect
        budget is per stall: a read that keeps making progress keeps going."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
        uids: list[bytes] | None = None
        done = 0
        reconnects = 0
        progress = 0
        while True:
            mailbox = None
            try:
                mailbox = self.open()
                if uids is None:
                    result, data = mailbox.uid("SEARCH", None, "SINCE", since)
                    if result != "OK" or not data:
                        return
                    uids = data[0].split()[-limit:][::-1]
                while done < len(uids):
                    chunk = uids[done:done + _HEADER_BATCH]
                    result, rows = mailbox.uid("FETCH", b",".join(chunk),
                                               "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
                    headers: dict[bytes, bytes] = {}
                    for item in rows or []:
                        if isinstance(item, tuple) and (found := re.search(rb"\bUID (\d+)", item[0] or b"")):
                            headers[found.group(1)] = item[1] or b""
                    for uid in chunk:
                        message = BytesParser(policy=policy.default).parsebytes(headers.get(uid, b""))
                        body = None
                        if (_RECRUITING_HINT.search(f"{message.get('Subject', '')} {message.get('From', '')}")
                                and not (skip and skip(_message_key(message, uid)))):
                            result, body_rows = mailbox.uid("FETCH", uid, "(BODY.PEEK[])")
                            raw = next((item[1] for item in body_rows or [] if isinstance(item, tuple)), b"")
                            message = BytesParser(policy=policy.default).parsebytes(raw[:400_000]) if raw else message
                            body = _text(message)
                        done += 1
                        yield uid, message, body
                return
            except _CONNECTION_ERRORS as error:
                if done > progress:
                    progress, reconnects = done, 0
                reconnects += 1
                if reconnects > _RECONNECTS:
                    raise MailUnavailable(f"{OFFLINE} ({str(error)[:80] or type(error).__name__})") from error
                time.sleep(min(10, 2 * reconnects))
            finally:
                if mailbox is not None:
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


def sync(settings: dict | None = None, *, messages=None, archiver=None) -> SyncReport:
    """Classify recent mail and advance matching applications.

    With ``email_tracking.archive_rejections`` on, a rejection is archived once it
    names one tracked application whose stage is Rejected. ``messages`` and
    ``archiver`` are for tests."""
    settings = settings or db.get_settings()
    tracking = settings.get("email_tracking") or {}
    days = int(tracking.get("lookback_days", 30))
    applications = db.list_tracker()
    known = db.known_mail_keys()
    without_id = db.mail_keys_without_id()
    rules = RULES_VERSION * 10 + bool(tracking.get("use_luna"))   # turning Luna on reads set-aside mail again
    db.prune_mail_seen(rules, (datetime.now(timezone.utc) - timedelta(days=days + 2)).isoformat(timespec="seconds"))
    seen = db.seen_mail_keys(rules)
    report = SyncReport()
    mailbox = None if messages is not None else Mailbox(settings.get("email_verification") or {})
    # Mail read before and set aside is not downloaded again; recorded mail is
    # always re-read, since its application may have become tracked since.
    source = messages if mailbox is None else mailbox.recent(days, skip=lambda key: key in seen and key not in known)
    rejections: list[tuple[bytes, str, int]] = []   # (uid, Message-ID, application id) still in the Inbox
    set_aside: list[str] = []
    try:
        _read(source, report, tracking, settings, applications, known, without_id, rejections, seen, set_aside, rules)
    finally:
        db.mark_mail_seen(set_aside, rules)   # keep progress even when the connection drops
    if tracking.get("archive_rejections") and rejections:
        _archive_rejections(report, rejections, archiver or (mailbox.archive if mailbox else None))
    return report


def _read(source, report, tracking, settings, applications, known, without_id, rejections, seen, set_aside,
          rules) -> None:
    for uid, message, body in source:
        report.scanned += 1
        if body is None:
            continue
        key = _message_key(message, uid)
        message_id = str(message.get("Message-ID") or "").strip().strip("<>")
        if key in without_id and message_id:
            db.set_mail_message_id(key, message_id)   # recorded before Message-IDs were kept
            without_id.discard(key)
        subject = str(message.get("Subject") or "")
        sender_header = str(message.get("From") or "")
        senders = getaddresses([sender_header])
        sender = " ".join(part for pair in senders for part in pair if part) or sender_header
        stage, confidence = classify(subject, body)
        if stage is None and key in known:
            continue   # read before (perhaps by Luna); keep that reading instead of asking again
        if stage is None and tracking.get("use_luna") and _RECRUITING_HINT.search(subject):
            try:
                from .luna import classify_email
                stage = classify_email(subject, body, settings)
                confidence = 0.6 if stage else 0.0
            except Exception:  # noqa: BLE001 - optional second opinion
                stage = None
        if stage is None:
            if key not in seen and key not in known:
                set_aside.append(key)
                if len(set_aside) >= 100:
                    db.mark_mail_seen(set_aside, rules)
                    seen.update(set_aside)
                    set_aside.clear()
            continue
        report.recruiting += 1
        try:
            received = parsedate_to_datetime(str(message.get("Date"))).astimezone(timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError, OverflowError):
            received = datetime.now(timezone.utc).isoformat(timespec="seconds")
        app = match_application(sender, subject, body, applications)
        app_id = int(app["id"]) if app else None
        if stage == "rejected" and app_id is not None and message_id:
            rejections.append((uid, message_id, app_id))
        # A message already read the same way is done. One read differently, by a
        # better rule or because its application is now tracked, is applied again.
        if known.get(key) == (stage, app_id):
            continue
        previous = known.get(key)
        db.record_mail_event(message_key=key, received_at=received, sender=sender, subject=subject, stage=stage,
                             application_id=app_id, confidence=confidence, message_id=message_id)
        known[key] = (stage, app_id)
        # A message read differently no longer vouches for the stage it set before.
        restaged = db.restage_from_mail(int(previous[1])) if previous and previous[1] is not None else None
        if restaged:
            for tracked in applications:
                if int(tracked["id"]) == int(previous[1]):
                    tracked["stage"] = restaged
                    db.add_event(f"Inbox correction: {tracked.get('company')} · {tracked.get('title')} → "
                                 f"{tracked['stage']}", "info", tracked.get("job_id"))
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


def _archive_rejections(report: SyncReport, rejections: list[tuple[bytes, str, int]], archiver) -> None:
    """Archive rejection emails whose application the Tracker shows as Rejected (by email or by hand)."""
    if archiver is None:
        return
    rejected = {int(row["id"]) for row in db.list_tracker() if row.get("stage") == "rejected"}
    items = [(uid, message_id) for uid, message_id, app_id in rejections if app_id in rejected]
    if not items:
        return
    try:
        report.archived = int(archiver(items))
    except Exception as error:  # noqa: BLE001 - archiving is a convenience; tracking already succeeded
        report.archive_error = str(error)[:240]
        db.add_event(f"Could not archive rejection emails: {report.archive_error}", "warning")
        return
    if report.archived:
        db.add_event(f"Archived {report.archived} rejection email{'s' if report.archived != 1 else ''} "
                     f"(Gmail label {ARCHIVE_LABEL})", "info")
