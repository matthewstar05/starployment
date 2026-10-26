"""Prepare applications for review, then send the ones the candidate approves.

Preparing a job reads the employer's public form definition (Greenhouse,
Lever, and Ashby publish one), fills every answer that saved facts can supply,
asks Luna to draft the open-ended ones, and builds the resume for that role.
Nothing is sent while preparing. Sending goes through ``scheduler.apply_one``,
so the browser engine stays the authority on the live form, and every
submission still crosses the durable submit boundary.
"""
from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from . import config, db
from .answers import FORMAT_NAMES, Q, fit_answer, missing_answer as missing, needs_attention, rule_answer
from .models import Job, Profile
from .sources.base import client, strip_html
from .sources.import_job import parse_job_url

SUPPORTED_ATS = ("greenhouse", "lever", "ashby", "workable", "rippling")
# Prepared for review like the others. Sent through Finish in browser, or by Approve & send,
# Send ready, and automatic runs once Settings → Workday → Send Workday applications is on
# (apply/workday.py signs in with the vault's saved password).
BROWSER_ONLY_ATS = ("workday",)
PREPARABLE_ATS = SUPPORTED_ATS + BROWSER_ONLY_ATS


def sends_unattended(ats: str, settings: dict) -> bool:
    """Whether Approve & send, Send ready, and automatic runs may send this kind of application."""
    return ats not in BROWSER_ONLY_ATS or bool((settings.get("workday") or {}).get("send"))
SOURCE_LABELS = {"saved": "Saved answer", "profile": "From your profile", "resume": "From your resume",
                 "luna": "Drafted by Luna", "autofill": "AI Autofill", "you": "Your answer", "none": "Needs your answer",
                 "declined": "Declined", "file": "Attached"}


def blank(value) -> bool:
    """No answer yet. A multi-select answer is a list; an empty list is no answer."""
    return value is None or value == "" or value == []


def _group_options(questions: list[dict]) -> dict[str, set[str]]:
    """Each multi-choice question's label mapped to its option labels (case-folded)."""
    return {_tidy(q["label"]).casefold(): {_tidy(o).casefold() for o in q.get("options") or []}
            for q in questions if q.get("options") and q.get("kind") in ("checkbox", "radio", "select")}


def _covered_by_group(label: str, groups: dict[str, set[str]]) -> bool:
    """True for one option of a known group, as a live form reports it ("PhD" or "Degree Type - PhD")."""
    text = _tidy(label).casefold()
    for group, options in groups.items():
        if text in options or any(text == f"{group} - {option}" for option in options):
            return True
    return False


def drop_option_duplicates(questions: list[dict]) -> list[dict]:
    """Remove single-checkbox rows that repeat an option of a "select all that apply" group.

    The group question holds the answer (a list of options). A copy of one
    option, marked required because its group is, would demand every option
    and conflict with the group's choice.
    """
    groups = _group_options(questions)
    return [q for q in questions
            if q.get("options") or q.get("kind") != "checkbox" or not _covered_by_group(q["label"], groups)]


def repair_drafts() -> int:
    """Clean option copies out of open drafts prepared by earlier versions. Returns drafts changed."""
    changed = 0
    for draft in db.list_drafts(db.DRAFT_OPEN + ("practice", "error")):
        if draft["status"] in ("sending", "approved"):
            continue
        questions = drop_option_duplicates(list(draft["questions"]))
        if len(questions) == len(draft["questions"]):
            continue
        status = draft["status"] if draft["status"] in ("practice", "error", "preparing") else draft_status(questions)
        db.update_draft(draft["id"], questions=questions, status=status)
        changed += 1
    return changed


def expand_answer(question: dict, value) -> dict[str, object]:
    """Form-ready answers for one question.

    A "select all that apply" group is filled as one checkbox per option, which
    the browser engine sees as ``"<question> - <option>"``.
    """
    if question.get("kind") == "checkbox" and question.get("options") and isinstance(value, list):
        chosen = {_tidy(v).casefold() for v in value}
        return {f"{question['label']} - {_tidy(option)}": _tidy(option).casefold() in chosen
                for option in question["options"]}
    return {question["label"]: value}


def send_answers(question: dict, value) -> dict[str, object]:
    """Answers for one approved application, covering how its live form may render the question.

    Only for sending (never the shared answer bank): a "select all that apply"
    question may appear as a searchable multi-select list, and a lone box may
    carry only its own text as a label.
    """
    answers = expand_answer(question, value)
    if question.get("kind") == "checkbox" and question.get("options") and isinstance(value, list):
        answers[question["label"]] = [_tidy(v) for v in value]
        if len(question["options"]) == 1:
            option = _tidy(question["options"][0])
            answers.setdefault(option, option.casefold() in {_tidy(v).casefold() for v in value})
    return answers


def _tidy(text) -> str:
    """Option text as a form shows it: trimmed, spaces collapsed ("Open to Anything "), and at
    most 400 characters, the length the browser engine reads a label to."""
    return " ".join(str(text).split())[:400]


