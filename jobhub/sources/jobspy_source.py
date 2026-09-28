"""LinkedIn / Indeed / ZipRecruiter / Google Jobs / Glassdoor via python-jobspy (no LLM, no login).

JobSpy is the best-maintained open-source scraper for the big boards. LinkedIn rate-limits aggressively;
Indeed is the most reliable. Each search term x site is one request batch.
"""
from __future__ import annotations

import math
import time

from ..models import Job
from .base import SourceJobs, iso, title_relevant


MAX_SEARCHES_PER_SCAN = 8


def _num(v):
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        return int(float(v))
    except Exception:  # noqa: BLE001
        return None


def _s(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v)


def _search_terms(cfg: dict, settings: dict) -> list[str]:
    """Use explicit board queries, or the configured role targets as a fallback."""
    configured = cfg.get("search_terms") or settings.get("target_titles", [])
    terms: list[str] = []
    seen: set[str] = set()
    for value in configured:
        term = str(value).strip()
        key = term.casefold()
        if term and key not in seen:
            terms.append(term)
            seen.add(key)
    return terms


def _search_locations(cfg: dict, settings: dict) -> list[str]:
    """Use an explicit override or every distinct saved target locality."""
    location = str(cfg.get("location", "")).strip()
    if location:
        return [location]
    locations: list[str] = []
    seen: set[str] = set()
    for value in settings.get("preferred_locations", []):
        candidate = str(value).strip()
        key = candidate.casefold()
        if candidate and key not in {"remote", "united states", "usa", "us"} and key not in seen:
            locations.append(candidate)
            seen.add(key)
    return locations or ["United States"]


def _search_plan(cfg: dict, settings: dict, hour: int | None = None) -> list[tuple[str, str]]:
    """Rotate a bounded batch through all saved title/location combinations."""
    pairs = [(term, location) for term in _search_terms(cfg, settings)
             for location in _search_locations(cfg, settings)]
    if len(pairs) <= MAX_SEARCHES_PER_SCAN:
        return pairs
    hour = int(time.time() // 3600) if hour is None else hour
    offset = (hour * MAX_SEARCHES_PER_SCAN) % len(pairs)
    return [pairs[(offset + index) % len(pairs)] for index in range(MAX_SEARCHES_PER_SCAN)]


def fetch(settings: dict) -> list[Job]:
    from jobspy import scrape_jobs  # heavy import; keep lazy

    cfg = settings.get("jobspy", {})
    out = SourceJobs()
    seen: set[str] = set()
    searches = _search_plan(cfg, settings)
    if not searches:
        return out
    for term, location in searches:
        try:
            df = scrape_jobs(
                site_name=cfg.get("sites", ["indeed"]), search_term=term,
                location=location, results_wanted=int(cfg.get("results_wanted", 20)),
                hours_old=int(cfg.get("hours_old", 72)), country_indeed=cfg.get("country_indeed", "usa"), description_format="markdown",
                linkedin_fetch_description=False, verbose=0,
                google_search_term=f"{term} jobs in {location} since yesterday",
            )
        except Exception as exc:  # noqa: BLE001
            out.warnings.append(f"{term} in {location}: {type(exc).__name__}: {str(exc)[:160]}")
            continue
        if df is None or len(df) == 0:
            continue
        for _, r in df.iterrows():
            url = _s(r.get("job_url_direct")) or _s(r.get("job_url"))
            if not url or url in seen:
                continue
            seen.add(url)
            title = _s(r.get("title"))
            if not title_relevant(title, settings):
                continue
            site = _s(r.get("site"))
            loc = _s(r.get("location"))
            is_remote = bool(r.get("is_remote")) if _s(r.get("is_remote")) not in ("", "nan") else "remote" in loc.lower()
            smin, smax = _num(r.get("min_amount")), _num(r.get("max_amount"))
            interval = _s(r.get("interval")).lower()
            factor = 2080 if interval == "hourly" else 12 if interval == "monthly" else 1
            smin, smax = (smin * factor if smin else None), (smax * factor if smax else None)
            if _s(r.get("currency")).upper() not in ("", "USD"):
                smin = smax = None
            ats = "linkedin" if site == "linkedin" else ("indeed" if site == "indeed" else "unknown")
            u = url.lower()
            for k in ("greenhouse", "lever", "ashby", "workday"):
                if k in u: ats = k
            out.append(Job(
                source=f"jobspy:{site}", ats=ats, company=_s(r.get("company")), title=title, location=loc,
                remote=is_remote, salary_min=smin, salary_max=smax, url=url,
                apply_url=_s(r.get("job_url_direct")) or url, description=_s(r.get("description"))[:15000],
                posted_at=iso(_s(r.get("date_posted")) or None),
                raw={"site": site, "job_type": _s(r.get("job_type")), "company_url": _s(r.get("company_url")),
                     "search_term": term, "search_location": location, "job_url": _s(r.get("job_url"))},
            ))
    if len(out.warnings) == len(searches):
        raise RuntimeError("All JobSpy searches failed: " + "; ".join(out.warnings))
    return out
