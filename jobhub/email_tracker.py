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
from email.message import EmailMessage
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
# Assessment platforms, by the names and domains their invitations use. "Suited" and
# "Litmus" are also ordinary words, so only their domains count.
_PLATFORMS = (r"\b(?:hackerrank|codesignal|codility|hirevue|karat|testgorilla|coderpad|qualified\.io|coderbyte|"
              r"litmus\.build|criteriacorp|shl\b|pymetrics|harver\b|mettl|imocha|hackerearth|vervoe|codescreen|"
              r"devskiller|cappfinity|arctic shores|plum\.io|wearesuited|suited\.com|modern ?hire|"
              r"spark ?hire|wonderlic|canditech|alva labs|equalture)")
_SIGNALS: dict[str, tuple[tuple[str, float], ...]] = {
    "offer": ((r"\boffer letter\b", 4), (r"pleased to (?:extend|offer)", 4), (r"extend (?:you )?an offer", 4),
              (r"\bverbal offer\b", 3), (r"offer of employment", 4)),
    "rejected": ((r"unfortunately", 2.2), (r"not (?:to )?(?:be )?(?:move|moving|proceed|proceeding) forward", 3.5),
                 (r"(?:pursue|proceed with|move forward|moving forward)[^.]{0,40}\bwith (?:other|another) (?:candidate|applicant)",
                  3.5), (r"(?:other|another) candidates? (?:has been|have been|was|were) (?:offered|selected|chosen)", 3.5),
                 (r"candidates? whose (?:background|experience|skills|qualifications)[^.]{0,80}(?:better|more closely)", 3.5),
                 (r"not (?:been )?selected", 3), (r"regret to inform", 3.5), (r"position has been filled", 3),
                 (r"no longer (?:under )?consider", 3), (r"will not be (?:moving|proceeding|advancing)", 3.5),
                 (r"other applicants whose", 3), (r"(?:do|does) not qualify", 3), (r"decided to (?:pursue|go with|move ahead with) other", 3.5), (r"unable to (?:offer|move forward)", 3),
                 (r"not (?:the|a) (?:right |good |strong )?(?:fit|match) (?:for|at) this time|not the right fit", 3),
                 (r"(?:decided|chosen) not to (?:move|proceed|advance|continue)", 3.5),
                 (r"won't be (?:moving|proceeding|advancing)", 3.5)),
    "interview": ((r"\binterview(?:s|ing)?\b", 1.6), (r"schedule (?:a|an|your|some) (?:time|call|interview|chat)", 3),
                  (r"phone screen", 3), (r"(?:recruiter|technical) (?:call|screen)", 2.5), (r"\bonsite\b|on-site interview", 2.5),
                  (r"\bsuperday\b", 3.5), (r"calendly\.com|goodtime\.io|availability for", 2.5),
                  (r"invite you to (?:a |an )?(?:interview|call|chat|meet)", 3.5), (r"next round", 2)),
    "assessment": ((r"online (?:[\w-]+ ){0,2}(?:assessments?|tests?|challenges?)|\boa\b", 3),
                   (r"coding (?:challenge|assessment|test)", 3.2), (_PLATFORMS, 3.5), (r"take[- ]home", 3),
                   (r"technical (?:assessment|challenge|exercise)", 3),
                   # "complete your assessments", "take two short assessments", "start the HackerRank test"
                   (r"(?:complete|take|start|begin|access|finish) (?:the |this |your |my |our |an? |two |both |these )?"
                    r"(?:(?!for |a )[\w-]+ ){0,3}(?:assessments?|challenges?|tests?)\b", 3),
                   (r"invit\w* (?:by [^.\n]{1,40} )?to (?:complete|take|start)", 3),
                   (r"assessments? (?:invitation|link|request|expire|deadline|due|reminder)|(?:pre-recruiting|"
                    r"pre-employment|pre-screening|cognitive|aptitude|psychometric) (?:assessment|test)", 3)),
    "applied": ((r"thank you for (?:applying|your application|your interest)", 2.4),
                (r"(?:application|submission) (?:has been |was )?(?:received|submitted)", 2.6),
                (r"received your application", 3), (r"thanks for applying", 2.8)),
}
_PRIORITY = ("offer", "rejected", "interview", "assessment", "applied")
# Subjects and senders worth downloading. Reading a body costs no tokens, so this errs wide.
_RECRUITING_HINT = re.compile(
    r"application|applying|applied|candida|recruit|interview|assessment|position|role|opportunit|offer|hiring|talent|"
    r"your interest|update|careers|greenhouse|lever\.co|ashbyhq|workday|next step|invit|challenge|"
    r"action required|reminder|deadline|expir|" + _PLATFORMS, re.I)
