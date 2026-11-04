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
                        r"not specified|choose not|i don'?t want|do not want to (answer|disclose)|"
                        r"undeclared|not declared|not disclosed|no answer|unspecified", re.I)
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
    if re.search(r"\bm\.?\s*s\.?\b|\bm\.?\s*a\.?\b|master|mba|\bm\.?\s*eng\.?\b", d): return 3
    if re.search(r"\bb\.?\s*s\.?\b|\bb\.?\s*a\.?\b|bachelor|\bb\.?\s*eng\.?\b", d): return 2
    if re.search(r"associate|\ba\.?\s*s\.?\b|\ba\.?\s*a\.?\b", d): return 1
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
    # Long forms first, and abbreviations only as whole words: "Master of Science in
    # Quantitative Economics" must not lose its "Ma" to the "MA" abbreviation.
    d = re.sub(r"^(?:(?:Bachelor|Master|Doctor)(?:'?s)?\s+of\s+[A-Za-z]+(?:\s+Administration)?"
               r"|(?:B\.?S|M\.?S|B\.?A|M\.?A|Ph\.?D|B\.?Eng|M\.?Eng)\.?(?=[\s,]|$))\s*,?\s*(?:in|of)?\s*",
               "", e.degree or "", flags=re.I)
    return d.split("|")[0].strip()


def _start_parts(e) -> tuple[str, str]:
    s = e.start or ""
    m = re.search(r"([A-Za-z]{3,9})\.?\s*(20\d\d)", s)
    if m:
        mon = next((M for M in MONTHS if M.lower().startswith(m.group(1).lower()[:3])), "")
        return mon, m.group(2)
    y = re.search(r"20\d\d", s)
    return "", (y.group(0) if y else "")


def _date_part(q: Q, text: str) -> Answer | None:
    """A date for a field that may be one list of months or one list of years ("Start Date" as two lists)."""
    if not text:
        return None
    if not q.options:
        return _explicit_answer(q, text)
    month = next((m for m in MONTHS if re.search(r"\b" + m[:3], text, re.I)), "")
    year = (re.search(r"(?:19|20)\d\d", text) or [""])[0]
    if any(re.fullmatch(r"\s*(?:19|20)\d\d\s*", option) for option in q.options) and year:
        picked = pick_option(q.options, year)
        return Answer(picked, "resume") if picked else None
    if month:
        for candidate in (month, month[:3], f"{MONTHS.index(month) + 1:02d}", str(MONTHS.index(month) + 1)):
            picked = pick_option(q.options, candidate)
            if picked:
                return Answer(picked, "resume")
    return _explicit_answer(q, text)


def missing_answer(question: dict) -> bool:
    """A prepared question that is required and has no usable answer yet.

    A required single checkbox (an acknowledgement) must be ticked: an
    unticked box cannot be submitted, so ``False`` counts as missing. A
    required upload other than the resume or a cover letter (a transcript,
    say) is missing too: Starployment cannot attach it, so the draft says so
    before sending instead of the send stopping at it.
    """
    if not question.get("required"):
        return False
    if question.get("kind") == "file":
        if question.get("source") == "you" and question.get("answer"):
            return False   # the candidate chose a file for it
        return question.get("source") != "file" and not re.search(r"resume|\bcv\b|cover", str(question.get("label")), re.I)
    value = question.get("answer")
    if question.get("kind") == "checkbox" and not question.get("options") and value is False:
        return True
    return value is None or value == "" or value == []


def needs_attention(question: dict) -> bool:
    """Missing, or answered but refused by the employer's form on the last try (``rejected``)."""
    return missing_answer(question) or bool(question.get("rejected"))


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


# A start-date question ("earliest start date", "when are you available") may be a date
# picker while the candidate's saved answer is a phrase such as "Flexible" or "2 weeks
# notice". Those phrases have one faithful date, counted from the day the form is sent.
AVAILABILITY_LABEL_RE = re.compile(r"\bstart|availab|\bbegin|\bjoin|notice period|earliest|commence", re.I)
_AVAILABLE_NOW_RE = re.compile(
    r"^\s*(?:available\s+)?(?:immediately|immediate|asap|as soon as possible|now|right away|any ?time|flexible|"
    r"open|whenever|today)\b", re.I)
