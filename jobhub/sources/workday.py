"""Workday public job search: POST https://{tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

Many banks, insurers, and asset managers publish only on Workday. A board is
written ``tenant.wdN/site`` (``citi.wd5/2``), the parts of its careers URL
``https://citi.wd5.myworkdayjobs.com/2``. Listings carry only a title, so the
title filter runs first and descriptions are fetched for matching roles only.
Workday applications sit behind a per-employer account, so these jobs are
filled in a visible browser with the candidate signed in (``apply.workday``),
never sent unattended.
"""
from __future__ import annotations

import re

from ..models import Job
from .base import client, discovered_boards, fetch_boards, parse_salary, strip_html, title_relevant

BOARD_RE = re.compile(r"^([a-z0-9][a-z0-9-]{0,62})\.(wd\d{1,3})/([a-z0-9][a-z0-9_.-]{0,99})$")
PAGE = 20                 # Workday's maximum page size
MAX_PAGES = 15            # newest 300 postings per board and scan
DETAILS_PER_BOARD = 60    # descriptions fetched per board and scan
US = "united states of america"

# Tenants are short codes; show the employer's name instead.
NAMES = {
    "citi": "Citi", "ms": "Morgan Stanley", "blackrock": "BlackRock", "statestreet": "State Street",
    "troweprice": "T. Rowe Price", "travelers": "Travelers", "spgi": "S&P Global", "aig": "AIG",
    "barclays": "Barclays", "vanguard": "Vanguard", "capitalone": "Capital One", "fmr": "Fidelity Investments",
    "lseg": "LSEG", "td": "TD Bank", "clearwateranalytics": "Clearwater Analytics", "visa": "Visa",
    "equifax": "Equifax", "nvidia": "NVIDIA", "cigna": "Cigna", "globalhr": "RTX", "ngc": "Northrop Grumman",
    "paloaltonetworks": "Palo Alto Networks", "amat": "Applied Materials", "generalmotors": "General Motors",
}


def parse_board(board: str) -> tuple[str, str, str] | None:
    match = BOARD_RE.match(str(board or "").strip().casefold())
    return (match.group(1), match.group(2), match.group(3)) if match else None


def company_name(tenant: str) -> str:
    return NAMES.get(tenant) or tenant.replace("-", " ").title()


def _us_facet(facets: list) -> dict[str, list[str]]:
    """The board's own "United States" location filter, when it offers one."""
    stack = list(facets or [])
    while stack:
        facet = stack.pop()
        if not isinstance(facet, dict):
            continue
        parameter = facet.get("facetParameter")
        for value in facet.get("values") or []:
            if not isinstance(value, dict):
                continue
            if "facetParameter" in value:       # a facet group (Locations → Country, City...)
                stack.append(value)
            elif parameter and "country" in parameter.casefold() and \
                    str(value.get("descriptor", "")).casefold() == US and value.get("id"):
                return {parameter: [value["id"]]}
    return {}


def _fetch_board(board: str, settings: dict) -> list[Job]:
    parts = parse_board(board)
    if parts is None:
        raise ValueError(f"Not a Workday board (expected tenant.wdN/site): {board}")
    tenant, wd, site = parts
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}"
    out: list[Job] = []
    with client() as c:
        def search(offset: int, facets: dict) -> dict:
            r = c.post(f"{api}/jobs", json={"appliedFacets": facets, "limit": PAGE, "offset": offset,
                                             "searchText": ""}, headers={"Accept": "application/json"})
            r.raise_for_status()
            return r.json()

        first = search(0, {})
        facets = _us_facet(first.get("facets"))
        page = search(0, facets) if facets else first
        total = int(page.get("total") or 0)
        postings = list(page.get("jobPostings") or [])
        for number in range(1, MAX_PAGES):
            if number * PAGE >= total:
                break
            postings += search(number * PAGE, facets).get("jobPostings") or []
        wanted = [p for p in postings if p.get("externalPath") and title_relevant(p.get("title", ""), settings)]
        for posting in wanted[:DETAILS_PER_BOARD]:
            r = c.get(f"{api}{posting['externalPath']}", headers={"Accept": "application/json"})
            if r.status_code != 200:
                continue
            info = (r.json() or {}).get("jobPostingInfo") or {}
            if info.get("canApply") is False:
                continue
            country = str(((info.get("country") or {}).get("descriptor")) or "").casefold()
            if facets == {} and country and country != US:
                continue
            desc = strip_html(info.get("jobDescription") or "")
            smin, smax = parse_salary(desc)
            locations = [info.get("location") or posting.get("locationsText") or ""] + list(info.get("additionalLocations") or [])
            location = "; ".join(dict.fromkeys(filter(None, locations)))
            url = info.get("externalUrl") or f"{base}/{site}{posting['externalPath']}"
            out.append(Job(
                source="workday", ats="workday", company=company_name(tenant), title=info.get("title") or posting["title"],
                location=location, remote=bool(re.search(r"\bremote\b", f"{location} {info.get('remoteType', '')}", re.I)),
                salary_min=smin, salary_max=smax, url=url, apply_url=url, description=desc[:15000],
                posted_at=info.get("startDate"),
                raw={"workday_board": f"{tenant}.{wd}/{site}", "req_id": info.get("jobReqId"),
                     "time_type": info.get("timeType"), "end_date": info.get("endDate")},
            ))
    return out


def fetch(settings: dict) -> list[Job]:
    boards = [b for b in settings.get("companies", {}).get("workday", []) if parse_board(b)]
    return fetch_boards(boards, _fetch_board, settings, source="workday",
                        discovered=discovered_boards(settings, "workday"))
