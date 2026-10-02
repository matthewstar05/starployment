"""Prepare applications for review, then send the ones the candidate approves.

Preparing a job reads the employer's public form definition (Greenhouse,
Lever, and Ashby publish one), fills every answer that saved facts can supply,
asks Luna to draft the open-ended ones, and builds the resume for that role.
Nothing is sent while preparing. Sending goes through ``scheduler.apply_one``,
so the browser engine stays the authority on the live form, and every
submission still crosses the durable submit boundary.
"""
from __future__ import annotations

import html
import json
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from . import config, db
from .answers import Q, rule_answer
from .models import Job, Profile
from .sources.base import client, strip_html
from .sources.import_job import parse_job_url

SUPPORTED_ATS = ("greenhouse", "lever", "ashby")
SOURCE_LABELS = {"saved": "Saved answer", "profile": "From your profile", "resume": "From your resume",
                 "luna": "Drafted by Luna", "autofill": "AI Autofill", "you": "Your answer", "none": "Needs your answer",
                 "declined": "Declined", "file": "Attached"}


def blank(value) -> bool:
    """No answer yet. A multi-select answer is a list; an empty list is no answer."""
    return value is None or value == "" or value == []


def expand_answer(question: dict, value) -> dict[str, object]:
    """Form-ready answers for one question.

    A "select all that apply" group is filled as one checkbox per option, which
    the browser engine sees as ``"<question> - <option>"``.
    """
    if question.get("kind") == "checkbox" and question.get("options") and isinstance(value, list):
        chosen = {str(v).casefold() for v in value}
        return {f"{question['label']} - {option}": option.casefold() in chosen for option in question["options"]}
    return {question["label"]: value}


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
            "options": [str(o) for o in (options or []) if str(o).strip()][:200],
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
    return out


_LEVER_KINDS = {"text": "text", "textarea": "textarea", "multiple-choice": "radio", "dropdown": "select",
                "multiple-select": "checkbox", "file-upload": "file"}
_LEVER_STANDARD = (("Resume/CV", "file", True, "resume"), ("Full name", "text", True, "name"),
                   ("Email", "text", True, "email"), ("Phone", "text", False, "phone"),
                   ("Current location", "combobox", False, "location"), ("Current company", "text", False, "org"),
                   ("LinkedIn URL", "text", False, "urls[LinkedIn]"), ("GitHub URL", "text", False, "urls[GitHub]"),
                   ("Portfolio URL", "text", False, "urls[Portfolio]"))


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
    for tag in re.findall(r"<input[^>]+name=\"cards\[[^\]]+\]\[baseTemplate\]\"[^>]*>", page):
        value = re.search(r'value="([^"]*)"', tag)
        if not value:
            continue
        try:
            card = json.loads(html.unescape(value.group(1)))
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
_ASHBY_KINDS = {"String": "text", "Email": "text", "Phone": "text", "SocialLink": "text", "Number": "text",
                "Date": "text", "LongText": "textarea", "Boolean": "radio", "ValueSelect": "select",
                "MultiValueSelect": "checkbox", "File": "file", "Location": "combobox"}


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
    return out


def discover_questions(job: Job) -> tuple[list[dict], str]:
    """Return the employer's published questions and how they were found."""
    readers = {"greenhouse": greenhouse_questions, "lever": lever_questions, "ashby": ashby_questions}
    reader = readers.get(job.ats)
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
        if question.get("source") in ("you", "luna") and not blank(question.get("answer")):
            continue   # keep a reviewer's answer or a draft
        if question["kind"] == "file":
            is_resume = re.search(r"resume|\bcv\b", question["label"], re.I) or question.get("name") == "resume"
            question["answer"] = "Resume for this role" if is_resume else None
            question["source"] = "file" if is_resume else "none"
            question["approved"] = bool(is_resume)
            continue
        q = Q(label=question["label"], kind=question["kind"], options=list(question.get("options") or []),
              required=question.get("required", False), name=question.get("name", ""))
        answer = rule_answer(q, profile, settings, job)
        if answer is None or answer.value in ("", None, []):
            question.update(answer=None, source="none", approved=False)
            continue
        value = answer.value
        declined = isinstance(value, str) and re.search(r"decline|prefer not|don.?t wish|not to (?:say|answer|disclose)",
                                                        value, re.I)
        source = {"user": "saved", "rule": "profile", "resume": "resume"}.get(answer.source, "profile")
        question.update(answer=value, source="declined" if declined else source, approved=True)
    return questions


