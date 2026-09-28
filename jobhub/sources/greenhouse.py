"""Greenhouse public job board API: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"""
from __future__ import annotations

from ..models import Job
from .base import fetch_boards, client, parse_salary, strip_html, title_relevant


def _fetch_company(slug: str, settings: dict) -> list[Job]:
    out: list[Job] = []
    with client() as c:
        r = c.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            title = j.get("title", "")
            if not title_relevant(title, settings):
                continue
            desc = strip_html(j.get("content", ""))
            smin, smax = parse_salary(desc)
            loc = (j.get("location") or {}).get("name", "")
            out.append(Job(
                source="greenhouse", ats="greenhouse", company=(j.get("company_name") or slug).title(),
                title=title, location=loc, remote="remote" in (loc + title).lower(),
                salary_min=smin, salary_max=smax, url=j.get("absolute_url", ""),
                # the board-hosted form always exists and is never iframed, unlike company career pages
                apply_url=f"https://job-boards.greenhouse.io/{slug}/jobs/{j.get('id')}", description=desc[:15000],
                posted_at=j.get("first_published"),
                raw={"gh_id": j.get("id"), "slug": slug, "departments": [d.get("name") for d in j.get("departments", [])]},
            ))
    return out


def fetch(settings: dict) -> list[Job]:
    slugs = settings.get("companies", {}).get("greenhouse", [])
    return fetch_boards(slugs, _fetch_company, settings, source="greenhouse")
