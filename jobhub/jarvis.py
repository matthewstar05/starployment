"""JARVIS policy. Saved preferences stay intact while background limits are reduced."""
from __future__ import annotations

import time

from . import families
from .models import Job, posted_epoch

MODES = ("off", "new", "all")
AUTOMATIC_ATS = ("greenhouse", "lever", "ashby", "workable", "rippling")


# JARVIS waits for headroom, but never forever: on a Mac that stays loaded (agents,
# builds, a browser), work goes ahead after this long, at its already halved concurrency.
MAX_DEFER_SECONDS = 600
_deferred: dict[str, float] = {}


def hold(kind: str) -> bool:
    """True when ``kind`` of work ("scan", "apply", "send") should wait because the computer is busy.

    Once the machine has been busy for ``MAX_DEFER_SECONDS`` in a row, work proceeds anyway,
    as the governor's "one task always runs" rule intends; headroom resets the clock.
    """
    from . import governor
    if not governor.busy():
        _deferred.pop(kind, None)
        return False
    first = _deferred.setdefault(kind, time.monotonic())
    return time.monotonic() - first < MAX_DEFER_SECONDS


def active(settings: dict) -> bool:
    return settings.get("jarvis_mode", "off") in ("new", "all")


def automatic(settings: dict) -> bool:
    return active(settings) or bool(settings.get("auto_apply"))


def parallel(settings: dict, key: str, default: int, ceiling: int = 32) -> int:
    value = max(1, min(int(settings.get(key, default) or 1), ceiling))
    return max(1, value // 2) if active(settings) else value


def interval_minutes(settings: dict) -> float:
    value = max(5, int(settings.get("scan_interval_min", 60)))
    return max(5, value / 2) if active(settings) else value


def eligible(job: Job, settings: dict) -> bool:
    from .priority import held_from_automation
    return (job.ats in AUTOMATIC_ATS and job.score >= float(settings.get("min_score_to_apply", 60))
            and job.score_json.get("auto_apply_eligible") is True
            and not held_from_automation(job, settings.get("priority_companies") or []))


def rank(jobs: list[Job], mode: str, now: float | None = None) -> list[Job]:
    """NEW uses the last 24 hours; ALL alternates recency and match, without duplicates."""
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


def diversify(jobs: list[Job], settings: dict, attempts: dict[str, int] | None = None) -> list[Job]:
    """Drop employers at their rolling cap, then alternate role families.

    ``attempts`` counts recent attempts by normalized company (db.recent_company_attempts).
    The order inside each family is kept, so the ranking still decides who goes first.
    """
    from .priority import normalize_company
    cap = settings.get("company_cap") or {}
    limit = int(cap.get("count") or 0)
    if limit:
        if attempts is None:
            from . import db
            attempts = db.recent_company_attempts(float(cap.get("days") or 30))
        jobs = [job for job in jobs if attempts.get(normalize_company(job.company), 0) < limit]
    if settings.get("mix_role_families", True):
        jobs = families.interleave(jobs, lambda job: families.family(job.title))
    return jobs
