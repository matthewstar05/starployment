"""JARVIS policy. Saved preferences stay intact while background limits are reduced.

Modes: OFF, NEW (last 24 hours, saved answers), ALL (any still-listed posting, AI
Autofill, gentle on the Mac), and ALL+ (``all_plus``: ALL with a wider net and the
saved, unhalved concurrency, for overnight runs; it still backs off before the Mac
runs out of memory or CPU).
"""
from __future__ import annotations

import re
import time

from . import families
from .models import Job, posted_epoch

MODES = ("off", "new", "all", "all_plus")
LABELS = {"off": "OFF", "new": "NEW", "all": "ALL", "all_plus": "ALL+"}
AUTOMATIC_ATS = ("greenhouse", "lever", "ashby", "workable", "rippling")


# JARVIS waits for headroom, but never forever: on a Mac that stays loaded (agents,
# builds, a browser), work goes ahead after this long, at its already reduced concurrency.
MAX_DEFER_SECONDS = 600
# ALL+ waits only while the Mac is close to its limits, and not as long.
PLUS_DEFER_SECONDS = 180
PLUS_LOAD_PER_CORE = 1.1
PLUS_MIN_FREE_MEMORY = 10
# A busy spell ends only after this long with headroom. Load hovering near the limit flips
# the reading every few seconds; without this, each flicker restarted the deferral clock,
# so JARVIS never reached its "go ahead anyway" point and cut most overnight cycles short.
HEADROOM_RESET_SECONDS = 60
# While a cycle is under way, how often to look again for headroom.
WAIT_POLL_SECONDS = 2.0
_deferred: dict[str, float] = {}
_headroom: dict[str, float] = {}


def label(mode: str) -> str:
    return LABELS.get(mode, "OFF")


def mode_of(settings: dict) -> str:
    mode = settings.get("jarvis_mode", "off")
    return mode if mode in MODES else "off"


def active(settings: dict) -> bool:
    return mode_of(settings) != "off"


def sweeping(settings_or_mode) -> bool:
    """ALL and ALL+: prepare, AI Autofill, and send postings of any age that are still listed."""
    mode = settings_or_mode if isinstance(settings_or_mode, str) else mode_of(settings_or_mode)
    return mode in ("all", "all_plus")


def plus(settings: dict) -> bool:
    return mode_of(settings) == "all_plus"


def automatic(settings: dict) -> bool:
    return active(settings) or bool(settings.get("auto_apply"))


def hold(kind: str, settings: dict | None = None) -> bool:
    """True when ``kind`` of work ("scan", "apply", "send") should wait because the computer is busy.

    Once the machine has been busy for ``MAX_DEFER_SECONDS`` in a row, work proceeds anyway,
    as the governor's "one task always runs" rule intends; ``HEADROOM_RESET_SECONDS`` of
    headroom resets the clock (a brief dip below the limit does not). ALL+
    waits only while the Mac is near its limits, for at most ``PLUS_DEFER_SECONDS``.
    """
    from . import governor
    boost = plus(settings) if settings is not None else False
    busy = (governor.busy(load_per_core=PLUS_LOAD_PER_CORE, min_free=PLUS_MIN_FREE_MEMORY) if boost
            else governor.busy())
    now = time.monotonic()
    if not busy:
        since = _headroom.setdefault(kind, now)
        if now - since >= HEADROOM_RESET_SECONDS:
            _deferred.pop(kind, None)
        return False
    since = _headroom.pop(kind, None)
    if since is not None and now - since >= HEADROOM_RESET_SECONDS:
        _deferred.pop(kind, None)          # the last busy spell really ended: a new one starts now
    first = _deferred.setdefault(kind, now)
    return now - first < (PLUS_DEFER_SECONDS if boost else MAX_DEFER_SECONDS)


