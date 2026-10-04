"""Pydantic models shared by sources, scoring, tailoring, and the API."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def posted_epoch(value: Any) -> float | None:
    """Seconds since the epoch (UTC) for a posting date, or None when unknown.

    Sources publish ISO timestamps with offsets, ``Z`` suffixes, bare dates
    (job sites that only say "posted today"), or epoch numbers. A bare date is
    placed at noon UTC so it sorts sensibly without claiming a precise time.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000 if number > 1e12 else number
    text = str(value).strip()
    try:
        if _DATE_ONLY.match(text):
            return datetime.fromisoformat(text + "T12:00:00+00:00").timestamp()
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def posted_has_time(value: Any) -> bool:
    """True when a posting date includes a time of day (not just a date)."""
    return bool(value) and not _DATE_ONLY.match(str(value).strip())


_BOARD_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def board_of(url: str) -> tuple[str, str] | None:
    """The public Greenhouse, Lever, or Ashby board a posting URL belongs to.

    Used to discover more company boards from postings found elsewhere. Only
    exact hosted job-board hosts count; the slug is lower-cased.
    """
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    path = [part for part in parts.path.split("/") if part]
    slug, ats = "", ""
    if host in ("job-boards.greenhouse.io", "boards.greenhouse.io", "job-boards.eu.greenhouse.io",
                "boards.eu.greenhouse.io"):
        ats = "greenhouse"
        if "jobs" in path and path.index("jobs") > 0:
            slug = path[path.index("jobs") - 1]
        elif path[:1] == ["embed"]:
            slug = (parse_qs(parts.query).get("for") or [""])[0]
    elif host == "jobs.lever.co" and len(path) >= 2:
        ats, slug = "lever", path[0]
    elif host == "jobs.ashbyhq.com" and len(path) >= 2:
        ats, slug = "ashby", path[0]
    if not ats or not _BOARD_SLUG.match(slug) or slug.lower() in ("embed", "jobs"):
        return None
    return ats, slug.lower()


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    tracking = {"source", "ref", "referrer", "gh_src", "lever-source", "lever-origin", "src", "trk"}
    query = sorted((k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_") and k.lower() not in tracking)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query), ""))


def job_id_for(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode()).hexdigest()[:16]


def job_fingerprint(job: "Job") -> str:
    """Match the same ATS posting across discovery sources and tracking links."""
    for url in (job.apply_url, job.url):
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        path = [part for part in parts.path.split("/") if part]
        if (host == "greenhouse.io" or host.endswith(".greenhouse.io")) and "jobs" in path:
            i = path.index("jobs")
            if i and i + 1 < len(path):
                return f"greenhouse:{path[i-1].lower()}:{path[i+1]}"
        if host in ("jobs.lever.co", "jobs.eu.lever.co") and len(path) >= 2:
            return f"lever:{path[0].lower()}:{path[1]}"
        if host == "jobs.ashbyhq.com" and len(path) >= 2:
            return f"ashby:{path[0].lower()}:{path[1]}"
    raw_id = job.raw.get({"greenhouse": "gh_id", "lever": "lever_id", "ashby": "ashby_id"}.get(job.ats, ""))
    if raw_id and job.raw.get("slug"):
        return f"{job.ats}:{job.raw['slug'].lower()}:{raw_id}"
    return canonical_url(job.url)


class Job(BaseModel):
    id: str = ""
    source: str                      # greenhouse | lever | ashby | simplify | remoteok | jobspy:indeed ...
    ats: str = "unknown"             # greenhouse | lever | ashby | workday | linkedin | unknown
    company: str
    title: str
    location: str = ""
    remote: bool = False
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    url: str                         # canonical posting URL (dedupe key)
    apply_url: str = ""              # where the application form lives
    description: str = ""
    posted_at: Optional[str] = None  # ISO
    discovered_at: str = Field(default_factory=now_iso)
    score: float = 0.0
    score_json: dict[str, Any] = Field(default_factory=dict)
    status: str = "new"              # new | queued | applying | applied | skipped | failed | needs_human
    raw: dict[str, Any] = Field(default_factory=dict)
    # None: follow the priority-company list; True/False: the candidate's own choice.
    starred: Optional[bool] = None

    def model_post_init(self, __context: Any) -> None:
        if not self.id:
            self.id = job_id_for(self.url)
        if not self.apply_url:
            self.apply_url = self.url


class Education(BaseModel):
    school: str = ""
    degree: str = ""
    field: str = ""
    location: str = ""
    start: str = ""
    end: str = ""
    gpa: str = ""
    details: list[str] = Field(default_factory=list)   # honors, relevant coursework


class Experience(BaseModel):
    company: str = ""
    title: str = ""
    location: str = ""
    start: str = ""
    end: str = ""
    bullets: list[str] = Field(default_factory=list)
    tech: list[str] = Field(default_factory=list)
    pinned: bool = False     # always include on generated resumes
    hidden: bool = False     # keep in the library, never print


class Project(BaseModel):
    name: str = ""
    role: str = ""
    start: str = ""
    end: str = ""
    bullets: list[str] = Field(default_factory=list)
    tech: list[str] = Field(default_factory=list)
    link: str = ""
    pinned: bool = False
    hidden: bool = False


class Profile(BaseModel):
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""
    state: str = ""
    region: str = ""
    country: str = ""
    address_line1: str = ""
    address_line2: str = ""
    postal_code: str = ""
    linkedin: str = ""
    github: str = ""
    website: str = ""
    headline: str = ""
    summary: str = ""
    education: list[Education] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    awards: list[str] = Field(default_factory=list)   # awards, certifications, leadership one-liners
    raw_text: str = ""
    resume_path: str = ""
    custom_facts: dict[str, str] = Field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def location(self) -> str:
        return ", ".join(x for x in (self.city, self.state or self.region, self.country) if x)


class ApplyResult(BaseModel):
    status: str                     # applied | dry_run | needs_human | failed
    message: str = ""
    screenshots: list[str] = Field(default_factory=list)
    log: list[str] = Field(default_factory=list)
    unanswered: list[str] = Field(default_factory=list)
    resume_path: str = ""
    submission_attempted: bool = False
    # Details (label, kind, options, required) for questions left unanswered,
    # so a reviewer can answer them without reopening the employer form.
    questions: list[dict[str, Any]] = Field(default_factory=list)