def _clean_value(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return value


def apply_edits(questions: list[dict], edits: dict[str, object]) -> list[dict]:
    """Record a reviewer's edits on a draft's questions. Edited answers are approved."""
    for question in questions:
        if question["id"] not in edits:
            continue
        value = _clean_value(edits[question["id"]])
        if blank(value):
            question.update(answer=None, source="none", approved=False)
        elif value != question.get("answer") or question.get("source") != "you":
            question.update(answer=value, source="you", approved=True)
            question.pop("rejected", None)
    return questions


def _shareable(question: dict, value, job_company: str) -> bool:
    """Answers the candidate gave that can answer the same question elsewhere.

    Legal acknowledgements and consent stay per application. Essays that name
    the employer, or run long, are specific to their job.
    """
    from .answers import _attestation_label, _normalized
    if question["kind"] == "file" or blank(value) or _attestation_label(_normalized(question["label"])):
        return False
    if isinstance(value, (bool, list)) or question.get("options"):
        return True
    text = str(value)
    return len(text) <= 160 and job_company.casefold() not in text.casefold() and "\n" not in text


def share_answers(draft_id: int) -> int:
    """Save this draft's general answers to the question bank and fill the same questions in other drafts.

    Returns how many questions in other prepared applications were filled.
    """
    draft = db.get_draft(draft_id)
    if draft is None:
        return 0
    settings = db.get_settings()
    company = str(draft.get("company") or "")
    given = {q["label"]: q for q in draft["questions"]
             if q.get("source") == "you" and _shareable(q, q.get("answer"), company)}
    if not given:
        return 0
    if (settings.get("luna") or {}).get("remember_approved", True):
        answers = dict(settings.get("answers") or {})
        bank = dict(answers.get("field_answers") or {})
        for label, question in given.items():
            for key, value in expand_answer(question, question["answer"]).items():
                bank[key] = value if isinstance(value, bool) else str(value)
            if isinstance(question["answer"], list):
                bank[label] = ", ".join(question["answer"])
        answers["field_answers"] = bank
        db.set_settings({"answers": answers})
    from .answers import _canonical_question
    by_key = {_canonical_question(label): q for label, q in given.items()}
    filled = 0
    for other in db.list_drafts(("ready", "needs_answers", "practice", "error")):
        if other["id"] == draft_id:
            continue
        changed = False
        for question in other["questions"]:
            source = by_key.get(_canonical_question(question["label"]))
            if source is None or not blank(question.get("answer")) or question["kind"] != source["kind"]:
                continue
            value = source["answer"]
            if question.get("options"):
                from .answers import pick_option
                values = value if isinstance(value, list) else [value]
                picked = [pick_option(question["options"], str(v)) for v in values]
                if any(p is None for p in picked):
                    continue
                value = picked if isinstance(source["answer"], list) else picked[0]
            question.update(answer=value, source="you", approved=True, note="Your answer from another application")
            changed = True
            filled += 1
        if changed:
            status = other["status"] if other["status"] in ("practice", "error") else draft_status(other["questions"])
            db.update_draft(other["id"], questions=other["questions"], status=status)
    return filled


def reanswer_open_drafts() -> int:
    """Re-fill open drafts from saved facts after the candidate changes their profile or answers."""
    profile = db.get_profile()
    if profile is None:
        return 0
    settings = db.get_settings()
    updated = 0
    for draft in db.list_drafts(("ready", "needs_answers")):
        job = db.get_job(draft["job_id"])
        if job is None:
            continue
        before = [q.get("answer") for q in draft["questions"]]
        answer_questions(draft["questions"], profile, settings, job)
        if [q.get("answer") for q in draft["questions"]] != before:
            db.update_draft(draft["id"], questions=draft["questions"], status=draft_status(draft["questions"]))
            updated += 1
    return updated


def _question(index: int, label: str, kind: str, options: list[str] | None = None,
              required: bool = False, name: str = "") -> dict:
    return {"id": f"q{index}", "label": " ".join(strip_html(label).split())[:400], "kind": kind,
            "options": [_tidy(o) for o in (options or []) if str(o).strip()][:200],
            "required": bool(required), "name": name, "answer": None, "source": "none",
            "approved": False, "note": ""}


# ------------------------------------------------------------------ form discovery
_GH_KINDS = {"input_text": "text", "textarea": "textarea", "multi_value_single_select": "select",
             "multi_value_multi_select": "checkbox", "input_file": "file"}


def greenhouse_questions(job: Job) -> list[dict]:
    parsed = parse_job_url(job.apply_url or job.url)
    url = (f"https://boards-api.greenhouse.io/v1/boards/{quote(parsed.slug, safe='')}"
           f"/jobs/{quote(parsed.posting_id, safe='')}")
    with client(timeout=12.0, follow_redirects=False) as http:
        response = http.get(url, params={"questions": "true"})
        response.raise_for_status()
        payload = response.json()
    out: list[dict] = []
    for section in ("questions", "location_questions"):
        for question in payload.get(section) or []:
            if not isinstance(question, dict):
                continue
            for field in question.get("fields") or []:
                kind = _GH_KINDS.get(str(field.get("type")))
                if not kind:
                    continue
                options = [v.get("label") for v in field.get("values") or [] if isinstance(v, dict)]
                out.append(_question(len(out) + 1, question.get("label") or field.get("name") or "",
                                     kind, options, question.get("required") is True, str(field.get("name") or "")))
                break   # the first visible field is the answer; alternates (e.g. pasted resume) are optional
    demographics = payload.get("demographic_questions") or {}
    for question in (demographics.get("questions") or []) if isinstance(demographics, dict) else []:
        options = [o.get("label") for o in question.get("answer_options") or [] if isinstance(o, dict)]
        out.append(_question(len(out) + 1, question.get("label") or "", "select", options,
                             question.get("required") is True))
    # Greenhouse adds a required consent box under its demographic survey; its
    # wording is standard and names the employer. Reading it ahead means it is
    # answered before sending instead of stopping the send.
    compliance = [item for item in payload.get("data_compliance") or [] if isinstance(item, dict)]
    if any(item.get("requires_consent") or item.get("requires_processing_consent") for item in compliance):
        # GDPR boards add a required data-processing consent box with standard wording.
        company = str(payload.get("company_name") or job.company).strip()
        out.append(_question(len(out) + 1, f"By checking this box, I agree to allow {company} to store and process my "
                             "data for the purpose of considering my eligibility regarding my current application "
                             "for employment.", "checkbox", [], True))
    if any(item.get("demographic_data_consent_applies") for item in compliance):
        company = str(payload.get("company_name") or job.company).strip()
        out.append(_question(len(out) + 1, f"By checking this box, I consent to {company} collecting, storing, and "
                             "processing my responses to the demographic data surveys above.", "checkbox", [], True))
    return out


_LEVER_KINDS = {"text": "text", "textarea": "textarea", "multiple-choice": "radio", "dropdown": "select",
                "multiple-select": "checkbox", "file-upload": "file", "university": "combobox"}
_LEVER_STANDARD = (("Resume/CV", "file", True, "resume"), ("Full name", "text", True, "name"),
                   ("Email", "text", True, "email"), ("Phone", "text", False, "phone"),
                   ("Current location", "combobox", False, "location"), ("Current company", "text", False, "org"),
                   ("LinkedIn URL", "text", False, "urls[LinkedIn]"), ("Twitter URL", "text", False, "urls[Twitter]"),
                   ("GitHub URL", "text", False, "urls[GitHub]"), ("Portfolio URL", "text", False, "urls[Portfolio]"),
                   ("Other website", "text", False, "urls[Other]"))


def lever_cards(page: str) -> list[str]:
    """The JSON of each custom-question card on a Lever application page.

    Cards sit in hidden inputs named ``cards[<id>][baseTemplate]``. Lever does
    not escape ">" inside the JSON, so this reads attributes with an HTML
    parser rather than a pattern that stops at the first ">".
    """
    from html.parser import HTMLParser

    cards: list[str] = []

    class Cards(HTMLParser):
        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == "input" and re.fullmatch(r"cards\[[^\]]+\]\[baseTemplate\]", values.get("name") or ""):
                cards.append(values.get("value") or "")

    Cards(convert_charrefs=True).feed(page)
    return cards


def lever_location_choices(page: str) -> list[str]:
    """Options of Lever's "Which location are you applying for?" list, when the posting has one."""
    from html.parser import HTMLParser

    choices: list[str] = []

    class Locations(HTMLParser):
        inside = False
        option = False

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == "select":
                self.inside = values.get("name") == "opportunityLocationId"
            self.option = self.inside and tag == "option" and bool(values.get("value"))

        def handle_endtag(self, tag):
            if tag == "select":
                self.inside = False
            if tag == "option":
                self.option = False

        def handle_data(self, data):
            if self.option and data.strip():
                choices.append(data.strip())

    Locations(convert_charrefs=True).feed(page)
    return choices


def lever_questions(job: Job) -> list[dict]:
    parsed = parse_job_url(job.apply_url or job.url)
    host = "jobs.eu.lever.co" if ".eu." in (job.apply_url or job.url) else "jobs.lever.co"
    url = f"https://{host}/{quote(parsed.slug, safe='')}/{quote(parsed.posting_id, safe='')}/apply"
    with client(timeout=15.0) as http:
        response = http.get(url)
        response.raise_for_status()
        page = response.text
    out: list[dict] = []
    for label, kind, required, name in _LEVER_STANDARD:
        if name in ("resume", "name", "email") or f'name="{name}"' in page:
            out.append(_question(len(out) + 1, label, kind, [], required, name))
    location = lever_location_choices(page)
    if location:
        # Postings open in several places ask which one; the choice is the candidate's.
        out.append(_question(len(out) + 1, "Which location are you applying for?", "select", location, True,
                             "opportunityLocationId"))
    for raw in lever_cards(page):
        try:
            card = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for field in card.get("fields") or []:
            kind = _LEVER_KINDS.get(str(field.get("type")))
            if not kind:
                continue
            options = [o.get("text") for o in field.get("options") or [] if isinstance(o, dict)]
            out.append(_question(len(out) + 1, field.get("text") or "", kind, options, field.get("required") is True))
    return out


_ASHBY_QUERY = """query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) {
    id
    applicationForm { sections { title fieldEntries { ... on FormFieldEntry { id field isRequired } } } }
  }
}"""
_ASHBY_KINDS = {"String": "text", "Email": "text", "Phone": "text", "SocialLink": "text", "Url": "text", "Number": "text",
                "Date": "text", "LongText": "textarea", "Boolean": "radio", "ValueSelect": "select",
                "MultiValueSelect": "checkbox", "File": "file", "Location": "combobox"}
# Ashby validates these types in the browser; a date picker takes only MM/DD/YYYY.
_ASHBY_FORMATS = {"Date": "date", "Number": "number", "Email": "email", "Phone": "phone", "Url": "url",
                  "SocialLink": "url"}


def ashby_questions(job: Job) -> list[dict]:
    parsed = parse_job_url(job.apply_url or job.url)
    with client(timeout=15.0) as http:
        response = http.post("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting", json={
            "operationName": "ApiJobPosting", "query": _ASHBY_QUERY,
            "variables": {"organizationHostedJobsPageName": parsed.slug, "jobPostingId": parsed.posting_id}})
        response.raise_for_status()
        data = response.json()
    posting = (data.get("data") or {}).get("jobPosting") or {}
    out: list[dict] = []
    for section in ((posting.get("applicationForm") or {}).get("sections") or []):
        for entry in section.get("fieldEntries") or []:
            field = entry.get("field") if isinstance(entry, dict) else None
            if not isinstance(field, dict):
                continue
            kind = _ASHBY_KINDS.get(str(field.get("type")))
            if not kind:
                continue
            options = ["Yes", "No"] if kind == "radio" else [
                v.get("label") for v in field.get("selectableValues") or [] if isinstance(v, dict)]
            out.append(_question(len(out) + 1, field.get("title") or "", kind, options,
                                 entry.get("isRequired") is True, str(field.get("path") or "")))
            if fmt := _ASHBY_FORMATS.get(str(field.get("type"))):
                out[-1]["format"] = fmt   # a typed field: its answer is checked before sending
    return out


def workable_questions(job: Job) -> list[dict]:
    url = job.apply_url or job.url
    m = re.search(r"/j/([0-9a-zA-Z]+)", url)
    if not m:
        raise ValueError("Could not extract Workable job shortcode from URL")
    shortcode = m.group(1)
    form_url = f"https://apply.workable.com/api/v1/jobs/{quote(shortcode, safe='')}/form"
    with client(timeout=15.0) as http:
        response = http.get(form_url)
        response.raise_for_status()
        sections = response.json()
    out: list[dict] = []

    def handle_field(field: dict) -> None:
        if not isinstance(field, dict):
            return
        ftype = str(field.get("type") or "text").lower()
        fid = str(field.get("id") or "")
        flabel = str(field.get("label") or fid)
        required = field.get("required") is True
        if ftype == "group":
            if required:
                raise ValueError("Required Workable history group needs review on the live form")
            return
        if ftype == "file":
            out.append(_question(len(out) + 1, flabel, "file", [], required, fid))
        elif ftype == "boolean":
            out.append(_question(len(out) + 1, flabel, "radio", ["Yes", "No"], required, fid))
        elif ftype in ("choice", "dropdown", "select") or ("options" in field and isinstance(field["options"], list)):
            options = []
            for opt in field.get("options") or []:
                if isinstance(opt, dict):
                    options.append(str(opt.get("label") or opt.get("name") or opt.get("value") or ""))
                elif isinstance(opt, str):
                    options.append(opt)
            kind = "select" if field.get("singleOption", True) else "checkbox"
            out.append(_question(len(out) + 1, flabel, kind, options, required, fid))
        elif ftype in ("paragraph", "textarea"):
            out.append(_question(len(out) + 1, flabel, "textarea", [], required, fid))
        elif ftype == "number":
            q = _question(len(out) + 1, flabel, "text", [], required, fid)
            q["format"] = "number"
            out.append(q)
        elif ftype == "email":
            q = _question(len(out) + 1, flabel, "text", [], required, fid)
            q["format"] = "email"
            out.append(q)
        elif ftype == "phone":
            q = _question(len(out) + 1, flabel, "text", [], required, fid)
            q["format"] = "phone"
            out.append(q)
        elif ftype == "date":
            q = _question(len(out) + 1, flabel, "text", [], required, fid)
            q["format"] = "date"
            out.append(q)
        else:
            out.append(_question(len(out) + 1, flabel, "text", [], required, fid))

    for section in (sections if isinstance(sections, list) else []):
        if not isinstance(section, dict):
            continue
        for field in section.get("fields") or []:
            handle_field(field)
    if not out:
        raise ValueError("The public Workable form contained no fields")
    return out


def rippling_questions(job: Job) -> list[dict]:
    parsed = parse_job_url(job.apply_url or job.url)
    if parsed.ats != "rippling":
        raise ValueError("Expected a public Rippling posting")
    url = f"https://ats.rippling.com/api/v1/board/{quote(parsed.slug, safe='')}/jobs/{quote(parsed.posting_id, safe='')}"
    with client(timeout=15.0) as http:
        response = http.get(url)
        response.raise_for_status()
        payload = response.json()
    if str(payload.get("uuid") or "") != parsed.posting_id:
        raise ValueError("Rippling posting does not match this vacancy")
    form = payload.get("activeJobApplication") or {}
    out = []
    for field in form.get("basicQuestions") or []:
        field_type = str(field.get("fieldType") or "").upper()
        kind = "file" if field_type == "FILE" else "text"
        q = _question(len(out) + 1, field.get("title") or "", kind, [], field.get("required") is True,
                      str(field.get("oid") or ""))
        if field_type == "PHONE_NUMBER":
            q["format"] = "phone"
        elif field.get("oid") == "email":
            q["format"] = "email"
        out.append(q)
    for section in form.get("additionalQuestions") or []:
        for field in (section.get("form") or {}).get("questions") or []:
            field_type = str(field.get("questionType") or "").upper()
            options = [str(v) for v in field.get("strChoices") or []]
            if field_type == "YES_NO_SCALE_4":
                options = ["Strong No", "No", "Yes", "Strong Yes"]
            kind = ("checkbox" if field_type == "MULTI_SELECT_CHECKBOX" else "select" if options
                    else "file" if field_type == "FILE" else "textarea" if field_type == "LONG_ANSWER" else "text")
            q = _question(len(out) + 1, field.get("title") or "", kind, options,
                          field.get("isRequired") is True,
                          f"customQuestions.{section.get('id', '')}.{field.get('uniqueKey', '')}")
            if str(field.get("dataType") or "").casefold() == "number" and not options:
                q["format"] = "number"
            out.append(q)
    if not out:
        raise ValueError("The public Rippling form contained no fields")
    return out


def discover_questions(job: Job) -> tuple[list[dict], str]:
    """Return the employer's published questions and how they were found."""
    readers = {
        "greenhouse": greenhouse_questions,
        "lever": lever_questions,
        "ashby": ashby_questions,
        "workable": workable_questions,
        "rippling": rippling_questions,
    }
    reader = readers.get(job.ats)
    if job.ats in BROWSER_ONLY_ATS:
        return [], job.ats   # behind the employer's sign-in: filled step by step in the browser
    if reader is None:
        return [], "unsupported"
    try:
        return reader(job), f"{job.ats}_form"
    except Exception as error:  # noqa: BLE001 - the browser checks the live form when sending
        db.add_event(f"Could not read the {job.ats.title()} form ahead of time: {str(error)[:160]}", "warn", job.id)
        return [], "unavailable"


# ------------------------------------------------------------------ answers
def answer_questions(questions: list[dict], profile: Profile, settings: dict, job: Job) -> list[dict]:
    """Fill answers that saved facts or the resume can supply. Leaves the rest open."""
    for question in questions:
        if question.get("source") in ("you", "luna", "autofill") and not blank(question.get("answer")):
            continue   # keep a reviewer's answer, a Luna draft, or an AI Autofill answer
        if question["kind"] == "file":
            if question.get("source") == "you" and not blank(question.get("answer")):
                continue   # a file the candidate chose for this question
            is_resume = re.search(r"resume|\bcv\b", question["label"], re.I) or question.get("name") == "resume"
            letter = (not is_resume and question.get("required") and re.search(r"cover", question["label"], re.I)
                      and (settings.get("answers") or {}).get("cover_letter_mode", "auto") == "auto")
            question["answer"] = ("Resume for this role" if is_resume else
                                  "Cover letter written from your resume" if letter else None)
            question["source"] = "file" if is_resume or letter else "none"
            question["approved"] = bool(is_resume or letter)
            continue
        q = Q(label=question["label"], kind=question["kind"], options=list(question.get("options") or []),
              required=question.get("required", False), name=question.get("name", ""))
        answer = rule_answer(q, profile, settings, job)
        if answer is None and question["kind"] == "checkbox" and q.options:
            # Lever shows some single-choice questions ("Authorized to work? Yes / No") as boxes:
            # a saved answer that picks one option ticks that box.
            single = rule_answer(Q(label=q.label, kind="select", options=q.options, required=q.required,
                                   name=q.name), profile, settings, job)
            if single is not None and single.value not in (None, ""):
                answer = type(single)([single.value], single.source)
        if answer is None or answer.value in ("", None, []):
            question.update(answer=None, source="none", approved=False, note="")
            continue
        value = answer.value
        declined = isinstance(value, str) and re.search(r"decline|prefer not|don.?t wish|not to (?:say|answer|disclose)",
                                                        value, re.I)
        source = {"user": "saved", "rule": "profile", "resume": "resume"}.get(answer.source, "profile")
        # A typed field (a date picker, a number box) refuses text that is not its type.
        # Show exactly what it will receive, or ask now instead of failing at Submit.
        fitted = fit_answer(question.get("format"), question.get("label", ""), value)
        if fitted is None:
            question.update(answer=None, source="none", approved=False,
                            note=f"Needs {FORMAT_NAMES[question['format']]}; your saved answer “{value}” "
                                 "doesn't fit this field")
            continue
        value, note = fitted
        question.update(answer=value, source="declined" if declined else source, approved=True, note=note)
    return questions


def draft_status(questions: list[dict]) -> str:
    return "needs_answers" if any(needs_attention(q) for q in questions) else "ready"


def luna_ready(settings: dict) -> bool:
    backend = (settings.get("agent") or {}).get("backend", "none")
    return backend not in ("", "none") and bool((settings.get("luna") or {}).get("draft_answers", True))


def _open(question: dict) -> bool:
    """A question AI Autofill may answer: still blank, or an answer the employer's form refused."""
    return question["kind"] != "file" and (blank(question.get("answer")) or bool(question.get("rejected")))


def autofill(draft_id: int, settings: dict | None = None) -> int:
    """AI Autofill: realistic answers for every open question on one draft (the candidate reviews them)."""
    from .luna import autofill_answers
    draft = db.get_draft(draft_id)
    if draft is None:
        raise ValueError("Draft not found")
    settings = settings or db.get_settings()
    job, profile = db.get_job(draft["job_id"]), db.get_profile()
    if job is None or profile is None:
        raise ValueError("Job or profile no longer exists")
    open_questions = [q for q in draft["questions"] if _open(q)]
    if not open_questions:
        return 0
    filled = autofill_answers(job, profile, settings, open_questions)
    current = db.get_draft(draft_id)
    if current is None or current["status"] not in db.DRAFT_OPEN + ("practice", "error"):
        return 0
    by_id = {q["id"]: q for q in current["questions"]}
    for qid, result in filled.items():
        question = by_id.get(qid)
        if question is not None and _open(question):
            question.update(answer=result["answer"], source="autofill", approved=False, note=result.get("note", ""))
            question.pop("rejected", None)
    status = current["status"] if current["status"] in ("approved", "practice", "error") else draft_status(current["questions"])
    # A fill that ran clears the note left by an earlier one that could not.
    stale = str(current.get("error") or "").startswith(("Luna could not draft answers", "AI Autofill failed"))
    db.update_draft(draft_id, questions=current["questions"], status=status, **({"error": ""} if stale else {}))
    if filled:
        db.add_event(f"AI Autofill answered {len(filled)} question(s) for {job.company} / {job.title}", "info", job.id)
    return len(filled)


def autofill_all(progress: Callable[[str, int, int], None] | None = None, workers: int | None = None) -> dict:
    """AI Autofill every open prepared application that still has unanswered questions."""
    settings = db.get_settings()
    from .jarvis import parallel
    limit = parallel(settings, "parallel_prepare", 8)
    workers = min(workers or limit, limit)
    targets = [d["id"] for d in db.list_drafts(("ready", "needs_answers", "practice", "error"))
               if any(_open(q) for q in d["questions"])]
    done = filled = failed = 0
    counter = threading.Lock()

    def run(draft_id: int) -> int:
        nonlocal done, failed
        try:
            return autofill(draft_id, settings)
        except Exception as error:  # noqa: BLE001
            with counter:
                failed += 1
            db.update_draft(draft_id, error=f"AI Autofill failed: {str(error)[:200]}")
            return 0
        finally:
            with counter:
                done += 1
            if progress:
                progress("AI Autofill", done, len(targets))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        filled = sum(pool.map(run, targets))
    return {"applications": len(targets), "answers": filled, "failed": failed}


SPAM_REJECTION_RE = re.compile(r"spam filter rejected|flagged as (?:possible )?spam", re.I)
# After Submit, the employer's form refused the application and asked for corrections
# (Ashby: "Your form needs corrections · Missing entry for required field: ...").
CORRECTIONS_RE = re.compile(r"needs corrections|missing entry for required field|"
                            r"please (?:fix|correct) the (?:errors?|following)", re.I)
_REFUSED_FIELD_RE = re.compile(r"missing entry for required field:\s*(.+?)(?=\s*(?:\||missing entry for|$))", re.I)


def refused(record: dict) -> bool:
    """True when the employer explicitly refused an attempt after Submit, so it was not received."""
    error = str(record.get("error") or "")
    return bool(record.get("submission_attempted")) and bool(SPAM_REJECTION_RE.search(error) or
                                                             CORRECTIONS_RE.search(error))


def reopen_rejected(app_id: int) -> str:
    """For an attempt the employer explicitly refused, clear it so it can be prepared and sent again.

    A refusal that names fields keeps the prepared answers and flags those
    fields, so the candidate fixes them instead of starting over.
    """
    record = db.get_application(app_id)
    if record is None:
        raise ValueError("Application record not found")
    error = str(record.get("error") or "")
    if not refused(record):
        raise ValueError("Only an attempt the employer explicitly refused can be prepared again")
    if SPAM_REJECTION_RE.search(error):
        return db.mark_not_received(app_id, "The employer's spam filter refused this submission; it was not received.")
    job_id = db.mark_not_received(app_id, "The employer's form asked for corrections after Submit, so it was not received.")
    draft = db.draft_for_job(job_id)
    if draft and draft["questions"]:
        named = {_tidy(m).casefold() for m in _REFUSED_FIELD_RE.findall(error)}
        for question in draft["questions"]:
            if _tidy(question["label"]).casefold() in named:
                question.update(rejected=True, approved=False, note=(
                    "The employer's form didn't receive this answer. Check it, then send again."))
        db.update_draft(draft["id"], questions=draft["questions"], status=draft_status(draft["questions"]))
        try:
            db.set_job_status(job_id, "prepared")
        except ValueError:
            pass
    return job_id


def prepare_for_browser(draft_id: int, edits: dict[str, object] | None = None) -> dict:
    """Approve what is answered so far for a send the candidate finishes in a browser window.

    Unlike Approve & send, open questions are allowed: the candidate completes them in the window.
    """
    draft = db.get_draft(draft_id)
    if draft is None:
        raise ValueError("This application is no longer prepared")
    if draft["status"] == "unconfirmed":
        return draft
    if draft["status"] not in db.DRAFT_OPEN + ("practice", "error"):
        raise ValueError("This application was already sent")
    questions = apply_edits(draft["questions"], edits or {})
    for question in questions:
        if not blank(question.get("answer")):
            question["approved"] = True
            question.pop("rejected", None)
    from .models import now_iso
    db.update_draft(draft_id, questions=questions, status="approved", approved_at=now_iso(), error="")
    return db.get_draft(draft_id)


def draft_for_attempt(app_id: int) -> int:
    """The draft that finishes an unconfirmed attempt in the browser; made from the attempt if none exists."""
    record = db.get_application(app_id)
    if record is None:
        raise ValueError("Application record not found")
    if record.get("status") != "needs_human" or not record.get("submission_attempted"):
        raise ValueError("Only an unconfirmed submission can be finished in the browser")
    draft = db.draft_for_job(record["job_id"])
    if draft is not None:
        if draft["status"] != "unconfirmed" or int(draft.get("application_id") or 0) != int(app_id):
            raise ValueError("This application has a newer prepared version; open that one instead")
        return int(draft["id"])
    draft_id = db.upsert_draft(record["job_id"], status="error", resume_path=str(record.get("resume_path") or ""))
    db.update_draft(draft_id, status="unconfirmed", application_id=app_id, error=str(record.get("error") or "")[:400])
    try:
        db.set_job_status(record["job_id"], "needs_human")   # still waiting on you, not a new draft
    except ValueError:
        pass
    return draft_id


def luna_fill(draft_id: int, settings: dict | None = None) -> int:
    """Ask Luna for open questions on one draft. Returns how many answers she drafted."""
    from .luna import draft_answers, draftable
    draft = db.get_draft(draft_id)
    if draft is None:
        raise ValueError("Draft not found")
    settings = settings or db.get_settings()
    job, profile = db.get_job(draft["job_id"]), db.get_profile()
    if job is None or profile is None:
        raise ValueError("Job or profile no longer exists")
    questions = draft["questions"]
    open_questions = [q for q in questions if blank(q.get("answer")) and draftable(q)]
    if not open_questions:
        return 0
    drafted = draft_answers(job, profile, settings, open_questions)
    current = db.get_draft(draft_id)
    if current is None or current["status"] not in db.DRAFT_OPEN:
        return 0   # the candidate removed or sent it meanwhile
    by_id = {q["id"]: q for q in current["questions"]}
    for qid, result in drafted.items():
        question = by_id.get(qid)
        if question is not None and blank(question.get("answer")):
            question.update(answer=result["answer"], source="luna", approved=False, note=result.get("note", ""))
    status = current["status"] if current["status"] == "approved" else draft_status(current["questions"])
    db.update_draft(draft_id, questions=current["questions"], status=status)
    if drafted:
        db.add_event(f"Luna drafted {len(drafted)} answer(s) for {job.company} / {job.title}", "info", job.id)
    return len(drafted)


# ------------------------------------------------------------------ preparing
def plan_candidates(count: int, min_score: float, one_per_company: bool = True, *,
                    posted_within_hours: int = 0, order: str = "match",
                    exclude_starred: bool | None = None) -> list[Job]:
    """New jobs on supported forms, one per employer when asked.

    ``posted_within_hours`` keeps only postings published in that window (0 is
    any time). ``order`` is ``"match"`` (best match first) or ``"newest"``
    (most recently posted first).
    ``exclude_starred`` follows the saved bulk plan when omitted.
    """
    if exclude_starred is None:
        exclude_starred = bool((db.get_settings().get("apply_plan") or {}).get("exclude_starred", True))
    busy_companies = set()
    if one_per_company:
        for draft in db.list_drafts(db.DRAFT_OPEN + ("sending",)):
            busy_companies.add(str(draft.get("company") or "").strip().casefold())
    picked: list[Job] = []
    seen: set[str] = set()
    for job in db.list_jobs(status="new", min_score=float(min_score), limit=20000,
                            posted_within_hours=posted_within_hours or None,
                            order="posted" if order == "newest" else "match", exclude_starred=exclude_starred):
        if job.ats not in PREPARABLE_ATS:
            continue
        company = job.company.strip().casefold()
        if one_per_company and (company in seen or company in busy_companies):
            continue
        seen.add(company)
        picked.append(job)
        if len(picked) >= count:
            break
    return picked


def prepare_job(job: Job, profile: Profile, settings: dict) -> int:
    """Create (or refresh) a draft for one job without Luna. Returns the draft id."""
    draft_id = db.upsert_draft(job.id, status="preparing")
    try:
        questions, method = discover_questions(job)
        answer_questions(questions, profile, settings, job)
        from .tailor import resume_choice
        resume, note = resume_choice(profile, job, settings, out_dir=config.TAILORED / "drafts")
        if not resume or not Path(resume).is_file():
            raise ValueError("No resume is available. Add a resume PDF in Profile.")
        status = draft_status(questions)
        db.upsert_draft(job.id, status=status, resume_path=resume, resume_note=note,
                        questions=questions, question_source=method)
    except Exception as error:  # noqa: BLE001
        db.upsert_draft(job.id, status="error", error=str(error)[:400])
        raise
    return draft_id


def prepare_batch(count: int, min_score: float, one_per_company: bool = True, *,
                  use_luna: bool | None = None, progress: Callable[[str, int, int], None] | None = None,
                  stop: threading.Event | None = None, luna_workers: int | None = None,
                  posted_within_hours: int = 0, order: str = "match",
                  exclude_starred: bool | None = None) -> dict:
    """Prepare up to ``count`` applications. Luna drafts run after every form is read.

    Starred roles are excluded by default, following the saved bulk plan.
    When included, they still wait for the candidate's own Approve & send.
    """
    settings = db.get_settings()
    profile = db.get_profile()
    if profile is None or not profile.resume_path or not Path(profile.resume_path).is_file():
        raise ValueError("Add your resume in setup before preparing applications")
    from .priority import is_starred
    if exclude_starred is None:
        exclude_starred = bool((settings.get("apply_plan") or {}).get("exclude_starred", True))
    jobs = plan_candidates(count, min_score, one_per_company, posted_within_hours=posted_within_hours, order=order,
                           exclude_starred=exclude_starred)
    from .jarvis import parallel
    limit = parallel(settings, "parallel_prepare", 8)
    workers = max(1, min(luna_workers or limit, limit))
    results: dict[int, int] = {}
    failed = started = reading = 0
    counter = threading.Lock()
    from .governor import wait_for_headroom

    def read(position: int, job: Job) -> None:
        nonlocal failed, started, reading
        if stop is not None and stop.is_set():
            return
        with counter:
            started += 1
            index = started
        if progress:
            progress(f"Preparing {job.company}", index, len(jobs))
        if exclude_starred and is_starred(db.get_job(job.id) or job, db.get_settings().get("priority_companies") or []):
            return  # the candidate may have starred it while an earlier form was being prepared
        wait_for_headroom(lambda: reading, stop)
        with counter:
            reading += 1
        try:
            results[position] = prepare_job(job, profile, settings)
        except Exception:  # noqa: BLE001 - recorded on the draft
            with counter:
                failed += 1
        finally:
            with counter:
                reading -= 1

    # Forms are read side by side (settings.parallel_prepare); db writes are serialized by its lock.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(read, range(len(jobs)), jobs))
    prepared = [results[position] for position in sorted(results)]
    drafted = fill_failed = 0
    use_luna = luna_ready(settings) if use_luna is None else use_luna and luna_ready(settings)
    fill = autofill if (settings.get("luna") or {}).get("autofill_on_prepare", True) else luna_fill
    if use_luna and prepared and not (stop is not None and stop.is_set()):
        from .luna import draftable
        def needs(q):
            return blank(q.get("answer")) and (q["kind"] != "file" if fill is autofill else draftable(q))
        needing = [d for d in prepared if any(needs(q) for q in (db.get_draft(d) or {}).get("questions", []))]
        done = 0

        def run(draft_id: int) -> int:
            nonlocal done, fill_failed
            if stop is not None and stop.is_set():
                return 0
            try:
                return fill(draft_id, settings)
            except Exception as error:  # noqa: BLE001
                with counter:
                    fill_failed += 1
                db.update_draft(draft_id, error=f"Luna could not draft answers: {str(error)[:200]}")
                return 0
            finally:
                with counter:
                    done += 1
                if progress:
                    progress("Luna is drafting answers", done, len(needing))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            drafted = sum(pool.map(run, needing))
    db.add_event(f"Prepared {len(prepared)} application(s) for review" + (f"; Luna drafted {drafted} answers" if drafted else ""),
                 "success" if prepared else "warn")
    priority = settings.get("priority_companies") or []
    starred = sum(1 for draft_id in prepared if is_starred(db.get_draft(draft_id) or {}, priority))
    return {"prepared": len(prepared), "failed": failed, "considered": len(jobs), "drafted": drafted,
            "fill_failed": fill_failed, "starred": starred}


def bulk_send_candidates(drafts: list[dict], priority: list[str],
                         settings: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Split drafts for "Send ready" into (send, held).

    An approved draft was approved on its own and goes. A ready draft goes
    unless it is starred (priority companies are starred automatically):
    starred applications wait for their own Approve & send.
    """
    from .priority import held_from_bulk_send
    send, held = [], []
    for draft in drafts:
        if not sends_unattended(draft.get("ats") or "", settings or {}):
            continue   # Workday without Send Workday applications: finished in a browser window
        if draft.get("status") == "ready" and held_from_bulk_send(draft, priority):
            held.append(draft)
        elif draft.get("status") in ("approved", "ready"):
            send.append(draft)
    return send, held


# ------------------------------------------------------------------ sending
def _rememberable(question: dict, value: object, job: Job) -> bool:
    """Only short, general answers become reusable; role-specific essays stay with this job."""
    if not isinstance(value, (str, bool)) or question["kind"] == "file":
        return False
    if isinstance(value, bool) or question.get("options"):
        return True
    text = str(value)
    return len(text) <= 120 and job.company.casefold() not in text.casefold() and "\n" not in text


def approve_and_queue(draft_id: int, edits: dict[str, object] | None = None) -> dict:
    """Apply a reviewer's edits, approve every answer, and mark the draft ready to send."""
    draft = db.get_draft(draft_id)
    if draft is None:
        raise ValueError("This application is no longer prepared")
    if draft["status"] not in db.DRAFT_OPEN + ("practice", "error"):
        raise ValueError("This application was already sent")
    questions = apply_edits(draft["questions"], edits or {})
    for question in questions:
        if not blank(question.get("answer")):
            question["approved"] = True
            question.pop("rejected", None)   # the reviewer saw the form's objection and kept or changed it
    unanswered = [q["label"] for q in questions if missing(q)]
    if unanswered:
        db.update_draft(draft_id, questions=questions, status="needs_answers")
        raise ValueError("Answer the required questions first: " + "; ".join(unanswered[:4])
                         + (f" and {len(unanswered) - 4} more" if len(unanswered) > 4 else ""))
    from .models import now_iso
    db.update_draft(draft_id, questions=questions, status="approved", approved_at=now_iso(), error="")
    return db.get_draft(draft_id)


def send_draft(draft_id: int, parallel: int = 1, assist: bool = False, *, unattended: bool = False) -> dict:
    """Send one approved draft through the browser engine and record the outcome.

    ``parallel`` is how many applications may be in flight at once. When every
    slot is taken (an automatic run, say), the draft stays approved and the
    result's status is ``busy`` so the caller can try again shortly.

    ``assist`` opens a visible window filled as far as Starployment can; the
    candidate finishes it and presses Submit. An unconfirmed draft reopens its
    own attempt rather than reserving a second one.
    """
    from . import scheduler
    draft = db.get_draft(draft_id)
    if draft is None:
        raise ValueError("This application is no longer prepared")
    reopen = assist and draft["status"] == "unconfirmed" and draft.get("application_id")
    if draft["status"] != "approved" and not reopen:
        raise ValueError("Approve this application before sending it")
    job = db.get_job(draft["job_id"])
    if job is None:
        raise ValueError("The job no longer exists")
    settings = db.get_settings()
    if not assist and not sends_unattended(job.ats, settings):
        raise ValueError("This application is on Workday, behind the employer's sign-in. Choose Finish in browser, "
                         "or turn on Settings → Workday → Send Workday applications.")
    extra: dict[str, object] = {}
    remember: dict[str, object] = {}
    for question in draft["questions"]:
        value = question.get("answer")
        if blank(value) or not question.get("approved"):
            continue
        if question["kind"] == "file":
            # Only a file the candidate chose; the resume and cover letter are attached by the engine.
            if question.get("source") == "you" and Path(str(value)).is_file():
                extra[question["label"]] = str(value)
            continue
        if question.get("source") in ("luna", "you", "saved", "autofill"):
            extra.update(send_answers(question, value))
            if question.get("source") in ("luna", "you") and _rememberable(question, value, job):
                remember[question["label"]] = value
    try:
        if reopen:
            application_id = int(draft["application_id"])
            db.reopen_for_browser(application_id)
        else:
            application_id = scheduler.queue_application(job.id, parallel=parallel)
    except ValueError as error:
        if reopen:
            return {"status": "blocked", "error": str(error), "draft_id": draft_id}
        # Every browser slot is taken: not a problem with this application.
        if "waits its turn" in str(error) or str(error).startswith("An application is already queued"):
            return {"status": "busy", "error": str(error), "draft_id": draft_id}
        db.update_draft(draft_id, status="error", error=str(error))
        return {"status": "blocked", "error": str(error), "draft_id": draft_id}
    db.update_draft(draft_id, status="sending", application_id=application_id, error="")
    result = scheduler.apply_one(job.id, application_id=application_id, extra_answers=extra,
                                 resume_override=draft.get("resume_path") or None, assist=assist, require_eligible=unattended,
                                 required_jarvis_mode="all" if unattended else None)
    status = str(result.get("status"))
    if status == "applied":
        db.update_draft(draft_id, status="sent")
        if remember and not unattended and (settings.get("luna") or {}).get("remember_approved", True):
            answers = dict(settings.get("answers") or {})
            bank = dict(answers.get("field_answers") or {})
            for label, value in remember.items():
                bank.setdefault(label, value)
            answers["field_answers"] = bank
            db.set_settings({"answers": answers})
    elif result.get("submission_attempted"):
        db.update_draft(draft_id, status="unconfirmed", error=str(result.get("message") or "")[:400])
    else:
        questions = draft["questions"]
        known = {_tidy(q["label"]).casefold() for q in questions}
        groups = _group_options(questions)
        refused: set[str] = set()
        for detail in result.get("questions") or []:
            label = _tidy(detail.get("label") or "")
            # A live form reports each box of a "select all that apply" group;
            # the group question already holds that answer.
            if not label or _covered_by_group(label, groups):
                continue
            if label.casefold() not in known:
                questions.append(_question(len(questions) + 1, label, str(detail.get("kind") or "text"),
                                           detail.get("options") or [], bool(detail.get("required"))))
                known.add(label.casefold())
                if not detail.get("invalid"):
                    continue
            refused.add(label.casefold())
        answer_questions(questions, db.get_profile() or Profile(), settings, job)
        # The form refused these answers, or never registered them. Ask again rather
        # than stopping with a draft that has nothing left to edit.
        for question in questions:
            if _tidy(question["label"]).casefold() not in refused or question.get("source") == "file":
                continue
            question.update(rejected=True, approved=False)
            if not blank(question.get("answer")):
                question["note"] = ("The employer's form didn't accept this answer. "
                                    "Check it, change it, or try AI Autofill.")
        new_status = "practice" if status == "dry_run" else "needs_answers" if draft_status(questions) == "needs_answers" else "error"
        db.update_draft(draft_id, status=new_status, questions=questions,
                        error="" if status == "dry_run" else str(result.get("message") or result.get("error") or "")[:400])
        try:
            db.set_job_status(job.id, "prepared")
        except ValueError:
            pass
    return {**result, "draft_id": draft_id}
