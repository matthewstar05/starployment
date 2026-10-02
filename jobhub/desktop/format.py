"""Pure text helpers for the desktop client. No Qt imports; safe to unit test."""
from __future__ import annotations

import re
from datetime import datetime, timezone

STATUS_LABELS = {
    "new": "New",
    "queued": "Queued",
    "applying": "Applying",
    "applied": "Applied",
    "needs_human": "Needs review",
    "failed": "Issue",
    "skipped": "Skipped",
    "dry_run": "Practice run",
    # prepared applications (drafts)
    "prepared": "Prepared",
    "preparing": "Preparing",
    "ready": "Ready to review",
    "needs_answers": "Needs answers",
    "approved": "Approved",
    "sending": "Sending",
    "sent": "Sent",
    "practice": "Practice run",
    "unconfirmed": "Unconfirmed",
    "error": "Issue",
    # hiring stages
    "assessment": "Assessment",
    "interview": "Interview",
    "offer": "Offer",
    "rejected": "Rejected",
    "withdrawn": "Withdrawn",
}


def status_label(status: str | None) -> str:
    """Human wording for a job or application status."""
    status = str(status or "unknown")
    return STATUS_LABELS.get(status, status.replace("_", " ").title())


def retryable_job_ids(applications: list[dict]) -> set[str]:
    """Offer another attempt only before any known submit for the vacancy."""
    blocked = set()
    latest: dict[str, dict] = {}
    for record in applications:
        job_id = record.get("job_id")
        key = record.get("canonical_key") or job_id
        if record.get("submission_attempted") or record.get("status") in {"applied", "queued", "running"}:
            blocked.add(key)
        if job_id and (job_id not in latest or int(record.get("id") or 0) > int(latest[job_id].get("id") or 0)):
            latest[job_id] = record
    return {
        job_id for job_id, record in latest.items()
        if record.get("status") in {"needs_human", "failed", "dry_run"}
        and (record.get("canonical_key") or job_id) not in blocked
    }


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def local_time(value: str | None) -> str:
    """``Sep 24, 04:50 PM`` in the computer's time zone; unparseable values pass through."""
    if not value:
        return ""
    when = parse_time(value)
    if when is None:
        return str(value)
    return when.astimezone().strftime("%b %d, %I:%M %p")


