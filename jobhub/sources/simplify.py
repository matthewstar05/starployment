"""SimplifyJobs GitHub listings (New-Grad-Positions and, optionally, Summer internships). Free JSON, no LLM."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from ..models import Job
from .base import client, iso, title_relevant

NEWGRAD = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"
INTERNS = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"


def _ats_from_url(url: str) -> str:
    h = urlparse(url).netloc.lower()
    if "greenhouse.io" in h: return "greenhouse"
    if "lever.co" in h: return "lever"
    if "ashbyhq.com" in h: return "ashby"
    if "myworkdayjobs.com" in h or "workday" in h: return "workday"
    if "linkedin.com" in h: return "linkedin"
    if "smartrecruiters.com" in h: return "smartrecruiters"
    if "workable.com" in h: return "workable"
    if "icims.com" in h: return "icims"
    return "unknown"


def _fetch(url: str, settings: dict, kind: str) -> list[Job]:
    out: list[Job] = []
    with client(40) as c:
        r = c.get(url)
        r.raise_for_status()
        for j in r.json():
            if not j.get("active", True) or not j.get("url"):
                continue
            title = j.get("title", "")
            if not title_relevant(title, settings):
                continue
            u = re.sub(r"[?&]utm_source=Simplify.*$", "", j["url"])
            locs = j.get("locations") or []
            out.append(Job(
                source=f"simplify:{kind}", ats=_ats_from_url(u), company=j.get("company_name", ""),
                title=title, location="; ".join(locs)[:200], remote=any("remote" in l.lower() for l in locs),
                url=u, apply_url=u, description="", posted_at=iso(j.get("date_posted")),
                raw={"sponsorship": j.get("sponsorship"), "terms": j.get("terms"), "degrees": j.get("degrees"),
                     "simplify_id": j.get("id"), "company_url": j.get("company_url")},
            ))
    return out


def fetch(settings: dict) -> list[Job]:
    jobs = _fetch(NEWGRAD, settings, "newgrad")
    if settings.get("include_internships"):
        jobs += _fetch(INTERNS, settings, "intern")
    return jobs
