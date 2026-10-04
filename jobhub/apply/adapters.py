"""Small, explicit ATS adapters; unknown/login-gated flows require manual handling."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from ..models import Job


@dataclass
class Adapter:
    name: str
    apply_url: str
    submit_selectors: list[str] = field(default_factory=lambda: [
        "button[type=submit]:visible", "input[type=submit]:visible",
        "button:has-text('Submit application')", "#btn-submit",
    ])
    apply_click_selectors: list[str] = field(default_factory=lambda: [
        "a:has-text('Apply for this job')", "button:has-text('Apply for this job')",
        "a:has-text('Apply now')", "button:has-text('Apply now')", "#apply_button", "a.postings-btn",
    ])
    # An ordinary "thank you for your interest" often appears in the original
    # posting. A new, application-specific acknowledgement is required.
    success_re: re.Pattern = field(default_factory=lambda: re.compile(
        r"thank you for (?:applying|your application)|thanks for applying|"
        r"(?:your )?application (?:has been |was |is )?(?:successfully )?(?:submitted|received)|"
        r"we(?:'ve| have) received your application|"
        r"(?:successfully submitted (?:your |an )?application)|your application is in", re.I))
    form_ready_selector: str = "input[type=file], form"
    settle_ms: int = 1800


def _with_suffix(url: str, suffix: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if not path.endswith(suffix):
        path += suffix
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def adapter_for(job: Job) -> Adapter | None:
    """Use the actual URL host, never an untrusted substring or source hint.

    Company-owned pages and other ATSs can still be discovered and ranked, but
    automatic submission is limited to the three implementations below.
    """
    url = job.apply_url or job.url
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        return None
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io",
                "job-boards.eu.greenhouse.io"}:
        return Adapter("greenhouse", url, form_ready_selector="#first_name, input[type=file], form")
    if host == "jobs.ashbyhq.com" and re.fullmatch(r"/[^/]+/[0-9a-fA-F-]{36}(?:/application)?/?", parsed.path):
        return Adapter("ashby", _with_suffix(url, "/application"),
                       form_ready_selector="#_systemfield_name, input[type=file]")
    if host in {"jobs.lever.co", "jobs.eu.lever.co"} and re.fullmatch(
            r"/[^/]+/[0-9a-fA-F-]{36}(?:/apply)?/?", parsed.path):
        return Adapter("lever", _with_suffix(url, "/apply"),
                       submit_selectors=["#btn-submit", "button[type=submit]:visible"],
                       form_ready_selector="input[name=name], input[type=file]")
    return None