def relative_time(value: str | None, now: datetime | None = None) -> str:
    """Apple-style relative time: ``Just now``, ``12m ago``, ``3h ago``, ``Yesterday``, ``Sep 24``."""
    when = parse_time(value)
    if when is None:
        return str(value or "")
    now = now or datetime.now(timezone.utc)
    seconds = (now - when).total_seconds()
    if seconds < 0:
        minutes = int(-seconds // 60)
        if minutes < 1:
            return "In a moment"
        return f"In {minutes}m" if minutes < 60 else f"In {minutes // 60}h"
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    local = when.astimezone()
    today = now.astimezone().date()
    if local.date() == today:
        return f"{int(seconds // 3600)}h ago"
    if (today - local.date()).days == 1:
        return "Yesterday"
    if local.year == today.year:
        return local.strftime("%b %d").replace(" 0", " ")
    return local.strftime("%b %d, %Y").replace(" 0", " ")


def countdown(seconds: float) -> str:
    """``in 42 min``/``in 1 hr 5 min`` for the next scheduled scan."""
    minutes = max(0, int(round(seconds / 60)))
    if minutes < 1:
        return "in under a minute"
    if minutes < 60:
        return f"in {minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"in {hours} hr" + (f" {rest} min" if rest else "")


def greeting(first_name: str = "", hour: int | None = None) -> str:
    hour = datetime.now().hour if hour is None else hour
    part = "Good morning" if 5 <= hour < 12 else "Good afternoon" if 12 <= hour < 18 else "Good evening"
    name = first_name.strip().split(" ")[0] if first_name and first_name.strip() else ""
    return f"{part}, {name}" if name else part


def salary(minimum: int | None, maximum: int | None) -> str:
    def compact(amount: int) -> str:
        return f"${amount / 1000:,.0f}k" if amount >= 1000 else f"${amount:,}"

    if minimum and maximum:
        return compact(minimum) if minimum == maximum else f"{compact(minimum)}–{compact(maximum)}"
    if minimum:
        return f"{compact(minimum)}+"
    if maximum:
        return f"Up to {compact(maximum)}"
    return "—"


def plural(count: int, noun: str, suffix: str = "s") -> str:
    return f"{count:,} {noun}{'' if count == 1 else suffix}"


_OUTCOME = re.compile(r"^(needs_human|applied|failed|dry_run): ([a-f0-9]{16}):")


def activity_label(message: str, applications_by_job: dict[str, dict]) -> str | None:
    """Turn diagnostic events into short dashboard copy without losing the log.

    Returns ``None`` for noise the dashboard should not show (per-source
    diagnostics and resume build notes stay in the scan log).
    """
    if message.startswith("Source "):
        return None
    outcome = _OUTCOME.match(message)
    if outcome:
        status, job_id = outcome.groups()
        application = applications_by_job.get(job_id, {})
        company = application.get("company", "Employer")
        title = application.get("title", "application")
        subject = f"{company} · {title}"
        return {
            "needs_human": f"Needs review · {subject}",
            "applied": f"Application confirmed · {subject}",
            "failed": f"Application issue · {subject}",
            "dry_run": f"Practice run finished · {subject}",
        }[status]
    if message.startswith("Tailored resume built for "):
        return None
    if message.startswith("Applying: "):
        return "Preparing application · " + message.removeprefix("Applying: ")
    if message.startswith("Scan started:"):
        return "Searching for new jobs"
    if message.startswith("Scan partial:"):
        return "Job search finished with some source issues"
    if message.startswith(("Scan ok:", "Scan complete:")):
        return "Job search finished"
    if message.startswith("Scan error:"):
        return "Job search needs attention"
    if message.startswith("Candidate setup saved"):
        return "Application profile updated"
    return message[:110].strip() or None


def activity_tone(message: str, level: str = "info") -> str:
    """Semantic tone (dot color) for an event: green, amber, red, accent, or neutral."""
    if message.startswith(("applied:", "Scan ok:", "Scan complete:")) or level == "success":
        return "green"
    if message.startswith(("needs_human:", "Scan partial:")) or level == "warn":
        return "amber"
    if message.startswith(("failed:", "Scan error:")) or level == "error" or " failed:" in message:
        return "red"
    if message.startswith(("Scan started:", "Applying: ")):
        return "accent"
    return "neutral"


_LOG_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)\s+(.*)$")


def pretty_log_line(line: str) -> str:
    """Replace a leading ISO timestamp with local, readable time."""
    match = _LOG_TIMESTAMP.match(str(line))
    if not match or parse_time(match.group(1)) is None:
        return str(line)
    return f"{local_time(match.group(1))}   {match.group(2)}"


# --------------------------------------------------------------------------- tracker
STAGE_ORDER = ("applied", "assessment", "interview", "offer", "rejected", "withdrawn")
ACTIVE_STAGES = {"applied", "assessment", "interview", "offer"}
CLOSED_STAGES = {"rejected", "withdrawn"}


def salary_midpoint(minimum: int | None, maximum: int | None) -> float | None:
    values = [v for v in (minimum, maximum) if isinstance(v, (int, float)) and v > 0]
    return sum(values) / len(values) if values else None


def tracker_stats(rows: list[dict]) -> dict:
    """Summary numbers for the Tracker header. A score is never shown as a probability."""
    from statistics import median

    total = len(rows)
    stages = [str(r.get("stage") or "applied") for r in rows]
    responded = sum(1 for s in stages if s in ("assessment", "interview", "offer", "rejected"))
    pays = [m for r in rows if (m := salary_midpoint(r.get("salary_min"), r.get("salary_max")))]
    places: dict[str, int] = {}
    for r in rows:
        place = "Remote" if r.get("remote") and not r.get("location") else str(r.get("location") or "").split(";")[0].strip()
        if place:
            places[place] = places.get(place, 0) + 1
    return {
        "total": total,
        "responded": responded,
        "response_rate": round(100 * responded / total) if total else 0,
        "assessment": stages.count("assessment"),
        "interview": stages.count("interview"),
        "offer": stages.count("offer"),
        "rejected": stages.count("rejected"),
        "median_salary": int(median(pays)) if pays else None,
        "top_locations": sorted(places.items(), key=lambda item: (-item[1], item[0]))[:3],
    }


def compact_money(amount: float | None) -> str:
    if not amount:
        return "—"
    return f"${amount / 1000:,.0f}k" if amount >= 1000 else f"${amount:,.0f}"
