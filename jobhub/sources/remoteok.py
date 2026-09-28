"""RemoteOK public JSON feed (https://remoteok.com/api). First element is a legal notice."""
from __future__ import annotations

from ..models import Job
from .base import client, iso, strip_html, title_relevant


def fetch(settings: dict) -> list[Job]:
    out: list[Job] = []
    with client() as c:
        r = c.get("https://remoteok.com/api")
        r.raise_for_status()
        for j in r.json()[1:]:
            title = j.get("position", "")
            if not title_relevant(title, settings):
                continue
            out.append(Job(
                source="remoteok", ats="unknown", company=j.get("company", ""), title=title,
                location=j.get("location") or "Remote", remote=True,
                salary_min=j.get("salary_min") or None, salary_max=j.get("salary_max") or None,
                url=j.get("url", ""), apply_url=j.get("apply_url") or j.get("url", ""),
                description=strip_html(j.get("description", ""))[:15000], posted_at=iso(j.get("epoch")),
                raw={"tags": j.get("tags", [])},
            ))
    return out
