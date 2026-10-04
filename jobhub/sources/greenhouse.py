"""Greenhouse public job board API: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs

The listing without ``content`` is about a tenth the size, so a scan reads it
first, keeps the relevant titles, and fetches full descriptions only for
postings it has not described before. A board with many new matches is read
once with ``content=true`` instead of one request per posting.
"""
from __future__ import annotations

from urllib.parse import quote

from ..models import Job
from .base import client, discovered_boards, fetch_boards, parse_salary, strip_html, title_relevant

API = "https://boards-api.greenhouse.io/v1/boards"
# Above this many undescribed matches, one full listing is cheaper than per-posting requests.
DETAIL_REQUEST_LIMIT = 12


def _job(slug: str, listing: dict, detail: dict | None) -> Job:
    data = detail or listing
    desc = strip_html(data.get("content", ""))
    smin, smax = parse_salary(desc)
    loc = (data.get("location") or {}).get("name", "")
    title = data.get("title", "")
    return Job(
        source="greenhouse", ats="greenhouse", company=(data.get("company_name") or slug).title(), title=title, location=loc, remote="remote" in (loc + title).lower(),
        salary_min=smin, salary_max=smax, url=data.get("absolute_url", ""),
        # the board-hosted form always exists and is never iframed, unlike company career pages
        apply_url=f"https://job-boards.greenhouse.io/{slug}/jobs/{data.get('id')}", description=desc[:15000],
        posted_at=data.get("first_published") or listing.get("first_published"),
        raw={"gh_id": data.get("id"), "slug": slug,
             "departments": [d.get("name") for d in (data.get("departments") or [])]},
    )


def _fetch_company(slug: str, settings: dict) -> list[Job]:
    known = settings.get("_known_postings") or set()
    board = f"{API}/{quote(slug, safe='')}/jobs"
    with client() as c:
        r = c.get(board)
        r.raise_for_status()
        listing = [j for j in r.json().get("jobs", []) if title_relevant(j.get("title", ""), settings)]
        needed = [j for j in listing if f"greenhouse:{slug.lower()}:{j.get('id')}" not in known]
        details: dict[object, dict] = {}
        if len(needed) > DETAIL_REQUEST_LIMIT:
            full = c.get(board, params={"content": "true"})
            full.raise_for_status()
            details = {j.get("id"): j for j in full.json().get("jobs", [])}
        else:
            for j in needed:
                try:
                    response = c.get(f"{board}/{j.get('id')}")
                    response.raise_for_status()
                    details[j.get("id")] = response.json()
                except Exception:  # noqa: BLE001 - the listing still names the role; describe it next scan
                    continue
    return [_job(slug, j, details.get(j.get("id"))) for j in listing]


def fetch(settings: dict) -> list[Job]:
    slugs = settings.get("companies", {}).get("greenhouse", [])
    return fetch_boards(slugs, _fetch_company, settings, source="greenhouse",
                        discovered=discovered_boards(settings, "greenhouse"))