def draft_status(questions: list[dict]) -> str:
    missing = [q for q in questions if q.get("required") and blank(q.get("answer")) and q["kind"] != "file"]
    return "needs_answers" if missing else "ready"


def luna_ready(settings: dict) -> bool:
    backend = (settings.get("agent") or {}).get("backend", "none")
    return backend not in ("", "none") and bool((settings.get("luna") or {}).get("draft_answers", True))


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
    open_questions = [q for q in draft["questions"] if blank(q.get("answer")) and q["kind"] != "file"]
    if not open_questions:
        return 0
    filled = autofill_answers(job, profile, settings, open_questions)
    current = db.get_draft(draft_id)
    if current is None or current["status"] not in db.DRAFT_OPEN + ("practice", "error"):
        return 0
    by_id = {q["id"]: q for q in current["questions"]}
    for qid, result in filled.items():
        question = by_id.get(qid)
        if question is not None and blank(question.get("answer")):
            question.update(answer=result["answer"], source="autofill", approved=False, note=result.get("note", ""))
    status = current["status"] if current["status"] in ("approved", "practice", "error") else draft_status(current["questions"])
    db.update_draft(draft_id, questions=current["questions"], status=status)
    if filled:
        db.add_event(f"AI Autofill answered {len(filled)} question(s) for {job.company} / {job.title}", "info", job.id)
    return len(filled)


def autofill_all(progress: Callable[[str, int, int], None] | None = None, workers: int = 3) -> dict:
    """AI Autofill every open prepared application that still has unanswered questions."""
    settings = db.get_settings()
    targets = [d["id"] for d in db.list_drafts(("ready", "needs_answers", "practice", "error"))
               if any(blank(q.get("answer")) and q["kind"] != "file" for q in d["questions"])]
    done = filled = failed = 0

    def run(draft_id: int) -> int:
        nonlocal done, failed
        try:
            return autofill(draft_id, settings)
        except Exception as error:  # noqa: BLE001
            failed += 1
            db.update_draft(draft_id, error=f"AI Autofill failed: {str(error)[:200]}")
            return 0
        finally:
            done += 1
            if progress:
                progress("AI Autofill", done, len(targets))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        filled = sum(pool.map(run, targets))
    return {"applications": len(targets), "answers": filled, "failed": failed}


SPAM_REJECTION_RE = re.compile(r"spam filter rejected|flagged as (?:possible )?spam", re.I)


