"""Import one public Greenhouse, Lever, or Ashby posting by URL.

The importer intentionally accepts only the hosted job-board domains and derives
the API request from the board path.  It never requests a user supplied URL.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from ..models import Job
from .base import client, iso, parse_salary, strip_html


class JobImportError(ValueError):
    """A URL is unsupported or the public posting could not be fetched."""


@dataclass(frozen=True)
class ParsedJobURL:
    ats: str
    slug: str
    posting_id: str


# These are exact hosts.  In particular, do not use suffix matching here:
# ``evilgreenhouse.io`` and ``jobs.lever.co.evil.test`` must never pass.
_HOSTS = {
    "job-boards.greenhouse.io": "greenhouse",
    "boards.greenhouse.io": "greenhouse",
    # EU-hosted boards; the public API at boards-api.greenhouse.io serves them too.
    "job-boards.eu.greenhouse.io": "greenhouse",
    "boards.eu.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.eu.lever.co": "lever-eu",
    "jobs.ashbyhq.com": "ashby",
    "grnh.se": "greenhouse",
    "apply.workable.com": "workable",
    "ats.rippling.com": "rippling",
}
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Ashby board names are organization names and may contain spaces ("Superhuman Platform Inc").
_ASHBY_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$")


def parse_job_url(url: str, *, _redirects: int = 0) -> ParsedJobURL:
    if not isinstance(url, str) or not url.strip():
        raise JobImportError("A public job URL is required")
    from urllib.parse import parse_qs
    from .base import _greenhouse_board_for

    parts = urlsplit(url.strip())
    if parts.scheme.lower() != "https" or parts.username or parts.password or parts.port:
        raise JobImportError("Only HTTPS public job-board URLs are supported")
    host = (parts.hostname or "").lower().rstrip(".")

    # Follow grnh.se shortlink redirects
    if host == "grnh.se":
        if _redirects >= 4:
            raise JobImportError("Could not resolve shortlink: redirect loop")
        try:
            with client(timeout=10.0, follow_redirects=True) as http:
                response = http.get(url.strip())
                final_url = str(response.url)
        except Exception as exc:
            raise JobImportError(f"Could not resolve shortlink: {exc}") from exc
        return parse_job_url(final_url, _redirects=_redirects + 1)

    # Handle boards.greenhouse.io/embed/job_app URLs
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io") and "/embed/job_app" in parts.path:
        qs = parse_qs(parts.query)
        token = (qs.get("token") or qs.get("gh_jid") or [""])[0].strip()
        for_slug = (qs.get("for") or [""])[0].strip()
        if for_slug:
            slug = for_slug
            posting_id = token
        elif token:
            slug = _greenhouse_board_for(token) or ""
            posting_id = token
        else:
            slug, posting_id = "", ""
        if slug and posting_id and _SEGMENT.match(slug) and _SEGMENT.match(posting_id):
            return ParsedJobURL("greenhouse", slug, posting_id)
        raise JobImportError("Could not resolve embedded Greenhouse job application")

    # Handle arbitrary career sites with ?gh_jid=
    if host not in _HOSTS and parts.query:
        qs = parse_qs(parts.query)
        gh_jid = (qs.get("gh_jid") or [""])[0].strip()
        if gh_jid and gh_jid.isdigit():
            slug = _greenhouse_board_for(gh_jid)
            if slug and _SEGMENT.match(slug):
                return ParsedJobURL("greenhouse", slug, gh_jid)

    ats = _HOSTS.get(host)
    if not ats:
        raise JobImportError("Unsupported job-board host")
    # Decode only for validation.  Encoded separators must not create hidden
    # path components or alter the API path.
    path = unquote(parts.path)
    if "%2f" in parts.path.lower() or "%5c" in parts.path.lower():
        raise JobImportError("Invalid job-board path")
    segments = [segment for segment in path.split("/") if segment]
    if ats in ("workable", "rippling"):
        marker = "j" if ats == "workable" else "jobs"
        valid = len(segments) in (3, 4) and segments[1] == marker and (len(segments) == 3 or segments[3] == "apply")
        slug, posting_id = (segments[0], segments[2]) if valid else ("", "")
    elif ats == "greenhouse":
        valid = len(segments) == 3 and segments[1].lower() == "jobs"
        slug, posting_id = (segments[0], segments[2]) if valid else ("", "")
    else:
        valid = len(segments) in (2, 3) and (len(segments) == 2 or segments[2].lower() in ("apply", "application"))
        slug, posting_id = (segments[0], segments[1]) if valid else ("", "")
    slug_pattern = _ASHBY_SLUG if ats == "ashby" else _SEGMENT
    if not valid or not slug_pattern.match(slug) or slug != slug.strip() or not _SEGMENT.match(posting_id):
        raise JobImportError("Expected a supported public job-board URL")
    if parts.fragment:
        raise JobImportError("URL fragments are not supported")
    return ParsedJobURL(ats, slug, posting_id)


def _get_payload(parsed: ParsedJobURL) -> dict:
    slug, posting_id = quote(parsed.slug, safe=""), quote(parsed.posting_id, safe="")
    if parsed.ats == "greenhouse":
        endpoint = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{posting_id}"
        params = {"content": "true"}
    elif parsed.ats in ("lever", "lever-eu"):
        api_host = "api.eu.lever.co" if parsed.ats == "lever-eu" else "api.lever.co"
        endpoint = f"https://{api_host}/v0/postings/{slug}/{posting_id}"
        params = {"mode": "json"}
    elif parsed.ats == "workable":
        endpoint = f"https://apply.workable.com/api/v2/accounts/{slug}/jobs/{posting_id}"
        params = {}
    elif parsed.ats == "rippling":
        endpoint = f"https://ats.rippling.com/api/v1/board/{slug}/jobs/{posting_id}"
        params = {}
    else:
        # Ashby's public board endpoint returns the board's listed postings.
        # Matching locally avoids guessing an undocumented per-posting URL.
        endpoint = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        params = {"includeCompensation": "true"}
    with client(follow_redirects=False) as http:
        response = http.get(endpoint, params=params)
        response.raise_for_status()
        payload = response.json()
    if parsed.ats == "ashby":
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        match = next((j for j in jobs if str(j.get("id", "")) == parsed.posting_id), None)
        if match is None:
            raise JobImportError("Public posting was not found")
        return match
    if not isinstance(payload, dict) or not payload:
        raise JobImportError("Public posting was not found")
    return payload


def _job_from_payload(parsed: ParsedJobURL, payload: dict, requested_url: str) -> Job:
    if parsed.ats in ("workable", "rippling"):
        expected = str(payload.get("shortcode" if parsed.ats == "workable" else "uuid") or "")
        if expected.casefold() != parsed.posting_id.casefold():
            raise JobImportError("Public posting no longer matches the requested vacancy")
        if parsed.ats == "workable":
            if payload.get("state") != "published":
                raise JobImportError("Public posting is no longer published")
            location = payload.get("location") or {}
            loc = ", ".join(str(location[k]) for k in ("city", "region", "country") if location.get(k))
            desc = strip_html("\n".join(str(payload.get(k) or "") for k in ("description", "requirements", "benefits")))
            title = payload.get("title") or ""
            published = payload.get("published")
            remote = bool(payload.get("remote"))
            company = parsed.slug.replace("-", " ").title()
            url = f"https://apply.workable.com/{parsed.slug}/j/{parsed.posting_id}/"
        else:
            desc = payload.get("description") or {}
            desc = strip_html("\n".join(str(v) for v in desc.values())) if isinstance(desc, dict) else strip_html(str(desc))
            loc = "; ".join(str(v) for v in payload.get("workLocations") or [])
            title = payload.get("name") or ""
            published = payload.get("createdOn")
            remote = "remote" in loc.casefold()
            company = payload.get("companyName") or parsed.slug.replace("-", " ").title()
            url = f"https://ats.rippling.com/{parsed.slug}/jobs/{parsed.posting_id}"
        salary_min, salary_max = parse_salary(desc)
        return Job(source=parsed.ats, ats=parsed.ats, company=company, title=title, location=loc,
                   remote=remote, salary_min=salary_min, salary_max=salary_max, url=url, apply_url=url,
                   description=desc[:15000], posted_at=published,
                   raw={"slug": parsed.slug, f"{parsed.ats}_id": parsed.posting_id})
    if parsed.ats == "greenhouse":
        title = payload.get("title", "")
        desc = strip_html(payload.get("content", ""))
        location = (payload.get("location") or {}).get("name", "")
        salary_min, salary_max = parse_salary(desc)
        posting_url = payload.get("absolute_url") or requested_url
        apply_url = f"https://job-boards.greenhouse.io/{quote(parsed.slug, safe='')}/jobs/{quote(parsed.posting_id, safe='')}"
        return Job(source="greenhouse", ats="greenhouse", company=payload.get("company_name") or parsed.slug.title(),
                   title=title, location=location, remote="remote" in (location + title).lower(),
                   salary_min=salary_min, salary_max=salary_max, url=posting_url,
                   apply_url=apply_url, description=desc[:15000], posted_at=payload.get("first_published"),
                   raw={"gh_id": payload.get("id", parsed.posting_id), "slug": parsed.slug,
                        "departments": [d.get("name") for d in payload.get("departments", [])]})
    if parsed.ats in ("lever", "lever-eu"):
        title = payload.get("text", "")
        cats = payload.get("categories") or {}
        desc = (payload.get("descriptionPlain") or "") + "\n" + "\n".join(
            (l.get("text", "") + "\n" + l.get("content", "")) for l in payload.get("lists", []))
        desc = strip_html(desc)
        salary = payload.get("salaryRange") or {}
        annual = salary.get("interval", "per-year") in ("per-year", "year", "yearly")
        salary_min, salary_max = ((salary.get("min"), salary.get("max"))
                                  if salary.get("currency") == "USD" and annual else parse_salary(desc))
        posting_url = payload.get("hostedUrl") or requested_url
        return Job(source="lever", ats="lever", company=parsed.slug.replace("-", " ").title(), title=title,
                   location=cats.get("location", "") or ", ".join(cats.get("allLocations", []) or []),
                   remote="remote" in (str(cats) + title).lower(), salary_min=salary_min, salary_max=salary_max,
                   url=posting_url, apply_url=payload.get("applyUrl") or posting_url, description=desc[:15000],
                   posted_at=iso(payload.get("createdAt")), raw={"lever_id": payload.get("id", parsed.posting_id),
                   "slug": parsed.slug, "team": cats.get("team"), "commitment": cats.get("commitment")})
    title = payload.get("title", "")
    desc = payload.get("descriptionPlain") or strip_html(payload.get("descriptionHtml", ""))
    loc = payload.get("location", "")
    comp = payload.get("compensation") or {}
    salary_min, salary_max = parse_salary(comp.get("compensationTierSummary") or desc)
    posting_url = payload.get("jobUrl") or requested_url
    return Job(source="ashby", ats="ashby", company=parsed.slug.replace("-", " ").title(), title=title,
               location=loc, remote=bool(payload.get("isRemote")) or "remote" in str(loc).lower(),
               salary_min=salary_min, salary_max=salary_max, url=posting_url,
               apply_url=payload.get("applyUrl") or posting_url, description=desc[:15000],
               posted_at=payload.get("publishedAt"), raw={"ashby_id": payload.get("id", parsed.posting_id),
               "slug": parsed.slug, "team": payload.get("team"), "employmentType": payload.get("employmentType")})


def fetch_job(url: str) -> Job:
    """Fetch and validate one currently listed public posting."""
    try:
        parsed = parse_job_url(url)
        payload = _get_payload(parsed)
    except JobImportError:
        raise
    except Exception as exc:  # keep transport details out of the API contract
        raise JobImportError(f"Could not fetch public posting: {exc}") from exc
    job = _job_from_payload(parsed, payload, url.strip())
    if not job.title.strip() or not job.url.strip():
        raise JobImportError("Public response did not contain a valid job")
    return job


def import_job(url: str, db_module=None) -> tuple[Job, bool]:
    """Fetch a posting, then insert or refresh it using canonical DB behavior."""
    job = fetch_job(url)
    if db_module is None:
        from .. import db as db_module
    created = bool(db_module.upsert_jobs([job]))
    if created:
        job = db_module.get_job(job.id) or job
    else:
        # upsert_jobs may replace the ID with the historical canonical ID.
        existing = db_module.get_job(job.id)
        if existing:
            job = existing
    return job, created
