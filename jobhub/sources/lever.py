"""Lever public postings API: https://api.lever.co/v0/postings/{slug}?mode=json"""
from __future__ import annotations

from ..models import Job
from .base import fetch_boards, client, iso, parse_salary, strip_html, title_relevant


def _fetch_company(slug: str, settings: dict) -> list[Job]:
    out: list[Job] = []
    with client() as c:
        r = c.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
        r.raise_for_status()
        for j in r.json():
            title = j.get("text", "")
            if not title_relevant(title, settings):
                continue
            cats = j.get("categories") or {}
            desc = (j.get("descriptionPlain") or "") + "\n" + "\n".join(
                (l.get("text", "") + "\n" + l.get("content", "")) for l in j.get("lists", []))
            sr = j.get("salaryRange") or {}
            smin, smax = (sr.get("min"), sr.get("max")) if sr.get("currency") == "USD" and sr.get("interval", "per-year") in ("per-year", "year", "yearly") else parse_salary(desc)
            loc = cats.get("location", "") or ", ".join(cats.get("allLocations", []) or [])
            out.append(Job(
                source="lever", ats="lever", company=slug.replace("-", " ").title(), title=title,
                location=loc, remote="remote" in (loc + title + str(j.get("workplaceType", ""))).lower(),
                salary_min=smin, salary_max=smax, url=j.get("hostedUrl", ""), apply_url=j.get("applyUrl", ""),
                description=strip_html(desc)[:15000], posted_at=iso(j.get("createdAt")),
                raw={"lever_id": j.get("id"), "slug": slug, "team": cats.get("team"), "commitment": cats.get("commitment")},
            ))
    return out


def fetch(settings: dict) -> list[Job]:
    slugs = settings.get("companies", {}).get("lever", [])
    return fetch_boards(slugs, _fetch_company, settings, source="lever")