# "If you move forward, the next stage is an assessment" describes the process; it is not an invitation.
_CONDITIONAL_CLAUSE = re.compile(r"\b(?:if|should|whether|in case|once|when)\b[^.!?\n]{0,160}")
# What an actual invitation asks the candidate to do. An acknowledgement ("thanks for applying")
# moves an application forward only with one of these, not by describing later stages.
# Describing an assessment that may come later ("you will receive a HackerRank challenge",
# "our process includes a coding test") is not an invitation to one. Read only for the
# assessment stage: "you'll receive an offer letter" is still an offer.
_PROSPECTIVE_CLAUSE = re.compile(
    r"\b(?:you(?:'ll| will| may| might| could| can expect to| would)(?! not)(?!n't)|candidates (?:will|may|who)|"
    r"(?:may|might|will) be (?:asked|invited|required|sent)|"
    r"(?:our|the|this) (?:recruiting|hiring|interview|selection|application) process (?:includes|consists|involves|"
    r"may include|typically|usually|will)|(?:roles|positions) (?:include|typically include|may include)|"
    r"next steps? (?:will |may |could |might )?(?:include|involve|consist))[^.!?\n]{0,200}|"
    # Roblox: "here's a quick overview of what to expect next:\n\n* Complete our Assessments: If ..."
    # runs past the line break into its bulleted list.
    r"(?:what to expect|overview of (?:our|the) (?:process|next steps))[^.!?]{0,200}")
_INVITATION = {
    # A platform's name alone is not an invitation: Akuna's acknowledgements (2026-10) name
    # HackerRank while describing their process. An invitation links to, or invites to, the test.
    "assessment": re.compile(_PLATFORMS + r"[^.\n]{0,60}(?:invit|link|click|deadline|expire|due)|"
                             r"(?:invit|link|click)[^.\n]{0,60}" + _PLATFORMS + r"|"
                             r"(?:complete|start|take|begin|access)\s+(?:the|this|your|an?|our)\s[^.\n]{0,40}"
                             r"(?:assessment|challenge|test|exercise)|(?:assessment|challenge) link|"
                             r"invited? (?:you )?to (?:complete|take)|"
                             # Akuna: "happy to move forward with the process ..., which is an online coding assessment"
                             r"(?:move|moving) forward[^.\n]{0,100}(?:assessment|challenge)|"
                             r"next step[^.\n]{0,80}(?:assessment|challenge)"),
    "interview": re.compile(r"schedule (?:a|an|your|some) (?:time|call|interview|chat)|phone screen|calendly\.com|"
                            r"goodtime\.io|availability for|invite you to|\bsuperday\b|book (?:a|your) (?:time|slot)"),
    "offer": re.compile(r"offer letter|pleased to (?:extend|offer)|extend (?:you )?an offer|offer of employment"),
}


# An offer spoken of as a condition, not made: Amcor's Workday account email (2026-11-04) says a
# survey "needs to be issued to candidates prior to any offer of employment".
_HYPOTHETICAL_OFFER = re.compile(
    r"(?:prior to|before|ahead of|in advance of|contingent (?:up)?on|conditional (?:up)?on|pending|"
    r"(?:any|all) (?:future )?)[^.!?\n]{0,30}\boffers? (?:of employment|letter)s?|\bany offers? of employment")
