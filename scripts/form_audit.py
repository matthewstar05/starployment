"""Audit Starployment's application pipeline against many real public job forms, without applying.

    .venv/bin/python scripts/form_audit.py --api 300 --live 40

``--api N``   Read the published form definitions of N postings (the same read-only
              public APIs that preparing an application uses) and report field types
              the app does not understand.
``--live N``  Run the whole pipeline for N postings in practice mode: prepare the
              application, answer every question with fictional data (as AI Autofill
              or the candidate would), then fill the live form in a background browser.
              Submit is never clicked. The browser may not send anything either: every
              request except page loads (GET) and Ashby's read-only form queries is
              aborted, so no file or form data leaves this computer.

Postings come from a read-only snapshot of the app's database (``--db`` to choose
one) or from ``--urls``. Everything runs in a temporary data folder with a
fictional candidate (example.test email, 555 phone number). The report is written
to ``tmp/form_audit/`` and groups problems by cause.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["JOBHUB_DATA_DIR"] = str(Path(tempfile.mkdtemp(prefix="starployment-audit-")) / "data")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from jobhub import config, db, prepare, scheduler  # noqa: E402
from jobhub.apply import engine as engine_module  # noqa: E402
from jobhub.models import Education, Experience, Job, Profile, Project  # noqa: E402

OUT = ROOT / "tmp" / "form_audit"
LIVE_DB = Path.home() / "Library" / "Application Support"

# ------------------------------------------------------------------ a fictional candidate
CANDIDATE = Profile(
    first_name="Alex", last_name="Rivera", email="alex.rivera@example.test", phone="415-555-0134",
    city="San Francisco", state="CA", country="United States", postal_code="94103",
    address_line1="100 Example Street", linkedin="https://www.linkedin.com/in/example-alex",
    github="https://github.com/example-alex", website="https://example.test",
    headline="Software engineer focused on data systems",
    summary="New-grad engineer who builds reliable data pipelines and web services.",
    # A real school, so school catalogs behave as they do for a real candidate.
    education=[Education(school="University of California, Berkeley", degree="Bachelor of Science in Computer Science",
                         start="Sep 2022", end="May 2026", gpa="3.7")],
    experience=[Experience(company="Northbeam Analytics", title="Software Engineer Intern", start="Jun 2025",
                           end="Sep 2025", location="San Francisco, CA",
                           bullets=["Built an Airflow pipeline that cut nightly reporting from 3 hours to 40 minutes"])],
    projects=[Project(name="Order Book Simulator", bullets=["Simulated a limit order book in C++"])],
    skills=["Python", "SQL", "TypeScript", "React", "Node.js", "Docker", "PostgreSQL", "PyTorch"],
)
ANSWERS = {"work_authorized_us": "Yes", "requires_sponsorship": "No", "us_citizen_or_permanent_resident": "Yes",
           "willing_to_relocate": "Yes", "salary_expectation": "120000", "available_start": "June 2026",
           "how_heard": "LinkedIn", "years_experience": "1"}
ESSAY = ("At Northbeam I rebuilt a nightly reporting job as an Airflow pipeline, which cut it from three hours to "
         "forty minutes and let analysts see results before standup. I like work where a careful design makes a "
         "slow, fragile process quick and dependable, and this role looks like more of that.")
DECLINE = re.compile(r"decline|prefer not|don.?t wish|not to (?:say|answer|disclose)|rather not", re.I)
DEMOGRAPHIC = re.compile(r"gender|race|ethnic|hispanic|veteran|disab|sexual|orientation|pronoun|transgender", re.I)


def setup_store() -> None:
    from reportlab.pdfgen import canvas

    db.init()
    for folder in (config.RESUMES, config.TAILORED, config.SCREENSHOTS):
        folder.mkdir(parents=True, exist_ok=True)
    resume = config.RESUMES / "alex-rivera.pdf"
    pdf = canvas.Canvas(str(resume))
    for index, line in enumerate(["Alex Rivera", "alex.rivera@example.test · 415-555-0134",
                                  "B.S. Computer Science, University of California, Berkeley, May 2026",
                                  "Software Engineer Intern, Northbeam Analytics"]):
        pdf.drawString(72, 740 - 18 * index, line)
    pdf.save()
    db.save_profile(CANDIDATE.model_copy(update={"resume_path": str(resume)}))
    settings = db.get_settings()
    db.set_settings({"setup_complete": True, "dry_run": True, "resume_strategy": "original",
                     "target_titles": ["Software Engineer"],
                     "answers": {**settings["answers"], **ANSWERS, "confirmed_fields": list(ANSWERS)}})


def fictional_answer(question: dict):
    """What AI Autofill or the candidate would put in an open question, with fictional facts."""
    label, kind = question["label"], question["kind"]
    options = list(question.get("options") or [])
    if kind == "file":
        return None
    if kind == "checkbox" and len(options) == 1 and re.search(r"acknowledg|agree|understand|consent|confirm",
                                                              options[0], re.I):
        return options[:1]
    if DEMOGRAPHIC.search(label) and options:
        declined = next((o for o in options if DECLINE.search(o)), None)
        return [declined] if kind == "checkbox" and declined else declined
    if kind == "checkbox" and options:
        return options[:1]
    if kind == "checkbox":
        return True
    if options:
        no = next((o for o in options if o.strip().casefold() in {"no", "no."}), None)
        if re.search(r"previously (?:worked|employed|applied)|former|current employee|related to|relative", label, re.I):
            return no or options[-1]
        return next((o for o in options if o.strip().casefold() in {"yes", "yes."}), None) or options[0]
    if question.get("format") == "date" or re.search(r"\bdate\b", label, re.I):
        return "06/01/2026"
    if re.search(r"linkedin", label, re.I):
        return CANDIDATE.linkedin
    if re.search(r"github|portfolio|website|url|link", label, re.I):
        return CANDIDATE.github
    if re.search(r"how many|number of|years|gpa", label, re.I):
        return "2"
    if re.search(r"location|city|where .* (?:live|based|located|work)", label, re.I):
        return "San Francisco, CA"
    return ESSAY if kind == "textarea" else "Northbeam Analytics"


# ------------------------------------------------------------------ choosing postings
def snapshot_jobs(path: Path | None) -> list[tuple[str, str, str]]:
    """(ats, company, url) for new postings, from a read-only copy of a Starployment database."""
    if path is None:
        for name in ("Starployment", "JobHub"):
            if (LIVE_DB / name / "jobhub.db").exists():
                path = LIVE_DB / name / "jobhub.db"
                break
    if path is None or not path.exists():
        raise SystemExit("No database found; pass --db or --urls")
    copy = Path(tempfile.mkdtemp()) / "snapshot.db"
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as source, sqlite3.connect(copy) as target:
        source.backup(target)
    with sqlite3.connect(copy) as con:
        rows = con.execute("SELECT ats, company, url, apply_url FROM jobs WHERE status='new' AND ats IN "
                           "('greenhouse','lever','ashby') ORDER BY posted_ts DESC NULLS LAST").fetchall()
    copy.unlink()
    from jobhub.scheduler import hosted_posting_url
    return [(ats, company, hosted_posting_url(Job(source="x", ats=ats, company=company, title="", url=url,
                                                   apply_url=apply_url or url)))
            for ats, company, url, apply_url in rows]


def sample(rows: list[tuple[str, str, str]], count: int, skip: int = 0) -> list[str]:
    """A spread across providers, one posting per employer, newest first (after ``skip`` per provider)."""
    by_ats: dict[str, list[str]] = defaultdict(list)
    seen: set[str] = set()
    for ats, company, url in rows:
        key = company.strip().casefold()
        if key in seen:
            continue
        seen.add(key)
        by_ats[ats].append(url)
    for ats in by_ats:
        by_ats[ats] = by_ats[ats][skip:]
    picked: list[str] = []
    while len(picked) < count and any(by_ats.values()):
        for ats in ("greenhouse", "ashby", "lever"):
            if by_ats[ats] and len(picked) < count:
                picked.append(by_ats[ats].pop(0))
    return picked


# ------------------------------------------------------------------ level 1: form definitions
def raw_field_types(job: Job) -> list[tuple[str, str]]:
    """(type, label) for every field the employer publishes, including ones the app skips."""
    from urllib.parse import quote

    from jobhub.sources.base import client
    from jobhub.sources.import_job import parse_job_url

    parsed = parse_job_url(job.apply_url or job.url)
    out: list[tuple[str, str]] = []
    with client(timeout=15.0) as http:
        if job.ats == "greenhouse":
            data = http.get(f"https://boards-api.greenhouse.io/v1/boards/{quote(parsed.slug, safe='')}"
                            f"/jobs/{quote(parsed.posting_id, safe='')}", params={"questions": "true"}).json()
            for question in (data.get("questions") or []) + (data.get("location_questions") or []):
                for field in (question.get("fields") or [])[:1]:
                    out.append((f"greenhouse:{field.get('type')}", question.get("label") or ""))
        elif job.ats == "ashby":
            data = http.post("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting", json={
                "operationName": "ApiJobPosting", "query": prepare._ASHBY_QUERY,
                "variables": {"organizationHostedJobsPageName": parsed.slug, "jobPostingId": parsed.posting_id}}).json()
            posting = (data.get("data") or {}).get("jobPosting") or {}
            for section in ((posting.get("applicationForm") or {}).get("sections") or []):
                for entry in section.get("fieldEntries") or []:
                    field = (entry or {}).get("field") or {}
                    out.append((f"ashby:{field.get('type')}", field.get("title") or ""))
        else:
            host = "jobs.eu.lever.co" if ".eu." in job.url else "jobs.lever.co"
            page = http.get(f"https://{host}/{parsed.slug}/{parsed.posting_id}/apply").text
            for raw in prepare.lever_cards(page):
                for field in json.loads(raw).get("fields") or []:
                    out.append((f"lever:{field.get('type')}", field.get("text") or ""))
    return out


KNOWN = {f"greenhouse:{k}" for k in prepare._GH_KINDS} | {f"ashby:{k}" for k in prepare._ASHBY_KINDS} | \
        {f"lever:{k}" for k in prepare._LEVER_KINDS}


def api_audit(urls: list[str]) -> dict:
    from jobhub.sources.import_job import fetch_job

    types: Counter = Counter()
    unknown: dict[str, list[str]] = defaultdict(list)
    errors: list[str] = []

    def one(url: str):
        try:
            job = fetch_job(url)
            fields = raw_field_types(job)
            questions, how = prepare.discover_questions(job)
            prepare.answer_questions(questions, db.get_profile(), db.get_settings(), job)
            for question in questions:
                prepare.expand_answer(question, fictional_answer(question))   # must not raise
            return url, fields, None
        except Exception as error:  # noqa: BLE001
            return url, [], f"{url}: {type(error).__name__}: {str(error)[:160]}"

    with ThreadPoolExecutor(8) as pool:
        for url, fields, error in pool.map(one, urls):
            if error:
                errors.append(error)
            for kind, label in fields:
                types[kind] += 1
                if kind not in KNOWN and len(unknown[kind]) < 5:
                    unknown[kind].append(f"{label[:80]}  ({url})")
    return {"postings": len(urls), "types": dict(types.most_common()), "unknown": dict(unknown), "errors": errors}


# ------------------------------------------------------------------ level 2: practice runs on live forms
BLOCKED: Counter = Counter()
_blocked_lock = threading.Lock()


def _graphql_operations(body: str) -> list[tuple[str, str]] | None:
    """(operation name, kind) for a GraphQL body, or None when the body is not GraphQL JSON."""
    try:
        payload = json.loads(body or "null")
    except json.JSONDecodeError:
        return None
    items = payload if isinstance(payload, list) else [payload]
    if not items or not all(isinstance(item, dict) and "query" in item for item in items):
        return None
    return [(str(item.get("operationName") or "?"),
             "mutation" if str(item.get("query") or "").lstrip().startswith("mutation") else "query") for item in items]


UPLOAD = re.compile(r"amazonaws\.com|storage\.googleapis|blob\.core|parseResume|upload|/submit|/apply\b|"
                    r"application/(?:submit|create)", re.I)


def _guard(route) -> None:
    """Page loads, read-only queries, and the page's own checks pass; uploads and anything
    that could submit or store application data are aborted."""
    request = route.request
    if request.method in ("GET", "HEAD", "OPTIONS"):
        try:
            route.continue_()
        except Exception:  # noqa: BLE001
            pass
        return
    try:   # uploads are binary; never let decoding fail inside the browser's event loop
        body = (request.post_data_buffer or b"").decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        body = ""
    operations = _graphql_operations(body) if "graphql" in request.url else None
    content_type = (request.headers.get("content-type") or "").lower()
    if operations is not None:
        blocked = any(kind == "mutation" for _, kind in operations)
        name = ",".join(name for name, _ in operations)
    else:
        blocked = "multipart" in content_type or len(body) > 20000 or bool(UPLOAD.search(request.url))
        name = ""
    try:
        if not blocked:
            route.continue_()
            return
        with _blocked_lock:
            BLOCKED[f"{request.method} {re.sub(r'[?#].*', '', request.url)[:80]} {name}".strip()] += 1
        route.abort()
    except Exception:  # noqa: BLE001 - the page closed while the request was in flight
        pass


def guard_browser() -> None:
    """Every browser the engine opens may only read: uploads and form posts are aborted."""
    original = engine_module.launch_context

    def guarded(playwright, channel, profile_dir=None, visible=False):
        context = original(playwright, channel, profile_dir, visible)
        context.route("**/*", _guard)
        return context

    engine_module.launch_context = guarded


def practice(url: str) -> dict:
    from jobhub.sources.import_job import fetch_job

    record: dict = {"url": url}
    try:
        job = fetch_job(url)
        db.upsert_jobs([job])
        job = db.get_job(job.id) or job
        record.update(ats=job.ats, company=job.company, title=job.title)
        draft_id = prepare.prepare_job(job, db.get_profile(), db.get_settings())
        draft = db.get_draft(draft_id)
        record["read_ahead"] = len(draft["questions"])
        edits = {}
        for question in draft["questions"]:
            if prepare.blank(question.get("answer")) and question["kind"] != "file":
                value = fictional_answer(question)
                if value is not None:
                    edits[question["id"]] = value
        prepare.approve_and_queue(draft_id, edits)
        result = prepare.send_draft(draft_id, parallel=scheduler.MAX_PARALLEL_SENDS)
        after = db.get_draft(draft_id)
        log = result.get("log") or []
        record.update(status=result.get("status"), message=str(result.get("message") or result.get("error") or ""),
                      draft_status=after["status"] if after else None,
                      could_not_fill=[line.split(" ", 1)[1] for line in log if "Could not fill" in line],
                      invalid=[line.split("Invalid after filling: ", 1)[1] for line in log
                               if "Invalid after filling: " in line],
                      unanswered=result.get("unanswered") or [],
                      added=[q["label"] for q in (after or {}).get("questions", [])][record["read_ahead"]:])
    except Exception as error:  # noqa: BLE001
        record.update(status="crash", message=f"{type(error).__name__}: {str(error)[:300]}")
    return record


def classify(record: dict) -> list[str]:
    """Group a practice run's problems by cause."""
    causes = []
    message = record.get("message", "")
    if record.get("status") == "crash":
        causes.append("crash: " + message.split(":")[0])
    if re.search(r"HTTP 4\d\d|closed|no longer|not found|No supported application form", message, re.I):
        causes.append("posting closed or no form")
    if re.search(r"captcha|security check", message, re.I):
        causes.append("captcha")
    for line in record.get("could_not_fill", []):
        reason = line.split(":", 1)[1].strip() if ":" in line else line
        reason = re.sub(r"\d+ms", "Nms", re.sub(r"“[^”]*”|\"[^\"]*\"", "…", reason))[:90]
        causes.append("could not fill: " + (reason or "unknown"))
    if record.get("invalid"):
        causes.append("invalid after filling")
    if record.get("added") and "would stop at" in message.casefold():
        causes.append("questions only on the live form")
    if "would stop at" in message.casefold() and not causes:
        causes.append("would stop: " + message.split("would stop at:", 1)[-1][:80])
    return causes


