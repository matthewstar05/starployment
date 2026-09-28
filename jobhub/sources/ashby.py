"""Ashby public job board API: https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"""
from __future__ import annotations

from ..models import Job
from .base import fetch_boards, client, parse_salary, strip_html, title_relevant


def _fetch_company(slug: str, settings: dict) -> list[Job]:
    out: list[Job] = []
    with client() as c:
        r = c.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"})
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            title = j.get("title", "")
            if not title_relevant(title, settings):
                continue
            desc = j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", ""))
            comp = (j.get("compensation") or {})
            smin = smax = None
            for tier in comp.get("compensationTiers", []) or []:
                for comp_ in tier.get("components", []) or []:
                    if comp_.get("compensationType") == "Salary" and comp_.get("interval") in ("1 YEAR", None):
                        smin = min(filter(None, [smin, comp_.get("minValue")]), default=comp_.get("minValue"))
                        smax = max(filter(None, [smax, comp_.get("maxValue")]), default=comp_.get("maxValue"))
            if smin is None:
                smin, smax = parse_salary(comp.get("compensationTierSummary") or desc)
            loc = j.get("location", "")
            out.append(Job(
                source="ashby", ats="ashby", company=slug.replace("-", " ").title(), title=title,
                location=loc, remote=bool(j.get("isRemote")) or "remote" in loc.lower(),
                salary_min=int(smin) if smin else None, salary_max=int(smax) if smax else None,
                url=j.get("jobUrl", ""), apply_url=j.get("applyUrl") or j.get("jobUrl", ""),
                description=desc[:15000], posted_at=j.get("publishedAt"),
                raw={"ashby_id": j.get("id"), "slug": slug, "team": j.get("team"), "employmentType": j.get("employmentType")},
            ))
    return out


def fetch(settings: dict) -> list[Job]:
    slugs = settings.get("companies", {}).get("ashby", [])
    return fetch_boards(slugs, _fetch_company, settings, source="ashby")
