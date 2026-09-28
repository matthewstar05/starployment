"""Read-only form readiness checks before reserving an unattended application.

Greenhouse publishes the required application questions through its public
job-detail API. This check sends no candidate data to the API and only skips a
job when a required answer cannot be supplied from saved facts. The browser
engine remains the authority on the live form and submission outcome.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import quote

from .answers import Q, rule_answer
from .models import Job, Profile
from .sources.base import client, strip_html
from .sources.import_job import parse_job_url


@dataclass(frozen=True)
class Readiness:
    status: str  # ready | missing_facts | unknown
    missing: tuple[str, ...] = ()


_FIELD_KINDS = {
    "input_text": "text",
    "textarea": "textarea",
    "multi_value_single_select": "select",
    "multi_value_multi_select": "select",
    "input_file": "file",
}


def _answerable(question: dict, profile: Profile, settings: dict, job: Job) -> bool | None:
    """True/False for known field types; None if metadata is too sparse to judge."""
    label = strip_html(question.get("label") or "")
    fields = question.get("fields")
    if not label or not isinstance(fields, list) or not fields:
        return None
    known_fields = 0
    unknown_fields = 0
    for field in fields:
        if not isinstance(field, dict):
            unknown_fields += 1
            continue
        field_type = field.get("type")
        if field_type == "input_hidden":
            # Greenhouse location widgets derive their hidden coordinates from
            # the visible location answer; these are not candidate facts.
            continue
        kind = _FIELD_KINDS.get(field_type)
        if not kind:
            unknown_fields += 1
            continue
        known_fields += 1
        field_name = str(field.get("name") or "")
        if kind == "file" and field_name == "resume" and Path(profile.resume_path).is_file():
            return True
        values = field.get("values") or []
        options = [str(value.get("label")) for value in values
                   if isinstance(value, dict) and value.get("label") is not None]
        q = Q(label=label, kind=kind, options=options, required=True, name=field_name)
        answer = rule_answer(q, profile, settings, job)
        if answer is None:
            continue
        if kind != "file" or (isinstance(answer.value, str) and Path(answer.value).is_file()):
            return True
    if unknown_fields:
        return None
    return False if known_fields else None


def greenhouse_readiness_from_payload(payload: dict, profile: Profile, settings: dict,
                                      job: Job) -> Readiness:
    """Evaluate only unconditional questions explicitly marked required."""
    if not isinstance(payload, dict) or not isinstance(payload.get("questions"), list):
        return Readiness("unknown")
    missing: list[str] = []
    ambiguous = False
    for section in ("questions", "location_questions", "compliance"):
        questions = payload.get(section) or []
        if not isinstance(questions, list):
            ambiguous = True
            continue
        for question in questions:
            if not isinstance(question, dict) or question.get("required") is not True:
                continue
            label = strip_html(question.get("label") or "") or "Required question"
            if re.match(r"^\s*(?:if\b|when\s+you\s+(?:answered|selected)\b)", label, re.I):
                # Public metadata does not say whether a conditional field is
                # currently visible. Let the browser resolve the live branch.
                ambiguous = True
                continue
            result = _answerable(question, profile, settings, job)
            if result is False:
                missing.append(label)
            elif result is None:
                ambiguous = True

    demographics = payload.get("demographic_questions") or {}
    if isinstance(demographics, dict):
        for question in demographics.get("questions") or []:
            if not isinstance(question, dict) or question.get("required") is not True:
                continue
            label = strip_html(question.get("label") or "") or "Required demographic question"
            options = [str(option.get("label")) for option in question.get("answer_options") or []
                       if isinstance(option, dict) and option.get("label") is not None]
            answer = rule_answer(Q(label=label, kind="select", options=options, required=True),
                                 profile, settings, job)
            if answer is None:
                missing.append(label)
    elif demographics:
        ambiguous = True

    for item in payload.get("data_compliance") or []:
        if isinstance(item, dict) and any(item.get(key) is True for key in
                                          ("requires_consent", "requires_processing_consent",
                                           "requires_retention_consent")):
            # Legal consent must be completed by the candidate on the form.
            missing.append("Data processing consent")
            break
    if missing:
        return Readiness("missing_facts", tuple(dict.fromkeys(missing)))
    return Readiness("unknown" if ambiguous else "ready")


def preflight_job(job: Job, profile: Profile | None, settings: dict) -> Readiness:
    """Fetch only a validated Greenhouse API URL; fail open on transport errors."""
    if job.ats != "greenhouse" or profile is None:
        return Readiness("unknown")
    try:
        parsed = parse_job_url(job.apply_url)
        if parsed.ats != "greenhouse":
            return Readiness("unknown")
        url = (f"https://boards-api.greenhouse.io/v1/boards/"
               f"{quote(parsed.slug, safe='')}/jobs/{quote(parsed.posting_id, safe='')}")
        with client(timeout=8.0, follow_redirects=False) as http:
            response = http.get(url, params={"questions": "true"})
            response.raise_for_status()
            payload = response.json()
        return greenhouse_readiness_from_payload(payload, profile, settings, job)
    except Exception:  # noqa: BLE001
        # A public API failure is not evidence the candidate cannot apply.
        return Readiness("unknown")