_AVAILABLE_AFTER_RE = re.compile(r"\b(\d{1,2}|one|two|three|four|five|six|eight)\s*(?:-\s*\d{1,2}\s*)?"
                                 r"(day|week|month)s?\b", re.I)
_SMALL_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "eight": 8}


def availability_date(label: str, value: Any, today: "date | None" = None) -> tuple["date", str] | None:
    """The date a start-date picker should receive for a saved availability phrase.

    "Flexible", "Immediately", or "ASAP" mean the send date; "2 weeks notice" means two
    weeks after it. Only start-date questions qualify, and anything else returns None.
    """
    from datetime import date, timedelta

    text = " ".join(str(value or "").split())
    if not text or not AVAILABILITY_LABEL_RE.search(label or ""):
        return None
    today = today or date.today()
    if _AVAILABLE_NOW_RE.search(text):
        return today, "available now"
    match = _AVAILABLE_AFTER_RE.search(text)
    if match and len(text) <= 40:
        count = int(match.group(1)) if match.group(1).isdigit() else _SMALL_NUMBERS[match.group(1).lower()]
        days = count * {"day": 1, "week": 7, "month": 30}[match.group(2).lower()]
        return today + timedelta(days=days), f"{text}, counted from the day it is sent"
    return None


def _availability_part(q: Q, saved: Any, part: str) -> Answer | None:
    """The month or year the candidate can start work, from a saved date or availability phrase."""
    parsed = form_date(saved)
    available = availability_date("start", saved)
    when = parsed[0] if parsed else available[0] if available else None
    if when is None:
        return None
    month = MONTHS[when.month - 1]
    candidates = [str(when.year)] if part == "year" else [month, month[:3], f"{when.month:02d}", str(when.month)]
    if not q.options:
        return Answer(candidates[0], "user")
    for candidate in candidates:
        if picked := pick_option(q.options, candidate):
            return Answer(picked, "user")
    return None


FORMAT_NAMES = {"date": "a date", "number": "a number", "email": "an email address", "url": "a web address",
                "phone": "a phone number"}


def fit_answer(fmt: str | None, label: str, value: Any) -> tuple[Any, str] | None:
    """Fit an answer to a typed form field, or None when it cannot go there.

    Returns ``(value, note)``; the note says what changed. A date becomes
    MM/DD/YYYY. An availability phrase on a start-date question stays a phrase
    and is dated when the form is sent (``availability_date``). A number keeps
    the candidate's own figure as digits ("$120k" → 120000; a range gives its
    low end). A web address gains https://. Nothing is invented: an answer that
    does not contain the needed value returns None, so the draft asks for it.
    """
    if not fmt or not isinstance(value, str):
        return value, ""
    text = " ".join(value.split())
    if fmt == "date":
        if formatted := us_date(text):
            return formatted, ""
        if availability_date(label, text):
            return text, f"“{text}” is entered as a date when the application is sent"
        return None
    if fmt == "number":
        match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(k\b)?", text, re.I)
        if not match:
            return None
        number = float(match.group(1).replace(",", "")) * (1000 if match.group(2) else 1)
        digits = str(int(number)) if number.is_integer() else f"{number:g}"
        return digits, ("" if digits == text else f"Entered as {digits} from “{text}”")
    if fmt == "email":
        return (text, "") if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", text) else None
    if fmt == "url":
        if re.match(r"https?://\S+$", text, re.I):
            return text, ""
        if re.fullmatch(r"(?:www\.)?[\w-]+(?:\.[\w-]+)+(?:/\S*)?", text):
            return "https://" + text, ""
        return None
    if fmt == "phone":
        return (text, "") if len(re.sub(r"\D", "", text)) >= 7 else None
    return value, ""


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