def live_audit(urls: list[str], workers: int) -> dict:
    guard_browser()
    started = time.time()
    with ThreadPoolExecutor(workers) as pool:
        records = list(pool.map(practice, urls))
    causes: Counter = Counter()
    for record in records:
        record["causes"] = classify(record)
        causes.update(set(record["causes"]))
    clean = sum(1 for r in records if not r["causes"] and r.get("status") == "dry_run")
    return {"postings": len(records), "clean": clean, "minutes": round((time.time() - started) / 60, 1),
            "causes": dict(causes.most_common()), "blocked_requests": dict(BLOCKED.most_common(15)),
            "records": records}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", type=int, default=0, help="postings whose form definitions to read")
    parser.add_argument("--live", type=int, default=0, help="postings to fill in practice mode")
    parser.add_argument("--workers", type=int, default=4, help="practice runs side by side")
    parser.add_argument("--skip", type=int, default=0, help="skip this many postings per provider (a fresh sample)")
    parser.add_argument("--db", type=Path, help="Starployment database to take postings from (read-only)")
    parser.add_argument("--urls", nargs="*", help="posting URLs instead of a database")
    args = parser.parse_args(argv)
    setup_store()
    rows = [("", "", url) for url in args.urls] if args.urls else snapshot_jobs(args.db)
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {}
    if args.api:
        report["api"] = api_audit(sample(rows, args.api) if not args.urls else args.urls)
        print(f"Form definitions: {report['api']['postings']} postings, "
              f"unknown field types: {sorted(report['api']['unknown']) or 'none'}, errors: {len(report['api']['errors'])}")
    if args.live:
        urls = args.urls or sample(rows, args.live, args.skip)
        report["live"] = live_audit(urls[:args.live], args.workers)
        live = report["live"]
        print(f"Practice runs: {live['clean']} of {live['postings']} clean in {live['minutes']} min")
        for cause, count in live["causes"].items():
            print(f"  {count:3d}  {cause}")
    path = OUT / f"report-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=1, default=str))
    print(f"Report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