# Account mail from an ATS (verify, activate, reset, sign-in codes) is never about an application's
# stage, whatever its footer mentions.
_ACCOUNT_MAIL = re.compile(r"verify your (?:candidate |email |e-mail )?(?:account|address|email)|"
                           r"(?:account|email) (?:verification|activation)|activate your (?:candidate )?account|"
                           r"(?:reset|change) your password|password reset|(?:sign[- ]?in|one[- ]time|verification) code",
                           re.I)


def classify(subject: str, body: str) -> tuple[str | None, float]:
    """Return ``(stage, confidence)`` for a recruiting email, or ``(None, 0)``."""
    if _ACCOUNT_MAIL.search(subject or ""):
        return None, 0.0
    text = _HYPOTHETICAL_OFFER.sub(" ", f"{subject}\n{body}".lower().replace("\u2019", "'"))
    # Conditional clauses ("If we decide not to move forward, we'll let you know") are not news;
    # only the acknowledgement itself is read from the full text.
    settled = _CONDITIONAL_CLAUSE.sub(" ", text)
    current = _PROSPECTIVE_CLAUSE.sub(" ", settled)
    source = {"applied": text, "assessment": current}
    scores = {stage: sum(weight for pattern, weight in rules if re.search(pattern, source.get(stage, settled)))
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
            if not invitation.search(current if stage == "assessment" else settled):
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


# Mailbox words, not employers: "DO-NOT-REPLY@criteriacorp.com" once matched a company named "Reply".
_SENDER_NOISE = re.compile(r"\b(?:do[-_. ]?not[-_. ]?reply|no[-_. ]?reply|noreply|reply|notifications?|mailer|"
                           r"hiring team|talent team|recruiting team)\b")
_TITLE_WORDS = re.compile(r"[a-z0-9+#]+")
_TITLE_FILLER = frozenset("and of the for a an in at to with new grad senior junior i ii iii iv 2026 2027".split())


def _same_employer(a: str, b: str) -> bool:
    """One tracked employer under two names ("Akuna Capital", "Akuna Capital University")."""
    a, b = _norm_company(a), _norm_company(b)
    return bool(a and b) and (a == b or a.startswith(b + " ") or b.startswith(a + " ")
                              or a.replace(" ", "") == b.replace(" ", ""))


def _title_overlap(title: str, text: str) -> float:
    """Share of a title's distinctive words that appear in the message."""
    words = {w for w in _TITLE_WORDS.findall((title or "").lower()) if w not in _TITLE_FILLER}
    if not words:
        return 0.0
    present = set(_TITLE_WORDS.findall(text))
    return len(words & present) / len(words)


def _sent_at(app: dict) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(app.get("finished_at") or app.get("started_at") or ""))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _latest_before(apps: list[dict], received: datetime | None) -> dict:
    """The application sent most recently before the email arrived.

    Plain "newest" moved an email every time another application to that employer
    was sent later (Netic's assessment invitations arrive seconds after each
    application), so stages flipped back to Applied on every check."""
    dated = [(stamp, app) for app in apps if (stamp := _sent_at(app)) is not None]
    if received is not None and dated:
        before = [(stamp, app) for stamp, app in dated if stamp <= received]
        if before:
            return max(before, key=lambda item: item[0])[1]
        return min(dated, key=lambda item: item[0])[1]   # clock skew: the earliest one
    return max(apps, key=lambda app: str(app.get("finished_at") or app.get("started_at") or ""))