def _custom_answer(q: Q, fields: Any, approved: bool = False) -> Answer | None:
    """Resolve a saved field answer by label, name, id, or common alias.

    ``approved`` answers were approved by the candidate for this one
    application, so "Yes"/"No" may set a checkbox, acknowledgements included
    (a form can show a yes/no question as a single box).
    """
    if not isinstance(fields, dict):
        return None
    question_keys = {_canonical_question(candidate) for candidate in (q.label, q.name, q.id)}
    question_keys.discard("")
    matches = [(key, value) for key, value in fields.items()
               if _canonical_question(str(key)) in question_keys]
    # Ambiguous aliases should remain unanswered rather than choosing one; several
    # aliases that agree (a box found by both its label and its name) are one answer.
    if not matches or len({repr(value) for _, value in matches}) != 1:
        return None
    saved = matches[0][1]
    if approved and q.kind == "checkbox" and isinstance(saved, str):
        flag = saved.strip().casefold()
        if flag in {"yes", "true"}:
            return Answer(True, "user")
        if flag in {"no", "false"}:
            return Answer(False, "user")
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
    opts = [o for o in options if o and (DECLINE_RE.search(o) or
            not re.match(r"^\s*(select|choose|--|please select)", o, re.I))]
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
    if wanted == "+1 us":
        codes = [o for o in opts if re.fullmatch(r"\+1 us(?:\s*-\s*united states)?", _normalized(o))]
        if len(codes) == 1:
            return codes[0]
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
    if q.kind == "combobox" and isinstance(value, list):
        # A multi-select shown as a searchable list (Greenhouse "select all that apply").
        picked = [str(item).strip() for item in value if str(item).strip()]
        return Answer(picked, source) if picked else None
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
        option_status = ("in progress" if re.search(r"in progress|pursuing|expected|currently enrolled|completing", normalized)
                         else "completed" if re.search(r"completed|earned|awarded|attained", normalized) else "unknown")
        if option_status != "unknown" and option_status != status:
            continue
        base = re.sub(r"\s*[([]?(in progress|pursuing|expected|currently enrolled|completing|completed|earned|awarded|attained)[)\]]?", "", normalized).strip()
        # Domain-specific aliases, never token overlap with arbitrary option text.
        if re.fullmatch(r"(?:bachelor'?s?|master'?s?|associate'?s?|doctorate|ph\.?d\.?|mba)(?: degree)?", base) and _degree_rank(base) == rank:
            matches.append(option)
    return Answer(matches[0], "rule") if len(matches) == 1 else None


TERMS_CONSENT_RE = (r"terms and conditions|terms of (?:use|service)|privacy (?:notice|policy|statement)|"
                    r"read and (?:agree|accept|consent|acknowledge)|i consent|consent to|data (?:privacy|protection)")
# Standing answers (Profile → Answers → Standing answers), each the candidate's own saved fact.
STANDING_KEYS = ("over_18", "relatives_at_employer", "non_compete", "security_clearance", "former_employee",
                 "application_terms_consent", "accuracy_certification")