def parallel(settings: dict, key: str, default: int, ceiling: int = 32) -> int:
    """NEW and ALL halve saved concurrency (minimum one); ALL+ and OFF use it as saved."""
    value = max(1, min(int(settings.get(key, default) or 1), ceiling))
    return max(1, value // 2) if active(settings) and not plus(settings) else value


def interval_minutes(settings: dict) -> float:
    """The scan interval. NEW and ALL keep the saved one: a scan of every board takes several
    minutes, so halving it kept the Mac scanning almost nonstop. ALL+ halves it (minimum five)."""
    value = max(5, int(settings.get("scan_interval_min", 60)))
    return max(5, value / 2) if plus(settings) else value


# Reasons ALL+ looks past, since it only considers postings a scan found still listed lately:
# an old posting that is still on its board is still hiring.
PLUS_IGNORES = frozenset({"Posting date is over 180 days old"})


def workday_on(settings: dict) -> bool:
    """Workday sends unattended only with both of the candidate's switches on (AGENTS.md rule 14):
    Manage Workday sign-ins (an account per employer, password in the vault) and Send Workday
    applications."""
    workday = settings.get("workday") or {}
    return bool(workday.get("send") and workday.get("create_accounts"))


def eligible(job: Job, settings: dict) -> bool:
    from .priority import held_from_automation
    allowed = job.ats in AUTOMATIC_ATS or (job.ats == "workday" and workday_on(settings))
    if not allowed or job.score < float(settings.get("min_score_to_apply", 60)):
        return False
    if held_from_automation(job, settings.get("priority_companies") or []):
        return False
    if job.score_json.get("auto_apply_eligible") is True:
        return True
    reasons = job.score_json.get("review_reasons")
    return bool(plus(settings) and reasons and set(reasons) <= PLUS_IGNORES)


def rank(jobs: list[Job], mode: str, now: float | None = None) -> list[Job]:
    """NEW uses the last 24 hours; ALL and ALL+ alternate recency and match, without duplicates."""
    now = time.time() if now is None else now
    def posted(job):
        return posted_epoch(job.posted_at) or posted_epoch(job.discovered_at) or 0
    newest = sorted(jobs, key=lambda job: (posted(job), job.score), reverse=True)
    if mode == "new":
        return [job for job in newest if posted(job) >= now - 86400]
    best = sorted(jobs, key=lambda job: (job.score, posted(job)), reverse=True)
    result, seen = [], set()
    for pair in zip(newest, best):
        for job in pair:
            if job.id not in seen:
                seen.add(job.id)
                result.append(job)
    return result


# ALL considers postings of any age, but only ones a scan found still listed lately:
# an old posting that is still on its board is still hiring.
LISTED_WITHIN_DAYS = 3


def still_listed(jobs: list[Job], days: float = LISTED_WITHIN_DAYS, now: float | None = None) -> list[Job]:
    from . import db
    now = time.time() if now is None else now
    seen = db.last_seen(job.id for job in jobs)
    return [job for job in jobs if seen.get(job.id, 0) >= now - days * 86400]


def company_limit(settings: dict) -> int:
    """Attempts per employer in ``company_cap.days``; ALL+ allows twice the saved count."""
    limit = int((settings.get("company_cap") or {}).get("count") or 0)
    return limit * 2 if limit and plus(settings) else limit


def diversify(jobs: list[Job], settings: dict, attempts: dict[str, int] | None = None) -> list[Job]:
    """Drop employers at their rolling cap, then alternate role families.

    ``attempts`` counts recent attempts by normalized company (db.recent_company_attempts).
    The order inside each family is kept, so the ranking still decides who goes first.
    """
    from .priority import normalize_company
    cap = settings.get("company_cap") or {}
    limit = company_limit(settings)
    if limit:
        if attempts is None:
            from . import db
            attempts = db.recent_company_attempts(float(cap.get("days") or 30))
        jobs = [job for job in jobs if attempts.get(normalize_company(job.company), 0) < limit]
    if settings.get("mix_role_families", True):
        jobs = families.interleave(jobs, lambda job: families.family(job.title))
    return jobs


# A posting JARVIS had to leave for the candidate (a required upload, an answer it may not
# invent, a posting it could not verify) is set aside instead of being retried every cycle.
# It comes back once its draft changes (the candidate answered, or saved answers changed)
# or when its time runs out: days for a reason only the candidate can fix, hours for one
# that may pass on its own (a busy Mac, an AI call that timed out).
SET_ASIDE_SECONDS = {"candidate": 3 * 86400, "posting": 86400, "transient": 2 * 3600}


def set_aside_seconds(kind: str) -> float:
    return SET_ASIDE_SECONDS.get(kind, SET_ASIDE_SECONDS["transient"])


def set_aside(job_ids, now: float | None = None) -> set[str]:
    """The jobs JARVIS set aside whose reason still stands."""
    from . import db
    now = time.time() if now is None else now
    holds = db.jarvis_holds(job_ids)
    if not holds:
        return set()
    drafts = db.draft_stamps(holds)
    return {job_id for job_id, (until, stamp) in holds.items()
            if until > now and drafts.get(job_id, "") == stamp}


# ---------------------------------------------------------------- one role, one application
# The same role is often posted more than once ("Forward Deployed Engineer", "…, Early Career",
# "…, 2027 Graduate"). Automatic runs treat those as one vacancy: these words name a hiring
# track or level, not a different job.
_STAGE_WORDS = re.compile(r"\b(?:early|career|new|grads?|graduates?|university|college|campus|entry|level|junior|jr|"
                          r"associate level|class|of|20\d\d|i{1,3}|iv|[1-4]|remote|hybrid|on.?site|us|usa|"
                          r"united states|full.?time|program)\b", re.I)


def role_key(title: str) -> str:
    """A title with its hiring-track words removed, for spotting the same role posted twice."""
    text = re.sub(r"[()\[\],/|–—:-]+", " ", (title or "").casefold())
    text = _STAGE_WORDS.sub(" ", text)
    return " ".join(sorted(set(text.split())))


def not_repeated(jobs: list[Job]) -> list[Job]:
    """Drop roles automatic runs must leave alone: one already applied to under another posting,
    and every role at an employer where an application has reached an assessment, interview, or
    offer (a second application there can look careless). The candidate may still apply by hand."""
    from . import db
    from .priority import normalize_company
    roles, engaged = db.automation_history()
    # Within one run the one-employer-per-cycle rule already keeps a role from going twice, and a
    # posting that cannot be sent (a missing answer) must still let its twin take the turn.
    return [job for job in jobs
            if normalize_company(job.company) not in engaged
            and (normalize_company(job.company), role_key(job.title)) not in roles]
