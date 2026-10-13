"""Candidate-owned setup and readiness rules shared by the desktop app and scheduler.

Only facts supplied by the candidate or extracted from their resume are saved.
An arbitrary question bank covers employer-specific prompts that cannot be
predicted in a fixed form; unanswered questions remain visible for review.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import db
from .models import Profile
from .profile import ingest_resume

EDITABLE_PROFILE_FIELDS = {
    "first_name", "last_name", "email", "phone", "city", "state", "region",
    "country", "address_line1", "address_line2", "postal_code", "linkedin",
    "github", "website", "headline", "summary", "custom_facts",
}


def missing_setup(profile: Profile | None, settings: dict) -> list[str]:
    """Minimum facts for an autonomous attempt, not a promise every form can submit."""
    missing: list[str] = []
    if profile is None or not profile.resume_path or not Path(profile.resume_path).is_file():
        missing.append("resume PDF")
    if profile is None:
        missing.extend(("first name", "last name", "email", "city", "country"))
    else:
        for attr, label in (("first_name", "first name"), ("last_name", "last name"),
                            ("email", "email"), ("city", "city"), ("country", "country")):
            if not getattr(profile, attr, "").strip():
                missing.append(label)
        if profile.email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", profile.email):
            missing.append("valid email")
    if not [title for title in settings.get("target_titles", []) if title.strip()]:
        missing.append("target job titles")
    if not settings.get("preferred_locations") and not settings.get("remote_ok"):
        missing.append("preferred locations")
    return missing


def save_setup(*, profile_patch: dict[str, Any] | None = None,
               settings_patch: dict[str, Any] | None = None,
               resume_pdf: str | None = None) -> tuple[Profile, dict, list[str]]:
    """Save a candidate's edits, including a free-form question and answer bank.

    The resume parser never invents current location or eligibility. A saved
    answer in a supported canned field counts as confirmed by the candidate.
    """
    profile_patch = profile_patch or {}
    settings_patch = settings_patch or {}
    extra = set(profile_patch) - EDITABLE_PROFILE_FIELDS
    if extra:
        raise ValueError("Unsupported profile fields: " + ", ".join(sorted(extra)))
    if resume_pdf:
        if not Path(resume_pdf).is_file() or Path(resume_pdf).suffix.lower() != ".pdf":
            raise ValueError("Choose an existing resume PDF")
        profile = ingest_resume(resume_pdf, use_llm=False)
    else:
        profile = db.get_profile() or Profile()
    values = profile.model_dump()
    for key, value in profile_patch.items():
        if key == "custom_facts":
            if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                    for k, v in value.items()):
                raise ValueError("Custom facts must map labels to text answers")
            values[key] = {k.strip(): v.strip() for k, v in value.items() if k.strip()}
        elif isinstance(value, str):
            values[key] = value.strip()
        else:
            raise ValueError(f"{key} must be text")
    profile = Profile.model_validate(values)

    patch = dict(settings_patch)
    answers = patch.get("answers")
    if answers is not None:
        if not isinstance(answers, dict):
            raise ValueError("Answers must be a dictionary")
        answers = dict(answers)
        for key in ("work_authorized_us", "requires_sponsorship", "willing_to_relocate"):
            value = answers.get(key)
            if isinstance(value, str) and value.strip().lower() in {"yes", "no"}:
                answers[key] = value.strip().title()
        existing = db.get_settings().get("answers", {})
        confirmed = set(existing.get("confirmed_fields", []))
        confirmed.update(answers.get("confirmed_fields", []))
        known = {"work_authorized_us", "requires_sponsorship", "salary_expectation",
                 "available_start", "how_heard", "willing_to_relocate",
                 "preferred_name", "pronouns", "years_experience"}
        confirmed.update(key for key in known if isinstance(answers.get(key), str) and answers[key].strip())
        answers["confirmed_fields"] = sorted(confirmed)
        if "field_answers" in answers:
            questions = answers["field_answers"]
            if not isinstance(questions, dict) or any(not isinstance(k, str) or not isinstance(v, (str, bool))
                                                       for k, v in questions.items()):
                raise ValueError("Question answers must map question text to text or checkbox answers")
            answers["field_answers"] = {k.strip(): v.strip() if isinstance(v, str) else v
                                        for k, v in questions.items() if k.strip()}
        patch["answers"] = answers

    # Validate preference values before the profile is committed. The profile
    # and settings share one local DB, but the nested setting schema is strict.
    from .config import merge_settings, validate_settings
    candidate_settings = validate_settings(merge_settings(db.get_settings(), patch))
    missing = missing_setup(profile, candidate_settings)
    patch["setup_complete"] = not missing
    candidate_settings = validate_settings(merge_settings(db.get_settings(), patch))
    db.save_profile(profile)
    settings = db.set_settings(patch)
    db.add_event("Candidate setup saved" + ("; ready for configured searches" if not missing else "; missing " + ", ".join(missing)))
    return profile, settings, missing


# ---------------------------------------------------------------- resume library
LIBRARY_LIMITS = {"education": 30, "experience": 200, "projects": 200, "skills": 600, "awards": 200}


def save_resume_library(library: dict[str, Any]) -> Profile:
    """Replace the resume builder's entries (education, experience, projects, skills, awards).

    These are the candidate's own words. The builder only selects and orders
    them for each role; it never rewrites them.
    """
    from .models import Education, Experience, Project

    profile = db.get_profile() or Profile()
    values = profile.model_dump()
    models = {"education": Education, "experience": Experience, "projects": Project}
    for key, model in models.items():
        if key not in library:
            continue
        rows = library[key]
        if not isinstance(rows, list) or len(rows) > LIBRARY_LIMITS[key]:
            raise ValueError(f"{key.title()} must be a list of at most {LIBRARY_LIMITS[key]} entries")
        cleaned = []
        for row in rows:
            entry = model.model_validate(row)
            for attr in ("bullets", "tech", "details"):
                if hasattr(entry, attr):
                    items = [" ".join(str(x).split())[:1200] for x in getattr(entry, attr) if str(x).strip()]
                    setattr(entry, attr, items[:40])
            if any(str(v).strip() for k, v in entry.model_dump().items() if isinstance(v, str)):
                cleaned.append(entry.model_dump())
        values[key] = cleaned
    for key in ("skills", "awards"):
        if key in library:
            items = library[key]
            if not isinstance(items, list) or len(items) > LIBRARY_LIMITS[key]:
                raise ValueError(f"{key.title()} must be a list of at most {LIBRARY_LIMITS[key]} items")
            values[key] = list(dict.fromkeys(" ".join(str(x).split())[:300] for x in items if str(x).strip()))
    profile = Profile.model_validate(values)
    db.save_profile(profile)
    db.add_event("Resume library saved")
    return profile


def merge_resume_into_library(pdf_path: str) -> tuple[Profile, dict[str, int]]:
    """Add entries parsed from another resume PDF without replacing existing ones."""
    from .profile import apply_links, extract_links, extract_text, heuristic_profile

    path = Path(pdf_path)
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise ValueError("Choose a resume PDF")
    text = extract_text(path)
    if not text.strip():
        raise ValueError("No readable text found in this PDF")
    parsed = apply_links(heuristic_profile(text), extract_links(path))
    profile = db.get_profile() or Profile()

    def key(entry, *fields):
        return tuple(" ".join(str(getattr(entry, f, "")).lower().split()) for f in fields)

    added = {"experience": 0, "projects": 0, "education": 0, "skills": 0}
    have = {key(e, "company", "title") for e in profile.experience}
    for entry in parsed.experience:
        if key(entry, "company", "title") not in have:
            profile.experience.append(entry)
            added["experience"] += 1
    have = {key(p, "name") for p in profile.projects}
    for entry in parsed.projects:
        if key(entry, "name") not in have:
            profile.projects.append(entry)
            added["projects"] += 1
    for entry in parsed.education:
        # The same degree may be saved as "B.S." and printed as "Bachelor of Science".
        same = next((e for e in profile.education if key(e, "school", "degree") == key(entry, "school", "degree")
                     or (key(e, "school", "end") == key(entry, "school", "end") and entry.end)), None)
        if same is None:
            profile.education.append(entry)
            added["education"] += 1
            continue
        for detail in entry.details:
            if detail.casefold() not in {d.casefold() for d in same.details}:
                same.details.append(detail)
                added["education"] += 1
    for award in parsed.awards:
        if award.casefold() not in {a.casefold() for a in profile.awards}:
            profile.awards.append(award)
    known = {s.casefold() for s in profile.skills}
    for skill in parsed.skills:
        if skill.casefold() not in known:
            profile.skills.append(skill)
            known.add(skill.casefold())
            added["skills"] += 1
    db.save_profile(profile)
    db.add_event(f"Imported resume entries from {path.name}")
    return profile, added