def _money(text: str) -> float | None:
    """Dollars in "$100,000", "100k", or "100000" (None when there is no figure)."""
    found = re.search(r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([kK])?", text or "")
    if not found:
        return None
    value = float(found.group(1).replace(",", ""))
    return value * 1000 if found.group(2) else value


def _money_range(option: str) -> tuple[float, float] | None:
    """(low, high) for "$100,000-$124,999", "$500,000+", or "Under $50,000"; None when not a range."""
    figures = [float(f.replace(",", "")) * (1000 if k else 1)
               for f, k in re.findall(r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*([kK])?", option or "")]
    if len(figures) >= 2:
        return min(figures[:2]), max(figures[:2])
    if len(figures) == 1:
        if re.search(r"\+|or more|and above|over|greater", option, re.I):
            return figures[0], float("inf")
        if re.search(r"under|less than|below|up to", option, re.I):
            return 0.0, figures[0]
    return None


def _standing(answers: dict, confirmed: list, key: str) -> str:
    return str(answers.get(key) or "").strip().casefold() if key in confirmed else ""


def _worked_for(profile: Profile, company: str) -> bool:
    """Whether the employer appears anywhere in the resume's work history."""
    from .priority import normalize_company
    wanted = normalize_company(company or "")
    if not wanted:
        return True    # no employer name to compare: treat as possibly worked there
    for entry in getattr(profile, "experience", []) or []:
        have = normalize_company(getattr(entry, "company", "") or "")
        if have and (have == wanted or wanted in have or have in wanted):
            return True
    return False


def _standing_answer(q: Q, label: str, answers: dict, confirmed: list, profile: Profile,
                     job: Job | None) -> Answer | None:
    """Yes/no questions the candidate answered once for every employer. Choice questions only:
    a free-text box ("If yes, list them") stays with the candidate."""
    if not (q.options or q.kind in {"radio", "select", "combobox"}) or q.kind == "checkbox":
        return None

    def matches(pattern: str) -> bool:
        return bool(re.search(pattern, label))

    def saved(key: str) -> Answer | None:
        text = _standing(answers, confirmed, key)
        return _explicit_answer(q, "Yes" if text == "yes" else "No") if text in {"yes", "no"} else None

    # Inverted or conditional wording keeps the question with the candidate.
    # ("including but not limited to" is a list, not a negation.)
    plain = re.sub(r"\b(?:including )?but not limited to\b", "", label)
    if re.search(r"\bnot\b|\bnever\b|\bunder (?:the age of )?18\b|younger than|\bminor\b|^if\b|willing|"
                 r"able to obtain|eligible (?:for|to obtain)|obtain (?:a|an)\b", plain):
        return None
    if matches(r"\b18\b|legal age|age of majority|over the age|at least (?:the age of )?18"):
        return saved("over_18")
    if matches(r"relative|family member|related to (?:an? )?(?:current )?(?:employee|member)|immediate family|"
               r"spouse|domestic partner"):
        return saved("relatives_at_employer")
    if matches(r"non.?compete|non.?solicit|restrictive covenant|non.?disclosure agreement"):
        return saved("non_compete")
    if matches(r"clearance") and matches(r"\b(?:have|hold|possess|currently|active|current)\b"):
        return saved("security_clearance")
    if matches(r"previously (?:been )?(?:worked|employed)|former (?:employee|worker)|worked (?:here|for us|for (?:this|the) "
               r"company)|ever (?:been )?(?:worked|employed) (?:for|by|at|with)|(?:have|had) you (?:ever )?worked (?:for|at)|"
               r"prior (?:employee|employment with)|(?:currently|presently) (?:employed by|work(?:ing)? (?:for|at))|"
               r"current (?:employee|worker)|been employed by|accepted an offer (?:of employment )?(?:to work )?(?:at|with|from)"):
        # "No" was saved as "no, unless the employer is in my resume's work history".
        if _standing(answers, confirmed, "former_employee") == "no" and job is not None \
                and not _worked_for(profile, job.company):
            return _explicit_answer(q, "No")
        return None
    # "Have you ever worked for PwC or Deloitte?": other named firms, answered by the same saved
    # "no, unless it is in my resume" (the candidate's resume lists every employer).
    if matches(r"(?:currently|ever)[^?]{0,30}\bwork(?:ed)? (?:for|at|with)\b") and \
            _standing(answers, confirmed, "former_employee") == "no":
        firms = re.findall(r"\b[A-Z][A-Za-z&.]{1,}(?:\s+[A-Z][A-Za-z&.]+)*", q.label.split("worked", 1)[-1])
        firms = [f for f in firms if f.casefold() not in {"or", "and", "either", "any", "their", "the", "inc", "llc"}]
        if firms and not any(_worked_for(profile, firm) for firm in firms):
            return _explicit_answer(q, "No")
        return None
    if matches(r"(?:interested in|able to|willing to)[^?]{0,40}\bwork (?:at|in|from) the location"):
        # Automatic runs pick only postings in places the candidate accepts; a saved yes to
        # relocating covers the rest.
        return _explicit_answer(q, "Yes") if _standing(answers, confirmed, "willing_to_relocate") == "yes" else None
    if matches(r"\b(?:are|were) you (?:a |being )?referr(?:al|ed)\b"):
        # The candidate's own saved source says how they came to the role; a non-referral source
        # answers "were you referred" with no.
        heard = [str(answers.get(key) or "") for key in ("workday_how_heard", "how_heard") if key in confirmed]
        if heard and not any(re.search(r"referr", text, re.I) for text in heard):
            return _explicit_answer(q, "No")
        return None
    return None


def rule_answer(q: Q, profile: Profile, settings: dict, job: Job | None) -> Answer | None:  # noqa: C901
    """Resolve verified facts or explicit per-question answers; leave unknowns open."""
    label = _normalized(q.label)
    answers = settings.get("answers", {})
    fields = answers.get("field_answers", {})
    custom = _custom_answer(q, answers.get("approved_answers"), approved=True) or _custom_answer(q, fields)
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

    # Texts about the application are a contact preference, not a fact: the private default is no.
    if q.kind in {"radio", "select", "combobox"} and matches(r"text message|\bsms\b|texting|whatsapp"):
        if q.kind == "combobox" and not q.options:
            return Answer("No", "rule")   # a searchable list: the browser picks the actual "No"
        declined = pick_option(q.options, "No")
        if declined:
            return Answer(declined, "rule")
    # No general checkbox rule may accept a new attestation or legal consent. The one standing
    # exception is Workday's application-terms box, and only with the candidate's own saved
    # consent (Profile → Answers → Standing answers); a certification of accuracy never counts.
    if (q.kind == "checkbox" and job is not None and job.ats == "workday" and _standing(answers, confirmed,
                                                                                         "application_terms_consent") == "yes"
            and matches(TERMS_CONSENT_RE) and not matches(r"certif|attest|true and (?:correct|complete)|accurate|"
                                                          r"signature|e-?sign|background check|drug|arbitration")):
        return Answer(True, "user")
    # Workday answers come only from the candidate's own saved answers and resume, so with their
    # saved say-so a Workday box confirming that information is accurate may be ticked too.
    if (q.kind == "checkbox" and job is not None and job.ats == "workday"
            and _standing(answers, confirmed, "accuracy_certification") == "yes"
            and matches(r"(?:information|answers?|application|statements?)[^.]{0,80}\b(?:accurate|true|correct|complete)\b|"
                        r"\bcertify\b|\battest\b")
            and not matches(r"signature|e-?sign|arbitration|background (?:check|investigation)|drug|credit check|"
                            r"polygraph|consent to (?:a |the )?(?:background|credit|drug)")):
        return Answer(True, "user")
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
        if matches(r"still (?:a )?student|currently (?:enrolled|a student|studying)") and not q.options:
            entry = _edu(profile)
            if entry and _degree_status(entry) != "unknown":
                return Answer(_degree_status(entry) == "in progress", "resume")
        return None
    standing = _standing_answer(q, label, answers, confirmed, profile, job)
    if standing is not None:
        return standing
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
    if matches(r"gender|\bsex\b|race|racial|ethnic|hispanic|latino|veteran|military|disabilit|handicap|sexual orientation|transgender|lgbt|pronoun"):
        if matches(r"pronoun") and "pronouns" in confirmed:
            return known("pronouns")
        if q.kind == "combobox" and not q.options:
            # A searchable list shows its choices only when opened; the browser picks its
            # own "decline to answer" wording, and leaves the question if there is none.
            return Answer("decline", "rule")
        return value("decline") if q.options else None
    # This saved answer establishes membership in a combined category only.
    # It cannot establish whether the candidate is a citizen specifically.
    if re.fullmatch(
        r"(?:are you\s+)?(?:either\s+)?(?:a\s+)?(?:u\.?s\.?|united states)\s+citizen\s+"
        r"(?:or|and/or)\s+(?:a\s+)?(?:lawful\s+)?(?:u\.?s\.?\s+)?permanent resident",
        label.rstrip("?. "),
    ):
        return known("us_citizen_or_permanent_resident")
    # Export-control rules define a "U.S. person" to include every U.S. citizen and permanent
    # resident, so a confirmed yes to that combined question answers this one. A no does not
    # (refugees and asylees are U.S. persons too), so it stays with the candidate.
    if matches(r"^(?:are you\s+)?(?:a\s+)?(?:u\.?\s?s\.?|united states)\s+person\b"):
        saved = str(answers.get("us_citizen_or_permanent_resident") or "").strip().casefold()
        return value("Yes") if "us_citizen_or_permanent_resident" in confirmed and saved in {"yes", "true"} else None
    if matches(r"citizenship|citizen|nationality|clearance|\b18\b|legal age|over the age|criminal|convict|felony|non.compete|relative|family member|offer deadline|previously applied|former employee|worked (here|for us)|currently employed by"):
        return None
    if matches(r"sponsor"):
        # Inverted and compound questions require an exact saved answer.
        if matches(r"not require|do not need|without|authori[sz]ed|citizen"):
            return None
        return eligibility("requires_sponsorship")
    if matches(r"authori[sz]ed|legally (able|permitted|eligible)|eligible to work|work permit|right to work"):
        refers_to_job_country = matches(r"\b(?:country|location|jurisdiction)(?:\s+or\s+(?:jurisdiction|region|location))?"
                                        r"\s+(?:where|in which)\s+(?:the\s+|this\s+)?(?:job|position|role)\b(?:[^?]{0,60}?)"
                                        r"\s+is\s+located\b")
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
    if matches(r"salary|compensation|pay expectation|desired pay") and q.options and "pay_range_target" in confirmed:
        # A list of pay ranges: the one containing the candidate's saved figure, else none.
        target = _money(str(answers.get("pay_range_target") or ""))
        ranges = [(option, _money_range(option)) for option in q.options]
        containing = [option for option, span in ranges if span and target and span[0] <= target <= span[1]]
        if len(containing) == 1:
            return Answer(containing[0], "user")
        if any(span for _, span in ranges):
            return None
    if matches(r"salary|compensation|pay expectation|desired pay"):
        return known("salary_expectation")
    if matches(r"available to start|availability|earliest|notice period|when (can|could) you"):
        return known("available_start")
    if matches(r"how did you (?:first\s+)?(?:hear|find|learn)|"
               r"where did you (?:first\s+)?(?:hear|find|learn)|"
               r"(?:please\s+)?tell us how you (?:first\s+)?(?:heard|found|learned)|"
               r"^(?:application source|job source|source|how heard)\b"):
        # Workday's picker needs a specific source ("LinkedIn"), not a category ("Job board").
        if job is not None and job.ats == "workday" and known("workday_how_heard"):
            return known("workday_how_heard")
        return known("how_heard")
    if matches(r"\bextension\b|^ext\.?$"):
        return None   # a phone extension is never the phone number; only an exact saved answer fills it
    if matches(r"^phone (?:device )?type$|^type of phone$|^phone number type$"):
        return known("phone_type")
    if matches(r"language preference|preferred language") and matches(r"communicat|correspond|contact"):
        return known("native_language")
    if matches(r"years? (?:of )?(relevant |professional |work )?experience"):
        return known("years_experience")
    if matches(r"employment type|full[- ]time|part[- ]time|contract") and q.options:
        return known("employment_type")

    if q.kind in {"text", "textarea"} and matches(r"^(?:professional )?headline$"):
        return value(profile.headline)
    if q.kind in {"text", "textarea"} and matches(r"^(?:professional )?summary$"):
        return value(profile.summary)
    if q.kind in {"text", "textarea"} and matches(r"^cover letter$") and job is not None:
        if (answers.get("cover_letter_mode", "auto") == "auto" and
                (profile.summary or profile.headline or profile.skills or _evidence(profile))):
            return Answer(cover_letter(profile, job, settings), "resume")
        return None
    if matches(r"first.*(?:and|&)?.*last.*name"):
        return value(profile.full_name)
    if matches(r"\bfirst\s*name|given name|^prénom$") or q.name in {"firstname", "first_name"}:
        return value(profile.first_name)
    if matches(r"\blast\s*name|family name|surname|^nom de famille$") or q.name in {"lastname", "last_name"}:
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
    if matches(r"^phone country code$"):
        digits = re.sub(r"\D", "", profile.phone)
        us_country = profile.country.casefold() in {"united states", "united states of america", "usa", "us"}
        if us_country and ((len(digits) == 10 and not profile.phone.strip().startswith("+")) or
                           (len(digits) == 11 and digits.startswith("1"))):
            return value("+1 US")
        return None
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
        if q.options and profile.country:
            for opt in q.options:
                if opt.strip().casefold() in {profile.country.casefold(), "united states", "united states of america", "usa", "u.s.a."} and profile.country.casefold() in {"united states", "usa", "u.s.", "united states of america"}:
                    return Answer(opt, "profile")
        return value(profile.country)
    if matches(r"\bstate\b|province|\bregion\b"):
        state = profile.state or profile.region
        if (q.kind in {"select", "combobox", "radio"} and
                profile.country.casefold().strip() in {"united states", "united states of america", "us", "usa"}):
            full_name = US_STATES.get(state.upper(), state)
            if q.options:
                for opt in q.options:
                    if opt.strip().casefold() in {state.casefold(), full_name.casefold()}:
                        return Answer(opt, "profile")
            state = full_name
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
    # Fields inside a form's education block arrive as "Education Start Date" and so on.
    if matches(r"^education\b.*\b(?:start date|from)$"):
        return _date_part(q, e.start if e else "")
    if matches(r"still (?:a )?student|currently (?:enrolled|a student|studying)"):
        if not e or _degree_status(e) == "unknown":
            return None
        studying = _degree_status(e) == "in progress"
        if q.kind == "checkbox" and not q.options:
            return Answer(studying, "resume")
        return value("Yes" if studying else "No")
    # The date on a signature line (Lever's disability self-identification form) is the day
    # the form is completed.
    if re.search(r"signature_?date", q.name or "", re.I) or matches(r"^(?:today'?s date|date signed|signature date)$"):
        from datetime import date
        return value(date.today().strftime("%m/%d/%Y"))
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
    if matches(r"current affiliation with your university|university affiliation|affiliation with (?:your )?university"):
        if e and _degree_status(e) == "in progress":
            return _education_answer(q, e)
        return None
    if matches(r"\bminor\b"):
        return None
    if matches(r"(?:year|month).*(?:complete|expect|finish).*(?:degree|education)|(?:complete|expect|finish).*(?:degree|education).*(?:year|month)"):
        if matches(r"month"):
            return value(_end_parts(e)[0] if e else "")
        return value(_end_parts(e)[1] if e else "")
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
    if matches(r"(end|graduation|grad).*date") and q.options and _date_part(q, e.end if e else ""):
        return _date_part(q, e.end if e else "")   # a month or year list
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
    # When the candidate can start work, split into a month and a year ("ft_employment_start_month").
    if matches(r"start.*(?:month|year)") and matches(r"employ|full.?time|\bft\b|^ft_|\bjob\b|\bwork|position|join") \
            and not matches(r"school|universit|college|degree|education|stud"):
        saved = answers.get("available_start") if "available_start" in confirmed else None
        return _availability_part(q, saved, "month" if matches(r"start.*month") else "year")
    if matches(r"start.*month"):
        return value(_start_parts(e)[0] if e else "")
    if matches(r"start.*year"):
        return value(_start_parts(e)[1] if e else "")
    if matches(r"start date.*(?:school|university|college)|(?:school|university|college).*start date"):
        return value(e.start if e else "")
    if matches(r"start date"):
        return known("available_start")
    if matches(r"^(?:education )?school(?: name)?\s*$|name of school|university|college|institution|"
               r"where did you (study|attend)|alma"):
        return value(e.school if e else "")
    # Greenhouse's employment block ("company-name-0", "title-0") lists past employers too:
    # the most recent one (the resume's first entry) is a true answer.
    if re.search(r"(?:^|-)(?:company-name|title)-+\d+$", q.id or q.name or "") and profile.experience:
        recent = next((item for item in profile.experience if not item.hidden), None)
        if recent:
            return value(recent.title if re.search(r"title", q.id or q.name or "") else recent.company)
    if matches(r"current (company|employer)|current title|current role|most recent (company|employer)|company name"):
        current = [item for item in profile.experience if re.search(r"present|current", item.end, re.I)]
        if not current and matches(r"most recent|previous|last (company|employer)"):
            # "Current or most recent employer": the resume's first entry, as in the employment block.
            current = [item for item in profile.experience if not item.hidden][:1]
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


# Skill families a question can ask about. A skill belongs to a family when its
# name contains one of these words (case-insensitive).
_SKILL_FAMILIES = {
    "ai": ("pytorch", "tensorflow", "keras", "scikit", "sklearn", "llm", "openai", "langchain", "hugging",
           "transformers", "autogluon", "econml", "xgboost", "lightgbm", "jax", "machine learning", "nlp",
           "computer vision", "rag", "vector"),
    "cloud": ("aws", "gcp", "google cloud", "azure", "docker", "kubernetes", "terraform", "firebase", "vercel",
              "lambda", "cloud", "heroku", "netlify"),
    "frontend": ("react", "vue", "angular", "svelte", "next", "vite", "html", "css", "tailwind", "typescript",
                 "javascript", "redux"),
    "backend": ("node", "express", "django", "flask", "fastapi", "spring", "rails", "graphql", "rest",
                "postgres", "mysql", "mongodb", "redis", "kafka", "grpc"),
    "data": ("sql", "pandas", "numpy", "spark", "airflow", "dbt", "snowflake", "tableau", "tidyverse", "r",
             "statsmodels", "matplotlib", "excel", "bigquery"),
}
_FAMILY_WORDS = {
    "ai": r"\bai\b|artificial intelligence|machine learning|\bml\b|deep learning|\bllms?\b|model",
    "cloud": r"cloud|aws|gcp|azure|devops|infrastructure|deploy",
    "frontend": r"front[- ]?end|\bui\b|web|react",
    "backend": r"back[- ]?end|server|api|database",
    "data": r"\bdata\b|analytics|statistic|sql",
}
_LANGUAGES = {"python", "r", "sql", "c", "c++", "c#", "java", "javascript", "typescript", "go", "rust", "scala",
              "bash", "swift", "kotlin", "ruby", "php", "sas", "matlab", "html", "css"}


def relevant_skills(profile: Profile, job: Job | None, question: str = "", limit: int = 5) -> list[str]:
    """The few skills a person would actually name for this question and role.

    A reviewer reads a short, relevant answer; a list of every skill reads as
    machine-made. Skills the question's topic and the posting mention come
    first, then the candidate's own order. Frameworks questions skip bare
    languages.
    """
    skills = [skill for skill in dict.fromkeys(profile.skills) if skill.strip()]
    if not skills:
        return []
    text = (question or "").casefold()
    posting = f"{job.title} {job.description}".casefold() if job else ""
    families = [name for name, pattern in _FAMILY_WORDS.items() if re.search(pattern, text)]
    frameworks = bool(re.search(r"framework|librar|tools?\b", text))

    def mentioned(skill: str, haystack: str) -> bool:
        return bool(haystack) and re.search(r"(?<![a-z0-9])" + re.escape(skill.casefold()) + r"(?![a-z0-9])",
                                            haystack) is not None

    def in_family(skill: str) -> bool:
        name = skill.casefold()
        return any(re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", name)
                   for family in families for word in _SKILL_FAMILIES[family])

    scored = []
    for index, skill in enumerate(skills):
        if frameworks and skill.casefold() in _LANGUAGES:
            continue
        if families and not in_family(skill) and not mentioned(skill, text):
            continue   # off-topic: a cloud question is not answered with React
        score = 3 * in_family(skill) + 2 * mentioned(skill, posting) + mentioned(skill, text)
        scored.append((-score, index, skill))
    picked = [skill for _, _, skill in sorted(scored)][:limit]
    # A topic question with no matching skills gets none, never the first few on the list.
    return picked if families else picked or skills[:limit]


def natural_list(items: list[str]) -> str:
    """``Python, SQL, and C++`` the way a person writes a short list."""
    items = [str(item) for item in items if str(item).strip()]
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def factual_essay(question: Q, profile: Profile, job: Job | None) -> Answer | None:
    """Useful local answers for broad factual prompts, using resume wording only."""
    label = _normalized(question.label)
    if re.search(r"why|motivat|interest|challenge|conflict|failure|failed|mistake|didn.?t work|"
                 r"did not work|first approach|what went wrong|changed your mind|not anticipate|"
                 r"setback|unsuccessful|grit|strength|weakness|relocat|sponsor|authori[sz]|"
                 r"citizen|agree|certif|consent", label):
        return None
    if re.search(r"(?:list|describe|what|tell).*(?:technical skills|programming languages|technologies you)", label):
        picked = relevant_skills(profile, job, question.label, limit=5)
        return Answer(natural_list(picked), "resume") if picked else None
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
