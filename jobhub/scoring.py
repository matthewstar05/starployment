"""Deterministic job scoring (0-100) with a transparent breakdown. No LLM required.

Components (max points):
  title_fit 30 | skills_fit 20 | seniority 12 | location 10 | salary 8 | freshness 8 | ease 12
`ease` measures form automation support. Scores are ranking heuristics, never
admission probabilities or estimates of future income.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from functools import lru_cache

from .models import Job, Profile

SENIOR_RE = re.compile(r"\b(senior|sr\.?|staff|principal|lead|manager|director|head|architect|distinguished)\b", re.I)
ENTRY_RE = re.compile(r"\b(new ?grad|entry[- ]level|junior|jr\.?|early[- ]career|university|graduate|associate|"
                      r"0-2 years|1\+? years?|recent graduate|campus|"
                      # Finance and consulting name their entry classes differently.
                      r"analyst program|class of 20\d\d|20\d\d (?:full[- ]time )?(?:analyst|associate)|rotational)\b",
                      re.I)
YEARS_RE = re.compile(r"(\d+)\+?\s*(?:-\s*\d+\s*)?(?:years|yrs)", re.I)
EASY_ATS = {"greenhouse": 12, "ashby": 11, "lever": 10, "smartrecruiters": 6, "workable": 10, "rippling": 10, "workday": 7,
            "icims": 3, "indeed": 5, "linkedin": 4, "unknown": 5}
CAREER_STAGE_WORDS = {"new", "grad", "graduate", "entry", "level", "junior", "jr", "early", "career"}
IT_SUPPORT_TITLE_RE = re.compile(
    r"\b(?:it|information technology)\s+(?:(?:systems?|network|infrastructure|operations|desktop)\s+)?"
    r"(?:engineer|analyst|specialist|administrator|technician)\b|"
    r"\b(?:technical|customer|product|application|desktop|field|network|it)\s+support\s+"
    r"(?:engineer|analyst|specialist|technician)\b|"
    r"\b(?:support\s+(?:engineer|specialist|technician)|help[ -]?desk|service[ -]?desk|"
    r"(?:tier|level)\s*[- ]?[123])\b", re.I)
IT_SUPPORT_TARGET_RE = re.compile(r"\b(?:it|information technology|support|help[ -]?desk|service[ -]?desk)\b", re.I)
# Explicit numbered/roman role levels are common at employers that do not use
# words such as "senior". Level II is treated as a mid-level signal for the
# unattended-application gate; level III+ has a higher experience floor unless
# the candidate explicitly targets that level.
ROLE_LEVEL_RE = re.compile(
    r"\b(?:engineer|developer|analyst|scientist|designer|accountant|consultant|"
    r"specialist|administrator|architect|product manager)\s+(ii|iii|iv|2|3|4)\b", re.I
)


@lru_cache(maxsize=65536)
def _tokens(s: str) -> frozenset[str]:
    # Cached: each job compares its title with every target title.
    return frozenset(re.findall(r"[a-z0-9+#.]{2,}", s.lower()))


def _title_match(title: str, targets: list[str]) -> float:
    best = 0.0
    title_tokens = _tokens(title)
    for target in targets:
        if target in title:
            best = max(best, 1.0)
        else:
            sought = _tokens(target)
            if sought and len(sought & title_tokens) / len(sought) >= 0.5:
                best = max(best, 0.6)
    return best


# Words too generic to show a title is the same kind of work as a target on their own:
# "Talent Acquisition Associate" shares only "associate" with "research associate", and
# "Technical Recruiter" only "technical" with "technical accounting".
GENERIC_ROLE_WORDS = frozenset("""associate associates analyst analysts consultant specialist coordinator
    representative assistant partner technical senior junior program programs operations team member staff
    level ii iii""".split())
PART_TIME_RE = re.compile(r"\b(?:part[- ]time|contract(?:or)?|temporary|temp|seasonal|per diem|freelance)\b", re.I)


def _occupational_match(title: str, targets: list[str]) -> bool:
    """A target fits when it appears in the title, or the title shares at least half of its words
    including one that names the work, not only a generic role word."""
    title_tokens = _tokens(title)
    for target in targets:
        if target in title:
            return True
        sought = _tokens(target)
        shared = sought & title_tokens
        if sought and len(shared) / len(sought) >= 0.5 and shared - GENERIC_ROLE_WORDS:
            return True
    return False


def _role_level(title: str) -> str | None:
    match = ROLE_LEVEL_RE.search(title)
    if not match:
        return None
    return {"ii": "2", "iii": "3", "iv": "4"}.get(match.group(1).casefold(), match.group(1))


def _location_matches(preference: str, location: str) -> bool:
    canonical = preference.strip().casefold()
    aliases = {
        "new york": ("nyc", "new york city"),
        "los angeles": ("l.a.", "la, ca", "la ca"),
        "san francisco": ("sf, ca", "sf ca"),
    }
    terms = (canonical, *aliases.get(canonical, ()))
    return any(
        bool(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", location.casefold()))
        for term in terms if term
    )


# Subjects too generic to show a posting wants this degree ("Bachelor of Science in Security").
_GENERIC_SUBJECTS = {"science", "sciences", "studies", "engineering", "arts", "technology", "security", "networks",
                     "design", "theory", "systems"}
_COURSE_PREFIX_RE = re.compile(r"^(?:advanced|intro(?:duction)? to|applied|intermediate)\s+", re.I)


@lru_cache(maxsize=8)
def _academic_terms(education: tuple[tuple[str, str, tuple[str, ...]], ...]) -> tuple[str, ...]:
    """Disciplines the candidate studied, matched like skills.

    Postings ask for a field of study as often as for a tool ("degree in
    Economics, Finance, or Statistics"), and a resume's skills list leans
    toward tools. Crediting degree subjects, minors, and coursework ranks
    economics and finance roles on the same footing as software ones.
    """
    terms: list[str] = []

    def add(value: str) -> None:
        value = _COURSE_PREFIX_RE.sub("", value.strip(" .;:|")).casefold()
        if not value or len(value) > 40:
            return
        words = value.split()
        if len(words) == 1 and (value in _GENERIC_SUBJECTS or not re.search(r"ics$|algorithms|probability|statistics|"
                                                                             r"databases|finance|accounting", value)):
            return
        terms.append(value)
        # "Quantitative Economics" also answers a posting that asks for "economics".
        if len(words) > 1 and words[-1] not in _GENERIC_SUBJECTS and re.search(r"ics$|finance|statistics", words[-1]):
            terms.append(words[-1])

    for degree, field, details in education:
        subject = field or (re.search(r".*\b(?:in|of)\s+([a-z &,]+)$", degree, re.I) or [None, ""])[1]
        for part in re.split(r",| and | & ", subject or ""):
            add(part)
        for line in details:
            for _label, body in re.findall(r"(minor|concentration|relevant coursework|coursework)\s*:\s*([^|]+)",
                                          line, re.I):
                for part in re.split(r",|;| & ", body):
                    add(part)
    return tuple(dict.fromkeys(terms))


@lru_cache(maxsize=4096)
def _skill_pattern(skill: str) -> re.Pattern:
    return re.compile(rf"(?<![a-z0-9]){re.escape(skill)}(?![a-z0-9])")


@lru_cache(maxsize=8)
def _skill_scanner(skills: tuple[str, ...]) -> tuple[re.Pattern | None, frozenset[str]]:
    """One pass over a posting that finds every whole-word skill.

    At each position the alternation reports one skill, so a skill that is a
    prefix of another ("java", "javascript") is checked on its own.
    """
    if not skills:
        return None, frozenset()
    overlapping = frozenset(s for s in skills if any(o != s and (o.startswith(s) or s.startswith(o)) for o in skills))
    alternatives = "|".join(re.escape(s) for s in sorted(skills, key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9])(?=({alternatives})(?![a-z0-9]))"), overlapping


def _skill_hits(skills: tuple[str, ...], text: str) -> list[str]:
    scanner, overlapping = _skill_scanner(skills)
    if scanner is None:
        return []
    found = {m.group(1) for m in scanner.finditer(text)}
    return [s for s in skills if s in found or (s in overlapping and s in text and _skill_pattern(s).search(text))]


# Description-derived signals per job, reused while the description and skills
# are unchanged: every scan re-scores the whole library, and reading thousands
# of long descriptions dominated that time.
_DESCRIPTION_SIGNALS: dict[str, tuple[tuple, tuple]] = {}


def _description_signals(job_id: str, desc: str, title: str, skills: tuple[str, ...]) -> tuple:
    key = (hash(desc), len(desc), title, skills)
    cached = _DESCRIPTION_SIGNALS.get(job_id)
    if cached is not None and cached[0] == key:
        return cached[1]
    text = desc + " " + title
    years = [int(m.group(1)) for m in YEARS_RE.finditer(desc)]
    signals = (tuple(_skill_hits(skills, text)), bool(ENTRY_RE.search(desc[:3000])),
               min(years) if years else None, "clearance" in desc)
    if len(_DESCRIPTION_SIGNALS) > 100_000:
        _DESCRIPTION_SIGNALS.clear()
    _DESCRIPTION_SIGNALS[job_id] = (key, signals)
    return signals


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def score_job(job: Job, profile: Profile | None, settings: dict) -> tuple[float, dict]:
    title = job.title.lower()
    desc = (job.description or "").lower()
    d: dict[str, float | str] = {}

    # --- title fit ---
    targets = [t.strip().lower() for t in settings.get("target_titles", []) if t.strip()]
    best = _title_match(title, targets)
    if best == 0 and re.search(r"engineer|developer|economist|actuar|trader", title):
        best = 0.35
    d["title_fit"] = round(30 * best, 1)

    # --- skills fit ---
    skills = [s.lower() for s in (profile.skills if profile else [])]
    studied = _academic_terms(tuple((e.degree, e.field, tuple(e.details)) for e in profile.education)) if profile else ()
    sought = tuple(s for s in dict.fromkeys(skills + list(studied)) if len(s) > 1)
    skill_hits, entry_in_desc, need_years, clearance = _description_signals(job.id, desc, title, sought)
    hits = list(skill_hits)
    denom = max(6, min(len(skills), 25)) if skills else 1
    d["skills_fit"] = round(20 * min(1.0, len(hits) / denom * 1.5), 1)
    d["matched_skills"] = ", ".join(hits[:12])

    # --- seniority ---
    experience_years = float(settings.get("years_experience", 0))
    sen = 8.0
    if SENIOR_RE.search(title): sen = 10 if experience_years >= 5 else 0
    if ENTRY_RE.search(title): sen = 12
    elif entry_in_desc: sen = max(sen, 10)
    if need_years is not None:
        need = need_years
        have = int(experience_years)
        if need > have + 2: sen = min(sen, 2)
        elif need > have: sen = min(sen, 6)
    d["seniority"] = sen

    # --- location ---
    from .locations import classify_location
    loc_res = classify_location(
        job.location or "",
        settings.get("preferred_locations", []),
        is_remote=job.remote,
        remote_ok=settings.get("remote_ok", True),
        favorite_keys=settings.get("favorite_locations"),
    )
    d["location"] = loc_res.score

    # --- salary ---
    minsal = int(settings.get("min_salary", 0) or 0)
    ref = job.salary_max or job.salary_min
    if not ref: sal = 4
    elif ref >= minsal * 1.5: sal = 8
    elif ref >= minsal: sal = 6.5
    elif ref >= minsal * 0.8: sal = 3
    else: sal = 0
    d["salary"] = sal

    # --- freshness ---
    dt = _parse_dt(job.posted_at)
    age_days = max(0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400) if dt else None
    fresh = 4 if age_days is None else 8 if age_days <= 2 else 6 if age_days <= 7 else 4 if age_days <= 14 else 2 if age_days <= 30 else 0
    d["freshness"] = fresh
    d["age_days"] = round(age_days, 1) if age_days is not None else None

    # --- ease / admission proxy ---
    ease = EASY_ATS.get(job.ats, 5)
    if job.raw.get("sponsorship") in ("Does Not Offer Sponsorship", "U.S. Citizenship is Required"):
        ease -= 0  # informative only; user profile decides. Keep neutral.
    if clearance: ease -= 4
    d["ease"] = max(0, ease)

    total = sum(v for k, v in d.items() if isinstance(v, (int, float)) and k != "age_days")
    d["total"] = round(total, 1)
    from .sources.base import title_wanted
    reasons = []
    if not title_wanted(job.title, settings): reasons.append("Title excluded by your preferences")
    occupational_targets = [t for t in targets if _tokens(t) - CAREER_STAGE_WORDS]
    if not _occupational_match(title, occupational_targets):
        reasons.append("Weak match to your target titles")
    if minsal >= 50_000 and PART_TIME_RE.search(job.title):
        reasons.append("Part-time, contract, or temporary role")
    if IT_SUPPORT_TITLE_RE.search(job.title) and not any(
        IT_SUPPORT_TARGET_RE.search(t) and _title_match(title, [t]) >= 0.6
        for t in occupational_targets
    ):
        reasons.append("IT/support specialization is not in your target titles")
    if loc_res.review_reason:
        reasons.append(loc_res.review_reason)
    elif loc_res.score <= 2:
        reasons.append("Location outside your preferences")
    if SENIOR_RE.search(title) and experience_years < 5: reasons.append("Senior role")
    role_level = _role_level(job.title)
    required_level_years = 3 if role_level == "2" else 5
    if role_level and experience_years < required_level_years and not any(
        _role_level(target) == role_level and _title_match(title, [target]) >= 0.6
        for target in occupational_targets
    ):
        reasons.append(f"Level {role_level} role requires {required_level_years}+ years or an explicit level target")
    if re.search(r"\bph\.?d\.?\b|doctorate", title) and not any(
        re.search(r"ph\.?d\.?|doctor", education.degree, re.I)
        for education in (profile.education if profile else [])
    ):
        reasons.append("Doctorate named in title is absent from your profile")
    if sen <= 2: reasons.append("Experience requirements exceed your target")
    if age_days is not None and age_days > 180:
        reasons.append("Posting date is over 180 days old")
    if job.salary_max and minsal and job.salary_max < minsal: reasons.append("Salary below your target")
    d["auto_apply_eligible"] = not reasons
    d["review_reasons"] = reasons
    return round(total, 1), d


def score_all(jobs: list[Job], profile: Profile | None, settings: dict) -> dict[str, tuple[float, dict]]:
    return {j.id: score_job(j, profile, settings) for j in jobs}