def reopen_rejected(app_id: int) -> str:
    """For an attempt the employer's spam filter refused, clear it so it can be prepared and sent again."""
    record = db.get_application(app_id)
    if record is None:
        raise ValueError("Application record not found")
    if not SPAM_REJECTION_RE.search(str(record.get("error") or "")):
        raise ValueError("Only an attempt the employer explicitly refused can be prepared again")
    return db.mark_not_received(app_id, "The employer's spam filter refused this submission; it was not received.")


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
def plan_candidates(count: int, min_score: float, one_per_company: bool = True) -> list[Job]:
    """The best-matching New jobs on supported forms, one per employer when asked."""
    busy_companies = set()
    if one_per_company:
        for draft in db.list_drafts(db.DRAFT_OPEN + ("sending",)):
            busy_companies.add(str(draft.get("company") or "").strip().casefold())
    picked: list[Job] = []
    seen: set[str] = set()
    for job in db.list_jobs(status="new", min_score=float(min_score), limit=20000):
        if job.ats not in SUPPORTED_ATS:
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
                  stop: threading.Event | None = None, luna_workers: int = 3) -> dict:
    """Prepare up to ``count`` applications. Luna drafts run after every form is read."""
    settings = db.get_settings()
    profile = db.get_profile()
    if profile is None or not profile.resume_path or not Path(profile.resume_path).is_file():
        raise ValueError("Add your resume in setup before preparing applications")
    jobs = plan_candidates(count, min_score, one_per_company)
    prepared: list[int] = []
    failed = 0
    for index, job in enumerate(jobs, 1):
        if stop is not None and stop.is_set():
            break
        if progress:
            progress(f"Preparing {job.company}", index, len(jobs))
        try:
            prepared.append(prepare_job(job, profile, settings))
        except Exception:  # noqa: BLE001 - recorded on the draft
            failed += 1
    drafted = 0
    use_luna = luna_ready(settings) if use_luna is None else use_luna and luna_ready(settings)
    fill = autofill if (settings.get("luna") or {}).get("autofill_on_prepare", True) else luna_fill
    if use_luna and prepared and not (stop is not None and stop.is_set()):
        from .luna import draftable
        def needs(q):
            return blank(q.get("answer")) and (q["kind"] != "file" if fill is autofill else draftable(q))
        needing = [d for d in prepared if any(needs(q) for q in (db.get_draft(d) or {}).get("questions", []))]
        done = 0

        def run(draft_id: int) -> int:
            nonlocal done
            if stop is not None and stop.is_set():
                return 0
            try:
                return fill(draft_id, settings)
            except Exception as error:  # noqa: BLE001
                db.update_draft(draft_id, error=f"Luna could not draft answers: {str(error)[:200]}")
                return 0
            finally:
                done += 1
                if progress:
                    progress("Luna is drafting answers", done, len(needing))

        with ThreadPoolExecutor(max_workers=max(1, luna_workers)) as pool:
            drafted = sum(pool.map(run, needing))
    db.add_event(f"Prepared {len(prepared)} application(s) for review" + (f"; Luna drafted {drafted} answers" if drafted else ""),
                 "success" if prepared else "warn")
    return {"prepared": len(prepared), "failed": failed, "considered": len(jobs), "drafted": drafted}


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
    missing = [q["label"] for q in questions
               if q.get("required") and blank(q.get("answer")) and q["kind"] != "file"]
    if missing:
        db.update_draft(draft_id, questions=questions, status="needs_answers")
        raise ValueError("Answer the required questions first: " + "; ".join(missing[:4]))
    from .models import now_iso
    db.update_draft(draft_id, questions=questions, status="approved", approved_at=now_iso(), error="")
    return db.get_draft(draft_id)


def send_draft(draft_id: int) -> dict:
    """Send one approved draft through the browser engine and record the outcome."""
    from . import scheduler
    draft = db.get_draft(draft_id)
    if draft is None:
        raise ValueError("This application is no longer prepared")
    if draft["status"] != "approved":
        raise ValueError("Approve this application before sending it")
    job = db.get_job(draft["job_id"])
    if job is None:
        raise ValueError("The job no longer exists")
    settings = db.get_settings()
    extra: dict[str, object] = {}
    remember: dict[str, object] = {}
    for question in draft["questions"]:
        value = question.get("answer")
        if blank(value) or not question.get("approved") or question["kind"] == "file":
            continue
        if question.get("source") in ("luna", "you", "saved", "autofill"):
            extra.update(expand_answer(question, value))
            if question.get("source") in ("luna", "you") and _rememberable(question, value, job):
                remember[question["label"]] = value
    try:
        application_id = scheduler.queue_application(job.id)
    except ValueError as error:
        db.update_draft(draft_id, status="error", error=str(error))
        return {"status": "blocked", "error": str(error), "draft_id": draft_id}
    db.update_draft(draft_id, status="sending", application_id=application_id, error="")
    result = scheduler.apply_one(job.id, application_id=application_id, extra_answers=extra,
                                 resume_override=draft.get("resume_path") or None)
    status = str(result.get("status"))
    if status == "applied":
        db.update_draft(draft_id, status="sent")
        if remember and (settings.get("luna") or {}).get("remember_approved", True):
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
        known = {q["label"].casefold() for q in questions}
        for detail in result.get("questions") or []:
            label = str(detail.get("label") or "").strip()
            if label and label.casefold() not in known:
                questions.append(_question(len(questions) + 1, label, str(detail.get("kind") or "text"),
                                           detail.get("options") or [], bool(detail.get("required"))))
                known.add(label.casefold())
        answer_questions(questions, db.get_profile() or Profile(), settings, job)
        new_status = "practice" if status == "dry_run" else "needs_answers" if draft_status(questions) == "needs_answers" else "error"
        db.update_draft(draft_id, status=new_status, questions=questions,
                        error="" if status == "dry_run" else str(result.get("message") or result.get("error") or "")[:400])
        try:
            db.set_job_status(job.id, "prepared")
        except ValueError:
            pass
    return {**result, "draft_id": draft_id}
