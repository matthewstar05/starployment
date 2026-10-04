"""Map application-form questions to answers.

Resolve explicit saved answers, supported profile facts, and extractive factual summaries.
Unknown personal facts stay unanswered. Demographics use a decline option when available.
Canned eligibility values must be individually confirmed before use.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .models import Job, Profile

if TYPE_CHECKING:
    from datetime import date

DECLINE_RE = re.compile(r"decline|prefer not|don'?t wish|do not wish|not to (answer|say|disclose)|rather not|"
                        r"not specified|choose not|i don'?t want|do not want to (answer|disclose)", re.I)
YES_RE = re.compile(r"^\s*yes\b", re.I)
NO_RE = re.compile(r"^\s*no\b", re.I)


@dataclass
class Q:
    label: str
    kind: str                      # text | textarea | select | combobox | radio | checkbox | file | number
    options: list[str] = field(default_factory=list)
    required: bool = False
    name: str = ""
    id: str = ""


@dataclass
class Answer:
    value: Any                     # str for text, option text for choice, bool for checkbox, path for file
    source: str                    # rule | user | resume
    confidence: float = 1.0


DEGREE_MAP = [
    (r"ph\.?d|doctor", "Doctorate"), (r"\bm\.?s\.?\b|\bm\.?a\.?\b|master|mba", "Master's Degree"),
    (r"\bb\.?s\.?\b|\bb\.?a\.?\b|bachelor", "Bachelor's Degree"), (r"associate", "Associate's Degree"),
]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
US_STATES = dict(zip(
    ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY").split(),
    ("Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|District of Columbia|Florida|"
     "Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|"
     "Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|"
     "New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|"
     "South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming").split("|"),
))


def _edu(profile: Profile):
    """Current/highest entry for an education record; completed-degree prompts are separate."""
    if not profile.education:
        return None
    ranked = sorted(profile.education, key=lambda e: -_degree_rank(e.degree))
    return ranked[0] if ranked else profile.education[0]


def _degree_rank(d: str) -> int:
    d = (d or "").lower()
    if re.search(r"ph\.?d|doctor", d): return 4
    if re.search(r"\bm\.?s\.?\b|\bm\.?a\.?\b|master|mba", d): return 3
    if re.search(r"\bb\.?s\.?\b|\bb\.?a\.?\b|bachelor", d): return 2
    if re.search(r"associate", d): return 1
    return 0


def _degree_text(e) -> str:
    d = (e.degree or "").lower()
    for pat, out in DEGREE_MAP:
        if re.search(pat, d):
            return out
    return e.degree or ""


def _field_text(e) -> str:
    if e.field:
        return e.field.split("|")[0].strip()
    d = re.sub(r"^(B\.?S\.?|M\.?S\.?|B\.?A\.?|M\.?A\.?|Ph\.?D\.?|Bachelor of \w+|Master of \w+)\s*(in|of)?\s*", "", e.degree or "", flags=re.I)
    return d.split("|")[0].strip()


def _start_parts(e) -> tuple[str, str]:
    s = e.start or ""
    m = re.search(r"([A-Za-z]{3,9})\.?\s*(20\d\d)", s)
    if m:
        mon = next((M for M in MONTHS if M.lower().startswith(m.group(1).lower()[:3])), "")
        return mon, m.group(2)
    y = re.search(r"20\d\d", s)
    return "", (y.group(0) if y else "")


def missing_answer(question: dict) -> bool:
    """A prepared question that is required and has no usable answer yet.

    A required single checkbox (an acknowledgement) must be ticked: an
    unticked box cannot be submitted, so ``False`` counts as missing.
    """
    if not question.get("required") or question.get("kind") == "file":
        return False
    value = question.get("answer")
    if question.get("kind") == "checkbox" and not question.get("options") and value is False:
        return True
    return value is None or value == "" or value == []


def form_date(value: Any) -> tuple["date", bool] | None:
    """The calendar date a date-picker field should receive, and whether the day was given.

    Accepts ``05/01/2027``, ``5/1/2027``, ``2027-05-01``, ``May 1, 2027``,
    ``May 2027``, ``05/2027``, and ``2027-05`` (also inside longer text such as
    "Expected May 2027"). A month without a day becomes the 1st, which is what
    the pickers themselves do with "May 2027". Seasons ("Spring 2027") are
    ambiguous and return None.
    """
    from datetime import date

    text = " ".join(str(value or "").split())
    if not text:
        return None
    month_names = {name[:3].casefold(): index for index, name in enumerate(MONTHS, 1)}

    def build(year: str, month: int | str, day: int | str | None) -> tuple[date, bool] | None:
        try:
            return date(int(year), int(month), int(day or 1)), day is not None
        except ValueError:
            return None

    if match := re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text):
        return build(match.group(1), match.group(2), match.group(3))
    if match := re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text):
        return build(match.group(3), match.group(1), match.group(2))
    if match := re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", text):
        month = month_names.get(match.group(1)[:3].casefold())
        return build(match.group(3), month, match.group(2)) if month else None
    if match := re.search(r"\b([A-Za-z]{3,9})\.?,?\s+(\d{4})\b", text):
        month = month_names.get(match.group(1)[:3].casefold())
        return build(match.group(2), month, None) if month else None
    if match := re.search(r"\b(\d{1,2})/(\d{4})\b", text):
        return build(match.group(2), match.group(1), None)
    if match := re.search(r"\b(\d{4})-(\d{1,2})\b", text):
        return build(match.group(1), match.group(2), None)
    return None


def us_date(value: Any) -> str | None:
    """``MM/DD/YYYY`` for a date-picker answer, or None when the text is not a date."""
    parsed = form_date(value)
    return parsed[0].strftime("%m/%d/%Y") if parsed else None


def _end_parts(e) -> tuple[str, str]:
    s = e.end or ""
    m = re.search(r"([A-Za-z]{3,9})\.?\s*(20\d\d)", s)
    if m:
        mon = next((M for M in MONTHS if M.lower().startswith(m.group(1).lower()[:3])), "")
        return mon, m.group(2)
    y = re.search(r"20\d\d", s)
    return "", (y.group(0) if y else "")


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().rstrip("*:").strip()).casefold().replace("’", "'")


def _posting_is_in_us(job: Job | None) -> bool:
    """Recognize only explicit US locations and a few unambiguous city labels.

    Some ATS feeds publish only a city even when the form says "the country
    where the job is located". A candidate's target cities are not evidence
    of a particular posting's country, so keep this list deliberately small.
    """
    if job is None:
        return False
    location = _normalized(job.location)
    country_names = {"united states", "united states of america", "usa", "us", "u.s.", "u.s.a."}
    parts = [part.strip() for part in location.split(",")]
    if parts == [location] and location in country_names:
        return True
    if len(parts) == 2 and parts[0] == "remote" and parts[1] in country_names:
        return True
    city_states = {
        "san francisco": {"ca", "california"},
        "los angeles": {"ca", "california"},
        "new york": {"ny", "new york"},
        "new york city": {"ny", "new york"},
        "chicago": {"il", "illinois"},
        "seattle": {"wa", "washington"},
    }
    city = parts[0]
    if city not in city_states:
        return False
    if len(parts) == 1:
        return True
    if len(parts) == 2:
        return parts[1] in city_states[city] | country_names
    return len(parts) == 3 and parts[1] in city_states[city] and parts[2] in country_names


_QUESTION_ALIAS_REPLACEMENTS = (
    (r"\be[- ]?mail(?: address)?\b", " email "),
    (r"\b(?:phone|telephone|mobile)(?: number)?\b", " phone "),
    (r"\b(?:given|forename)\s+name\b", " first name "),
    (r"\b(?:family|surname)\s+name\b", " last name "),
    (r"\b(?:postal|postcode)\s+code\b", " zip "),
    (r"\bpostal\b", " zip "),
    (r"\b(?:university|college)\b", " school "),
    (r"\b(?:legally\s+)?authorized\s+to\s+work\b|\bwork\s+authorization\b|\bright\s+to\s+work\b",
     " work authorization "),
    (r"\b(?:require|need)\s+(?:future\s+)?sponsorship\b|\bsponsor(?:ship)?\b", " sponsorship "),
    (r"\b(?:desired|expected|minimum)\s+salary\b|\bcompensation\b|\bpay\s+expectation\b", " salary "),
    (r"\b(?:available\s+to\s+)?start\s+date\b|\bearliest\s+start\b|\bavailability\b", " start "),
    (r"\brelocation\b|\brelocate\b", " relocate "),
)
_QUESTION_FILLER_RE = re.compile(
    r"\b(?:please|kindly|enter|provide|share|tell\s+us|what\s+is|what's|"
    r"what\s+are|are\s+you|do\s+you|does\s+your|will\s+you|would\s+you|"
    r"can\s+you|could\s+you|your|the|a|an)\b", re.I
)


def _canonical_question(value: Any) -> str:
    """Make user question keys stable across ATS wording and punctuation.

    This is deliberately a conservative canonicalizer. It handles common
    labels and question framing, while leaving uncommon wording intact so two
    unrelated custom answers cannot collide accidentally.
    """
    if not isinstance(value, str):
        return ""
    text = value.replace("’", "'").casefold()
    text = re.sub(r"\(\s*(?:required|optional)\s*\)", " ", text)
    text = re.sub(r"\[[^\]]*\]|[*#:]+$", " ", text)
    for pattern, replacement in _QUESTION_ALIAS_REPLACEMENTS:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"\b(?:in|within)\s+(?:the\s+)?(?:united\s+states|u\.?\s*s\.?\s*a?\.?|us)\b", " ", text)
    text = _QUESTION_FILLER_RE.sub(" ", text)
    text = re.sub(r"[^\w+#]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _custom_answer(q: Q, fields: Any) -> Answer | None:
    """Resolve a saved field answer by label, name, id, or common alias."""
    if not isinstance(fields, dict):
        return None
    question_keys = {_canonical_question(candidate) for candidate in (q.label, q.name, q.id)}
    question_keys.discard("")
    matches = [(key, value) for key, value in fields.items()
               if _canonical_question(str(key)) in question_keys]
    # Ambiguous aliases should remain unanswered rather than choosing one.
    if len(matches) != 1:
        return None
    saved = matches[0][1]
    if q.kind == "checkbox" and isinstance(saved, str) and _attestation_label(_normalized(q.label)):
        return None
    # The settings API stores custom answers as strings. Parse only the two
    # unambiguous checkbox literals, while retaining the consent/attestation
    # guard in rule_answer below.
    if q.kind == "checkbox" and isinstance(saved, str) and saved.strip().casefold() in {"true", "false"}:
        saved = saved.strip().casefold() == "true"
    return _explicit_answer(q, saved, "user")


def _attestation_label(label: str) -> bool:
    return bool(re.search(r"agree|consent|acknowledge|certify|attest|confirm that|privacy|terms and|understand", label))


def pick_option(options: list[str], intent: str | None) -> str | None:
    """Match a known answer without guessing a school, location or yes/no option."""
    if not isinstance(intent, str) or not intent.strip():
        return None
    opts = [o for o in options if o and not re.match(r"^\s*(select|choose|--|please select)", o, re.I)]
    wanted = _normalized(intent)
    exact = [o for o in opts if _normalized(o) == wanted]
    if len(exact) == 1:
        return exact[0]
    # ATS catalogs sometimes replace commas with dashes in the same proper
    # name. Require the entire token sequence to match exactly and uniquely.
    def words(value: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.casefold())).strip()

    normalized = [o for o in opts if words(o) == words(wanted)]
    if len(normalized) == 1:
        return normalized[0]
    # Greenhouse's country selector uses calling-code suffixes such as
    # "United States +1". Keep this narrow so unrelated prefix matches do
    # not turn a candidate's country into an invented selection.
    if wanted == "united states":
        countries = [o for o in opts if re.fullmatch(r"united states(?: of america)?\s*\+1", _normalized(o))]
        if len(countries) == 1:
            return countries[0]
    if wanted == "decline":
        matches = [o for o in opts if DECLINE_RE.search(o)]
    elif wanted in {"yes", "no"}:
        matches = [o for o in opts if (YES_RE if wanted == "yes" else NO_RE).search(o)]
    else:
        return None
    return matches[0] if len(matches) == 1 else None


def _explicit_answer(q: Q, value: Any, source: str = "rule") -> Answer | None:
    if q.kind == "checkbox":
        return Answer(value, source) if isinstance(value, bool) else None
    if value is None or isinstance(value, (dict, list, bool)) or not str(value).strip():
        return None
    text = str(value).strip()
    if q.kind == "number" and not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    if q.options:
        selected = pick_option(q.options, text)
        return Answer(selected, source) if selected else None
    return Answer(text, source)


def _degree_status(entry) -> str:
    from calendar import monthrange
    from datetime import date
    today = date.today()
    month, year = _end_parts(entry)
    if not year:
        return "unknown"
    if month:
        number = MONTHS.index(month) + 1
        end = date(int(year), number, monthrange(int(year), number)[1])
    else:
        end = date(int(year), 12, 31)
    if end < today:
        return "completed"
    month, year = _start_parts(entry)
    if year and date(int(year), MONTHS.index(month) + 1 if month else 12, 1) <= today:
        return "in progress"
    return "unknown"


def _education_answer(q: Q, entry) -> Answer | None:
    if not entry:
        return None
    desired = _degree_text(entry)
    direct = _explicit_answer(q, desired)
    if direct or not q.options:
        return direct
    rank = _degree_rank(entry.degree)
    status = _degree_status(entry)
    matches = []
    for option in q.options:
        normalized = _normalized(option)
        option_status = ("in progress" if re.search(r"in progress|pursuing|expected|currently enrolled", normalized)
                         else "completed" if re.search(r"completed|earned|awarded|attained", normalized) else "unknown")
        if option_status != "unknown" and option_status != status:
            continue
        base = re.sub(r"\s*[([]?(in progress|pursuing|expected|currently enrolled|completed|earned|awarded|attained)[)\]]?", "", normalized).strip()
        # Domain-specific aliases, never token overlap with arbitrary option text.
        if re.fullmatch(r"(?:bachelor'?s?|master'?s?|associate'?s?|doctorate|ph\.?d\.?|mba)(?: degree)?", base) and _degree_rank(base) == rank:
            matches.append(option)
    return Answer(matches[0], "rule") if len(matches) == 1 else None


def rule_answer(q: Q, profile: Profile, settings: dict, job: Job | None) -> Answer | None:  # noqa: C901
    """Resolve verified facts or explicit per-question answers; leave unknowns open."""
    label = _normalized(q.label)
    answers = settings.get("answers", {})
    fields = answers.get("field_answers", {})
    custom = _custom_answer(q, fields)
    if custom:
        return custom
    # Profile custom facts are candidate-provided values and use the same
    # conservative canonical matching as settings field answers.
    custom = _custom_answer(q, getattr(profile, "custom_facts", {}))
    if custom:
        return custom
    confirmed = answers.get("confirmed_fields", [])
    if not isinstance(confirmed, list):
        confirmed = []

    def known(key: str):
        return _explicit_answer(q, answers.get(key), "user") if key in confirmed else None

    def value(text: Any):
        return _explicit_answer(q, text)

    def eligibility(key: str):
        if key in confirmed and answers.get(key) not in (None, ""):
            return known(key)
        # An explicitly offered opt-out asserts no eligibility fact.
        return value("decline") if q.options else None

    def matches(pattern: str):
        return re.search(pattern, label)

    # No general checkbox rule may accept a new attestation or legal consent.
    if q.kind == "checkbox":
        if matches(r"undergrad(?:uate)?.*discipline") and " - " in q.label:
            subject = q.label.rpartition(" - ")[-1].strip()
            bachelor_entries = [entry for entry in profile.education if _degree_rank(entry.degree) == 2]
            if any(pick_option([subject], _field_text(entry)) for entry in bachelor_entries):
                return Answer(True, "resume")
            return None
        if matches(r"subscribe|newsletter|promotional (?:emails|messages)|"
                   r"marketing (?:emails|communications|messages|updates)|text message|sms"):
            return Answer(False, "rule")
        return None
    graduation_window = re.fullmatch(
        r"i confirm that my graduation date will be either (fall|spring) (20\d\d) "
        r"or (fall|spring) (20\d\d)", label,
    )
    if graduation_window and q.kind in {"select", "radio", "combobox"}:
        entry = _edu(profile)
        month, year = _end_parts(entry) if entry else ("", "")
        month_number = MONTHS.index(month) + 1 if month in MONTHS else 0
        season = "spring" if 1 <= month_number <= 5 else "fall" if 8 <= month_number <= 12 else ""
        offered = {(graduation_window.group(1), graduation_window.group(2)),
                   (graduation_window.group(3), graduation_window.group(4))}
        return value("Yes") if season and (season, year) in offered else None
    if _attestation_label(label):
        return None
    # A follow-up field must not be filled from a generic profile fact while
    # its preceding yes/no or eligibility answer remains unknown.
    if matches(r"^if\s+(?:so|yes|applicable)\b|^in the event that\b"):
        return None
    if matches(r"gender|\bsex\b|race|ethnic|hispanic|latino|veteran|military|disabilit|handicap|sexual orientation|transgender|lgbt|pronoun"):
        if matches(r"pronoun") and "pronouns" in confirmed:
            return known("pronouns")
        return value("decline") if q.options else None
    # This saved answer establishes membership in a combined category only.
    # It cannot establish whether the candidate is a citizen specifically.
    if re.fullmatch(
        r"(?:are you\s+)?(?:either\s+)?(?:a\s+)?(?:u\.?s\.?|united states)\s+citizen\s+"
        r"(?:or|and/or)\s+(?:a\s+)?(?:lawful\s+)?(?:u\.?s\.?\s+)?permanent resident",
        label.rstrip("?. "),
    ):
        return known("us_citizen_or_permanent_resident")
    if matches(r"citizenship|citizen|nationality|clearance|\b18\b|legal age|over the age|criminal|convict|felony|non.compete|relative|family member|offer deadline|previously applied|former employee|worked (here|for us)|currently employed by"):
        return None
    if matches(r"sponsor"):
        # Inverted and compound questions require an exact saved answer.
        if matches(r"not require|do not need|without|authori[sz]ed|citizen"):
            return None
        return eligibility("requires_sponsorship")
    if matches(r"authori[sz]ed|legally (able|permitted|eligible)|eligible to work|work permit|right to work"):
        refers_to_job_country = matches(r"\b(?:country|location)\s+where\s+(?:the\s+)?job\s+is\s+located\b")
        if (matches(r"united states|\bu\.?s\.?a?\b") or
                (refers_to_job_country and _posting_is_in_us(job))) and not matches(r"not authori|unauthori"):
            return eligibility("work_authorized_us")
        return value("decline") if q.options else None
    if matches(r"in[- ]person|on[- ]site|onsite|in[- ]office|\boffice\b|hybrid|commute|remote"):
        return None
    if matches(r"relocat"):
        if matches(r"not willing|unable|unwilling|\bnyc\b|new york|chicago|seattle|"
                   r"los angeles|san francisco"):
            return None
        return known("willing_to_relocate")
    if matches(r"salary|compensation|pay expectation|desired pay"):
        return known("salary_expectation")
    if matches(r"available to start|availability|earliest|notice period|when (can|could) you"):
        return known("available_start")
    if matches(r"how did you (?:first\s+)?(?:hear|find|learn)|"
               r"where did you (?:first\s+)?(?:hear|find|learn)|"
               r"(?:please\s+)?tell us how you (?:first\s+)?(?:heard|found|learned)|"
               r"^(?:application source|job source|source|how heard)\b"):
        return known("how_heard")
    if matches(r"years? of (relevant |professional |work )?experience"):
        return known("years_experience")
    if matches(r"employment type|full[- ]time|part[- ]time|contract") and q.options:
        return known("employment_type")

    if matches(r"first.*(?:and|&)?.*last.*name"):
        return value(profile.full_name)
    if matches(r"\bfirst\s*name|given name"):
        return value(profile.first_name)
    if matches(r"\blast\s*name|family name|surname"):
        return value(profile.last_name)
    if matches(r"preferred name|nickname"):
        return known("preferred_name")
    if matches(r"^name of (?:school|university|college|institution)\b"):
        school = _edu(profile)
        return value(school.school if school else "")
    if matches(r"^(full )?name\b|legal name|your name"):
        return value(profile.full_name)
    if matches(r"e-?mail"):
        return value(profile.email)
    if matches(r"phone|mobile|telephone"):
        return value(profile.phone)
    if "linkedin" in label:
        return value(profile.linkedin)
    if "github" in label:
        return value(profile.github)
    if matches(r"\b(?:twitter|facebook|instagram|tiktok|stackoverflow|stack overflow|medium|angellist)\b|"
               r"\bx\s+(?:profile|url)\b"):
        return None
    if matches(r"portfolio|website|personal site|url|link to your") and q.kind == "text":
        return value(profile.website or profile.github)
    if matches(r"\b(?:prefer(?:red|ence)?|desired|target)\b.*\b(?:city|state|country|location|region)\b"):
        return None
    if matches(r"\bcity\b.*(?:\bstate\b|\bprovince\b|\bregion\b)|"
               r"(?:\bstate\b|\bprovince\b|\bregion\b).*\bcity\b"):
        state = profile.state or profile.region
        if not profile.city or not state:
            return None
        parts = [profile.city, state]
        if matches(r"\bcountry\b"):
            if not profile.country:
                return None
            parts.append(profile.country)
        return value(", ".join(parts))
    if matches(r"\bcountry\b"):
        return value(profile.country)
    if matches(r"\bstate\b|province|\bregion\b"):
        state = profile.state or profile.region
        if (q.kind in {"select", "combobox", "radio"} and
                profile.country.casefold().strip() in {"united states", "united states of america", "us", "usa"}):
            state = US_STATES.get(state.upper(), state)
        return value(state)
    if matches(r"\bcity\b") and not matches(r"state|country"):
        return value(profile.city)
    if matches(r"current location|where (?:are you|do you) (?:currently )?(?:located|based|live)|^location"):
        if q.kind == "combobox" and profile.city and profile.country:
            # An autocomplete may initially expose only popular choices. The
            # browser must search for the candidate's city and verify the
            # actual city/region/country option before committing it.
            return Answer(profile.location, "rule")
        return value(profile.location)
    if matches(r"address line\s*2|address\s*2|line\s*2|suite|unit|apt"):
        return value(getattr(profile, "address_line2", ""))
    if matches(r"address line\s*1|address\s*1|street address|street|^address"):
        return value(getattr(profile, "address_line1", ""))
    if matches(r"zip|postal"):
        return value(getattr(profile, "postal_code", ""))

    e = _edu(profile)
    if matches(r"all post[- ]secondary education attained|all degrees? (?:obtained|earned|completed)"):
        completed = [entry for entry in profile.education if _degree_status(entry) == "completed"]
        if not completed:
            return None
        lines = [f"{entry.school}; {entry.degree} in {_field_text(entry)} ({entry.end})"
                 for entry in completed if entry.school and entry.degree and _field_text(entry)]
        return value("\n".join(lines)) if len(lines) == len(completed) else None
    if matches(r"highest.*(completed|attained)|degree.*(completed|earned)|completed.*degree"):
        completed = [entry for entry in profile.education if _degree_status(entry) == "completed"]
        if not completed:
            return None
        return _education_answer(q, sorted(completed, key=lambda item: -_degree_rank(item.degree))[0])
    if matches(r"\bminor\b"):
        return None
    if matches(r"\bdegree\b|education level|highest level"):
        return _education_answer(q, e)
    if matches(r"discipline|major|field of study|area of study|concentration"):
        return value(_field_text(e) if e else "")
    if matches(r"gpa"):
        return value(e.gpa.split("/")[0].strip() if e and e.gpa else "")
    if matches(r"(end|graduation|grad).*(month)"):
        return value(_end_parts(e)[0] if e else "")
    if matches(r"(end|graduation|grad).*year"):
        return value(_end_parts(e)[1] if e else "")
    if matches(r"(end|graduation|grad).*date"):
        if not e:
            return None
        exact = value(e.end)
        if exact or not q.options:
            return exact
        month, year = _end_parts(e)
        month_number = MONTHS.index(month) + 1 if month in MONTHS else 0
        season = "Spring" if 1 <= month_number <= 5 else "Fall" if 9 <= month_number <= 12 else ""
        return value(f"{season} {year}") if season and year else None
    if matches(r"start.*month"):
        return value(_start_parts(e)[0] if e else "")
    if matches(r"start.*year"):
        return value(_start_parts(e)[1] if e else "")
    if matches(r"start date.*(?:school|university|college)|(?:school|university|college).*start date"):
        return value(e.start if e else "")
    if matches(r"start date"):
        return known("available_start")
    if matches(r"^school(?: name)?\s*$|name of school|university|college|institution|"
               r"where did you (study|attend)|alma"):
        return value(e.school if e else "")
    if matches(r"current (company|employer)|current title|current role|most recent (company|employer)|company name"):
        current = [item for item in profile.experience if re.search(r"present|current", item.end, re.I)]
        # Multiple current roles cannot be collapsed into one employer by guessing.
        if len(current) != 1:
            return None
        return value(current[0].title if matches(r"title|role") else current[0].company)
    if q.kind in {"textarea", "text"}:
        return factual_essay(q, profile, job)
    return None


# ---------------- LLM fallback ----------------
def profile_context(profile: Profile) -> str:
    parts = [f"Name: {profile.full_name}", f"Headline: {profile.headline}", f"Location: {profile.location}",
             f"Summary: {profile.summary}", "Skills: " + ", ".join(profile.skills[:40])]
    for e in profile.education[:3]:
        parts.append(f"Education: {e.degree} {e.field} at {e.school} ({e.start}-{e.end}) GPA {e.gpa}")
    for x in profile.experience[:4]:
        parts.append(f"Experience: {x.title} at {x.company} ({x.start}-{x.end}): " + " | ".join(x.bullets[:4]))
    for pr in profile.projects[:5]:
        parts.append(f"Project: {pr.name} ({pr.role}): " + " | ".join(pr.bullets[:3]))
    return "\n".join(parts)


def _evidence(profile: Profile) -> list[str]:
    result = []
    for item in profile.experience:
        for bullet in item.bullets:
            result.append(f"{item.title} at {item.company}: {bullet}")
    for item in profile.projects:
        for bullet in item.bullets:
            result.append(f"{item.name}: {bullet}")
    return result


def factual_essay(question: Q, profile: Profile, job: Job | None) -> Answer | None:
    """Useful local answers for broad factual prompts, using resume wording only."""
    label = _normalized(question.label)
    if re.search(r"why|motivat|interest|challenge|conflict|failure|failed|mistake|didn.?t work|"
                 r"did not work|first approach|what went wrong|changed your mind|not anticipate|"
                 r"setback|unsuccessful|grit|strength|weakness|relocat|sponsor|authori[sz]|"
                 r"citizen|agree|certif|consent", label):
        return None
    if re.search(r"(?:list|describe|what|tell).*(?:technical skills|programming languages|technologies you)", label):
        return Answer(", ".join(profile.skills), "resume") if profile.skills else None
    if not re.search(r"(?:describe|summari[sz]e|tell us about|overview of|summary of).*(?:project|technical experience|relevant experience|professional background)", label):
        return None
    evidence = _evidence(profile)
    if "project" in label:
        evidence = [f"{item.name}: {bullet}" for item in profile.projects for bullet in item.bullets]
    if not evidence:
        return None
    if job:
        terms = set(re.findall(r"[a-z0-9+#]{3,}", job.title.lower() + " " + job.description.lower()))
        evidence.sort(key=lambda text: -len(terms & set(re.findall(r"[a-z0-9+#]{3,}", text.lower()))))
    return Answer("\n\n".join(evidence[:2]), "resume")


def llm_answers(questions: list[Q], profile: Profile, job: Job | None, settings: dict) -> dict[str, Answer]:
    """Unknown personal facts must stay unknown, even when an LLM is configured.

    Broad experience prompts can use an extractive resume answer. Free-form LLM
    answers require review and are intentionally not passed to automatic submission.
    """
    out = {}
    for question in questions:
        answer = factual_essay(question, profile, job)
        if answer:
            out[question.label] = answer
    return out


def cover_letter(profile: Profile, job: Job, settings: dict) -> str:
    evidence = factual_essay(Q("Describe your relevant experience", "textarea"), profile, job)
    body = evidence.value if evidence else ("My skills include " + ", ".join(profile.skills[:10]) + "." if profile.skills else profile.headline)
    return (f"Dear {job.company} team,\n\nI am applying for the {job.title} role.\n\n"
            f"{body}\n\nThank you for considering my application.\n\nSincerely,\n{profile.full_name}")