def match_application(sender: str, subject: str, body: str, applications: list[dict],
                      received: datetime | None = None) -> dict | None:
    """The single tracked application this message is about, or None when unsure."""
    sender_l, subject_l, body_l = sender.lower(), subject.lower(), body[:6000].lower()
    domain = sender_l.rpartition("@")[-1]
    domain_labels = set(re.findall(r"[a-z0-9]+", domain.replace("-", "")))
    sender_w, subject_w, body_w = _words(_SENDER_NOISE.sub(" ", sender_l)), _words(subject_l), _words(body_l)
    sender_compact = sender_w.replace(" ", "")
    subject_compact = subject_w.replace(" ", "")
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
        if needle in subject_w or (len(compact) >= 6 and compact in subject_compact):   # "Eight Sleep" for "Eightsleep"
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
    # Several applications at the same employer: prefer the role the message names, else the newest.
    same = [app for score, app in ranked if _same_employer(app.get("company") or "", best.get("company") or "")]
    if len(same) > 1:
        titled = [app for app in same if str(app.get("title") or "").lower() in f"{subject_l} {body_l}"]
        if len(titled) == 1:
            return titled[0]
        text = f"{subject_l} {body_l}"
        overlaps = sorted(((_title_overlap(str(app.get("title") or ""), text), app) for app in same),
                          key=lambda item: -item[0])
        if overlaps[0][0] >= 0.75 and (len(overlaps) == 1 or overlaps[1][0] < overlaps[0][0]):
            return overlaps[0][1]
        return _latest_before(same, received)
    others = [score for score, app in ranked[1:] if not _same_employer(app.get("company") or "", best.get("company") or "")]
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
# (host, user, UIDVALIDITY) -> {uid: (message key, looked like recruiting mail)}; see Mailbox._header_cache.
_HEADERS: dict[tuple, dict[bytes, tuple[str, bool]]] = {}
_RECONNECTS = 3
_CONNECTION_ERRORS = (imaplib.IMAP4.abort, OSError)   # includes timeouts, SSL, DNS, and broken pipes
# Bump when ``classify``, ``_RECRUITING_HINT`` or the Luna reading changes, so mail
# already read and set aside (``db.mark_mail_seen``) is read again under the new rules.
RULES_VERSION = 4
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
                cache = self._header_cache(mailbox)
                while done < len(uids):
                    chunk = uids[done:done + _HEADER_BATCH]

                    def settled(uid: bytes) -> bool:
                        known = cache.get(uid)
                        return known is not None and (not known[1] or bool(skip and skip(known[0])))
                    wanted = [uid for uid in chunk if not settled(uid)]
                    headers: dict[bytes, bytes] = {}
                    if wanted:
                        result, rows = mailbox.uid("FETCH", b",".join(wanted),
                                                   "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
                        for item in rows or []:
                            if isinstance(item, tuple) and (found := re.search(rb"\bUID (\d+)", item[0] or b"")):
                                headers[found.group(1)] = item[1] or b""
                    for uid in chunk:
                        if uid not in headers and settled(uid):
                            done += 1
                            yield uid, EmailMessage(), None   # read on an earlier check: nothing new
                            continue
                        message = BytesParser(policy=policy.default).parsebytes(headers.get(uid, b""))
                        body = None
                        key = _message_key(message, uid)
                        hinted = bool(_RECRUITING_HINT.search(f"{message.get('Subject', '')} {message.get('From', '')}"))
                        if uid in headers:
                            cache[uid] = (key, hinted)
                        if hinted and not (skip and skip(key)):
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


    def _header_cache(self, mailbox) -> dict:
        """Each message's key and whether it looked like recruiting mail, by UID, for this
        mailbox. A UID names the same message for as long as the folder's UIDVALIDITY stays
        the same, so a later check skips even the headers of mail it has already settled."""
        try:
            validity = (mailbox.response("UIDVALIDITY")[1] or [None])[0]
        except Exception:  # noqa: BLE001 - a server that does not say: no cache
            validity = None
        if not validity:
            return {}
        return _HEADERS.setdefault((self.host, self.username, bytes(validity)), {})


def test_mailbox(verification_settings: dict) -> tuple[bool, str]:
    try:
        mailbox = Mailbox(verification_settings).open()
        mailbox.logout()
        return True, "Connected. Starployment can read your inbox (read-only)."
    except imaplib.IMAP4.error as error:
        return False, f"The mail server rejected the sign-in. Check the address and app password. ({str(error)[:120]})"
    except Exception as error:  # noqa: BLE001
        return False, str(error)[:240]


def sync(settings: dict | None = None, *, messages=None, archiver=None, luna_now: bool = False) -> SyncReport:
    """Classify recent mail and advance matching applications.

    With ``email_tracking.archive_rejections`` on, a rejection is archived once it
    names one tracked application whose stage is Rejected. ``messages`` and
    ``archiver`` are for tests. Luna reads unsettled emails in full batches, or at once with
    ``luna_now`` (a check the candidate asked for)."""
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
    # Mail read under these rules is not downloaded again: set-aside mail, and recorded mail
    # already linked to an application (it stays with it, ``_apply``). Recorded mail no
    # application matched yet is read again, since its application may be tracked by now; so is
    # a rejection still waiting to be archived.
    archiving = bool(tracking.get("archive_rejections"))

    def settled(key: str) -> bool:
        if key not in seen:
            return False
        if key not in known:
            return True
        stage, app_id = known[key]
        return app_id is not None and not (archiving and stage == "rejected")
    source = messages if mailbox is None else mailbox.recent(days, skip=settled)
    rejections: list[tuple[bytes, str, int]] = []   # (uid, Message-ID, application id) still in the Inbox
    set_aside: list[str] = []
    try:
        _read(source, report, tracking, settings, applications, known, without_id, rejections, seen, set_aside, rules,
              luna_now)
    finally:
        db.mark_mail_seen(set_aside, rules)   # keep progress even when the connection drops
    if tracking.get("archive_rejections") and rejections:
        _archive_rejections(report, rejections, archiver or (mailbox.archive if mailbox else None))
    return report


# Luna reads only what the rules cannot settle, many emails per call (each agy call
# carries ~8k tokens of its own before the first email), and never the same email twice.
LUNA_BATCH = 20
# Scheduled checks wait for a full batch, but never longer than this; a manual check asks at once.
LUNA_WAIT = timedelta(hours=4)
# Phrases that ask the candidate to do something or close the application. Words alone fire on
# company slogans ("national security challenges") and on lists of platform links, so links are
# removed first and only these phrases count (2026-10 inbox: 45 of 1,357 recruiting emails).
_LATER_STAGE_CUE = re.compile(
    r"(?:complete|take|start|begin|finish|access)\b[^.\n]{0,60}\b(?:assessments?|challenge|tests?)\b|"
    r"assessments? (?:link|invitation)|invit\w* (?:you )?to (?:complete|take|interview|schedule|meet|a |an )|"
    r"schedul\w* (?:a|an|your|some)\b|your availability|(?:decided|chosen) (?:not|to (?:move|pursue|proceed))|"
    r"not (?:be )?(?:moving|proceeding|selected|advancing)|other candidates|not (?:the right|a) (?:fit|match)", re.I)
_LINK = re.compile(r"\(?\s*https?://\S+\s*\)?")
_APPLICATION_CUE = re.compile(r"your (?:application|candidacy|submission)|applied|applying|assessment|interview|"
                              r"challenge|position|\brole\b|recruit|hiring", re.I)


# Job alerts and digests list other people's openings; "interview" in them is never about the candidate.
_JOB_ALERT_SENDER = re.compile(r"jobalerts?|job-alerts?|jobs-listings@linkedin|jobs-noreply@linkedin|alert@indeed|"
                               r"@match\.indeed|noreply@glassdoor|alerts@ziprecruiter|@notifications\.joinhandshake|"
                               r"jobs@simplify", re.I)


def _needs_luna(stage: str | None, app: dict | None, subject: str, body: str) -> bool:
    """Whether a second reading could change what the rules found."""
    if stage in ("assessment", "interview", "offer"):
        return app is None   # a real step for an employer the rules could not name
    if stage not in (None, "applied"):
        return False
    text = _LINK.sub(" ", f"{subject}\n{body[:3000]}")
    current = _PROSPECTIVE_CLAUSE.sub(" ", _CONDITIONAL_CLAUSE.sub(" ", text.lower()))
    if stage is None and not (_RECRUITING_HINT.search(subject) and _APPLICATION_CUE.search(text)):
        return False
    return bool(_LATER_STAGE_CUE.search(current))


_HINT_FROM = re.compile(r"(?:\bfrom|\bwith|\bat|\bto)\s+([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})")
_HINT_INVITES = re.compile(r"^([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})\s+(?:invites|has invited|would like)")
_SENDER_ROLE = re.compile(r"\b(?:hiring|talent|recruiting|recruitment|careers?|team|early|support|assessments?|"
                          r"university|campus|no-?reply|do-not-reply|the)\b", re.I)


def employer_hint(sender: str, subject: str) -> str:
    """The employer an email seems to come from, for listing it when no application matches."""
    subject = re.sub(r"^\s*(?:\[[^\]]*\]\s*|(?:re|fwd?):\s*)+", "", subject or "", flags=re.I)
    for pattern in (_HINT_INVITES, _HINT_FROM):
        found = pattern.search(subject)
        if found and found.group(1).lower() not in {"the", "your", "our", "a", "an"}:
            return found.group(1).strip(" .!-")
    name, address = (getaddresses([sender or ""]) or [("", "")])[0]
    name = _SENDER_ROLE.sub(" ", name).strip(" -|,\"'")
    if len(name) >= 2 and "@" not in name:
        return " ".join(name.split())
    domain = (address or sender or "").rpartition("@")[-1].split(".")
    labels = [label for label in domain[:-1] if label not in {"mail", "email", "us", "eu", "noreply", "careers", "jobs"}]
    return labels[-1].title() if labels else ""


def _read(source, report, tracking, settings, applications, known, without_id, rejections, seen, set_aside,
          rules, luna_now: bool = False) -> None:
    luna = bool(tracking.get("use_luna")) and _luna_ready(settings)
    asked = db.mail_luna_readings() if luna else {}
    pending: list[dict] = []
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
        if _JOB_ALERT_SENDER.search(sender_header) or _ACCOUNT_MAIL.search(subject):
            if key in known:   # read as an update by older rules
                linked = db.dismiss_mail_event(key)
                if linked is not None:
                    db.restage_from_mail(int(linked))
            continue
        item = {"uid": uid, "key": key, "message": message, "message_id": message_id, "subject": subject,
                "sender": sender, "sender_header": sender_header, "body": body, "stage": stage,
                "confidence": confidence, "company": ""}
        app = match_application(sender, subject, body, applications, _received(message)) if stage else None
        if luna and _needs_luna(stage, app, subject, body):
            if key in asked:
                _merge_luna(item, *asked[key])
            else:
                pending.append(item)
                if stage is None:
                    continue   # not set aside, so the next check reads it again until Luna has
        _apply(item, report, applications, known, rejections, seen, set_aside, rules)
    if pending and not luna_now and len(pending) < LUNA_BATCH:
        oldest = min((_received(item["message"]) for item in pending), default=None)
        if oldest and datetime.now(timezone.utc) - oldest < LUNA_WAIT:
            return   # a few emails wait for a fuller, cheaper batch
    for start in range(0, len(pending), LUNA_BATCH):
        batch = pending[start:start + LUNA_BATCH]
        try:
            from .luna import classify_emails
            readings = classify_emails([{"sender": i["sender_header"], "subject": i["subject"], "body": i["body"]}
                                        for i in batch], settings)
        except Exception:  # noqa: BLE001 - optional second opinion; the rules' reading stands
            readings = [None] * len(batch)
        answered = []
        for item, reading in zip(batch, readings):
            before = item["stage"]
            if reading is not None:
                answered.append((item["key"], reading.get("stage") or "", reading.get("company") or ""))
                _merge_luna(item, reading.get("stage"), reading.get("company"))
            if before is None or item["stage"] != before or item["company"]:
                _apply(item, report, applications, known, rejections, seen, set_aside, rules,
                       counted=before is not None)
        db.save_mail_luna_readings(answered)


def _received(message) -> datetime | None:
    try:
        return parsedate_to_datetime(str(message.get("Date"))).astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _luna_ready(settings: dict) -> bool:
    try:
        from .agents import status
        return status(str((settings.get("agent") or {}).get("backend") or "none")).ready
    except Exception:  # noqa: BLE001
        return False


def _merge_luna(item: dict, stage: str | None, company: str | None) -> None:
    """Luna can name a stage the rules missed or an employer they could not match; it never demotes."""
    if stage in ("assessment", "interview", "offer", "rejected") and item["stage"] in (None, "applied"):
        item["stage"], item["confidence"] = stage, 0.6
    elif stage == "applied" and item["stage"] is None:
        item["stage"], item["confidence"] = stage, 0.5
    item["company"] = (company or "").strip()[:80]


def _apply(item: dict, report, applications, known, rejections, seen, set_aside, rules, counted: bool = False) -> None:
    key, stage, subject, sender, body = item["key"], item["stage"], item["subject"], item["sender"], item["body"]
    message, message_id = item["message"], item["message_id"]
    if stage is None:
        if key not in seen and key not in known:
            set_aside.append(key)
            if len(set_aside) >= 100:
                db.mark_mail_seen(set_aside, rules)
                seen.update(set_aside)
                set_aside.clear()
        return
    report.recruiting += not counted
    received = (_received(message) or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    arrived = _received(message)
    app = match_application(sender, subject, body, applications, arrived)
    if app is None and item["company"]:   # the employer Luna named, read as if the subject said it
        app = match_application(f"{sender} {item['company']}", f"{subject} · {item['company']}", body, applications,
                                arrived)
    previous = known.get(key)
    if previous and previous[1] is not None and (app is None or int(app["id"]) != int(previous[1])):
        # A message stays with the application it was first linked to. Matching again from
        # scratch moved it whenever a posting's employer name or title changed between scans
        # ("Netic" and "Cybernetic Labs"), or another application to that employer was tracked,
        # so stages flipped back and forth on every check.
        linked = next((tracked for tracked in applications if int(tracked["id"]) == int(previous[1])), None)
        app = linked or db.tracked_application(int(previous[1])) or app
    app_id = int(app["id"]) if app else None
    if stage == "rejected" and app_id is not None and message_id:
        rejections.append((item["uid"], message_id, app_id))
    # A message already read the same way is done. One read differently, by a
    # better rule or because its application is now tracked, is applied again.
    if previous == (stage, app_id):
        _settle(key, app_id, seen, set_aside, rules)
        return
    db.record_mail_event(message_key=key, received_at=received, sender=sender, subject=subject, stage=stage,
                         application_id=app_id, confidence=item["confidence"], message_id=message_id,
                         employer=item["company"] or employer_hint(item["sender_header"], subject))
    known[key] = (stage, app_id)
    _settle(key, app_id, seen, set_aside, rules)
    # A message read differently no longer vouches for the stage it set before.
    restaged = db.restage_from_mail(int(previous[1])) if previous and previous[1] is not None else None
    if restaged:
        for tracked in applications:
            if int(tracked["id"]) == int(previous[1]):
                tracked["stage"] = restaged
                db.add_event(f"Inbox correction: {tracked.get('company')} · {tracked.get('title')} → "
                             f"{tracked['stage']}", "info", tracked.get("job_id"))
    if app is None:
        report.unmatched[:] = [u for u in report.unmatched if (u["sender"], u["subject"]) != (sender, subject)]
        report.unmatched.append({"sender": sender, "subject": subject, "stage": stage})
        return
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


def _settle(key: str, app_id: int | None, seen: set, set_aside: list, rules: int) -> None:
    """Mark a message linked to an application as read under these rules, so the next
    check does not download it again (unlinked mail is read again until it matches)."""
    if app_id is None or key in seen:
        return
    seen.add(key)
    set_aside.append(key)


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
