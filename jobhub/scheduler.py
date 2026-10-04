"""Hourly discovery and a durable, serialized application workflow."""
from __future__ import annotations

import asyncio
import threading
import time
import traceback
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from . import db
from .apply import apply_to_job
from .models import ApplyResult, Job, job_fingerprint
from .priority import held_from_automation
from .readiness import preflight_job
from .scoring import score_all
from .sources import SOURCES
from .tailor import resume_for_job, snapshot_resume_for_application

# Applications may run side by side, each in its own browser (see engine.browser_profile_slot).
# The candidate's "parallel_sends" setting decides how many; this is the hard ceiling.
MAX_PARALLEL_SENDS = 8
_apply_slots = threading.BoundedSemaphore(MAX_PARALLEL_SENDS)
_scan_lock = threading.Lock()


class State:
    running_scan = False
    running_apply: str | None = None      # the most recently started application, for status displays
    running_applies: set[str] = set()
    last_scan_at: str | None = None
    next_scan_at: str | None = None
    last_error: str = ""
    stop = False


STATE = State()
log = logging.getLogger(__name__)


class PostingRefreshError(RuntimeError):
    """The current public ATS posting could not be verified safely."""


# Discovered company boards read per provider in one scan. Boards rotate, least
# recently read first, so every discovered board is read every few scans.
DISCOVERED_BOARDS_PER_SCAN = 120
BOARD_SOURCES = ("greenhouse", "lever", "ashby")


def _scan_settings(settings: dict) -> dict:
    """This scan's view of settings: configured boards plus a rotating slice of discovered ones."""
    import copy
    scan = copy.deepcopy(settings)
    if settings.get("discover_boards", True):
        companies = settings.get("companies") or {}
        scan["_discovered_boards"] = {
            ats: db.boards_for_scan(ats, set(companies.get(ats) or []), DISCOVERED_BOARDS_PER_SCAN)
            for ats in BOARD_SOURCES if settings.get("sources", {}).get(ats)
        }
    with db.conn() as c:
        scan["_known_postings"] = {r["canonical_key"] for r in c.execute(
            "SELECT canonical_key FROM jobs WHERE ats='greenhouse' AND length(description) > 0").fetchall()}
    return scan


def rescore() -> None:
    """Refresh existing rankings after candidate or targeting preferences change."""
    jobs = db.list_jobs(status="all", limit=100000)
    db.update_scores(score_all(jobs, db.get_profile(), db.get_settings()))


def hosted_posting_url(job: Job) -> str:
    """The job's link on its public job board.

    Many Greenhouse employers (Stripe, Airbnb, Coinbase...) list their own
    careers page as the posting URL; the board link is the apply URL.
    """
    from .sources.import_job import JobImportError, parse_job_url
    for url in (job.url, job.apply_url):
        try:
            parse_job_url(url)
            return url
        except JobImportError:
            continue
    return job.url


def enrich_job(job: Job) -> Job:
    """Refresh supported ATS metadata before drafting or applying.

    The public endpoint is derived from the validated board URL. A successful
    response must still identify the same vacancy; otherwise the stored posting
    is left untouched and the caller must stop.
    """
    if job.ats not in ("greenhouse", "lever", "ashby"):
        return job
    try:
        from .sources.import_job import fetch_job
        enriched = fetch_job(hosted_posting_url(job))
        if enriched.ats != job.ats or job_fingerprint(enriched) != job_fingerprint(job):
            raise PostingRefreshError("The public posting no longer matches this saved vacancy. Review it before continuing.")
        refreshed = db.refresh_job_metadata(job.id, enriched)
        score, detail = score_all([refreshed], db.get_profile(), db.get_settings())[refreshed.id]
        db.update_scores({refreshed.id: (score, detail)})
        refreshed.score, refreshed.score_json = score, detail
        return refreshed
    except Exception as error:  # noqa: BLE001
        log.warning("Could not refresh public posting %s: %s", job.id, error)
        db.add_event(f"Current posting refresh failed: {error}", "warn", job.id)
        if isinstance(error, PostingRefreshError):
            raise
        raise PostingRefreshError("Could not verify the current public posting. Review the listing before continuing.") from error


def scan_once() -> dict:
    if not _scan_lock.acquire(blocking=False):
        return {"skipped": "scan already running"}
    STATE.running_scan = True
    run_id = None
    log: list[str] = []
    found = new = 0
    status = "ok"
    try:
        run_id = db.create_run()
        settings = _scan_settings(db.get_settings())
        enabled = [n for n, on in settings.get("sources", {}).items() if on and n in SOURCES]
        db.add_event(f"Scan started: {', '.join(enabled) or 'no enabled sources'}")
        jobs: list[Job] = []
        t0 = time.time()
        successful = 0
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {ex.submit(SOURCES[n], settings): n for n in enabled}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    result = future.result()
                    jobs.extend(result)
                    successful += 1
                    log.append(f"{name}: {len(result)} jobs")
                    board_results = getattr(result, "board_results", None)
                    if board_results and name in BOARD_SOURCES:
                        db.record_board_scans(name, board_results)
                    for notice in getattr(result, "notices", []):
                        log.append(f"{name}: INFO {notice}")
                    for warning in getattr(result, "warnings", []):
                        status = "partial"
                        log.append(f"{name}: WARNING {warning}")
                        db.add_event(f"Source {name}: {warning}", "warn")
                except Exception as error:  # noqa: BLE001
                    status = "partial"
                    log.append(f"{name}: ERROR {error}")
                    db.add_event(f"Source {name} failed: {error}", "warn")
        if enabled and not successful:
            status = "error"
        by_key: dict[str, Job] = {}
        for job in jobs:
            if not job.url:
                continue
            key = job_fingerprint(job)
            if key not in by_key or len(job.description) > len(by_key[key].description):
                by_key[key] = job
        jobs = list(by_key.values())
        found = len(jobs)
        new = db.upsert_jobs(jobs)
        boards = db.discover_boards(jobs) if settings.get("discover_boards", True) else 0
        if boards:
            log.append(f"discovered {boards} more company board(s) to read in later scans")
        rescore()
        log.append(f"total {found} unique, {new} new, {time.time() - t0:.0f}s")
        db.add_event(f"Scan {status}: {found} jobs from {successful}/{len(enabled)} sources, {new} new",
                     "success" if status == "ok" else "warn")
        STATE.last_scan_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        STATE.last_error = "" if status == "ok" else "; ".join(line for line in log if "ERROR" in line or "WARNING" in line)[-2000:]
    except Exception as error:  # noqa: BLE001
        status = "error"
        STATE.last_error = str(error)
        log.append(traceback.format_exc()[-1200:])
        db.add_event(f"Scan failed: {error}", "error")
    finally:
        try:
            if run_id is not None:
                db.finish_run(run_id, found, new, 0, status, log)
        finally:
            STATE.running_scan = False
            _scan_lock.release()
    return {"run_id": run_id, "found": found, "new": new, "status": status, "log": log}


def queue_application(job_id: str, parallel: int = 1) -> int:
    """Reserve one application. ``parallel`` is how many may be queued or running at once."""
    profile = db.get_profile()
    if profile is None or not profile.resume_path:
        raise ValueError("Upload a resume before applying")
    from pathlib import Path
    if not Path(profile.resume_path).is_file():
        raise ValueError("The saved resume is missing; upload it again")
    return db.reserve_application(job_id, limit=max(1, min(int(parallel), MAX_PARALLEL_SENDS)))


def apply_one(job_id: str, dry_run: bool | None = None, application_id: int | None = None,
              require_eligible: bool = False, extra_answers: dict | None = None,
              resume_override: str | None = None) -> dict:
    """Reserve once, record before submitting, and persist the outcome atomically.

    ``extra_answers`` are answers the candidate approved for this one
    application (question label -> answer). They apply to this attempt only.
    ``resume_override`` is the resume chosen when the application was prepared.
    """
    if application_id is None:
        try:
            application_id = queue_application(job_id)
        except ValueError as error:
            return {"error": str(error), "status": "blocked"}
    with _apply_slots:
        if not db.start_application(application_id, job_id):
            return {"error": "Application reservation is no longer available", "status": "blocked"}
        STATE.running_apply = job_id
        STATE.running_applies.add(job_id)
        resume = ""
        try:
            job = db.get_job(job_id)
            profile = db.get_profile()
            if job is None or profile is None:
                raise ValueError("Job or profile no longer exists")
            settings = db.get_settings()
            if extra_answers:
                import copy
                settings = copy.deepcopy(settings)
                answers = settings.setdefault("answers", {})
                answers["field_answers"] = {**(answers.get("field_answers") or {}), **extra_answers}
                # Kept apart as well: the candidate approved these for this application, so a
                # "Yes" may tick an acknowledgement box (saved answers never can).
                answers["approved_answers"] = dict(extra_answers)
            job = enrich_job(job)
            if require_eligible:
                settings = db.get_settings()
                min_score = float(settings.get("min_score_to_apply", 60))
                if (job.score_json.get("auto_apply_eligible") is not True or job.score < min_score
                        or not settings.get("auto_apply") or not settings.get("setup_complete")):
                    raise PostingRefreshError(
                        "The current posting no longer meets your automatic application criteria. "
                        "Review the refreshed role before applying."
                    )
                if held_from_automation(job, settings.get("priority_companies") or []):
                    raise PostingRefreshError(
                        "This is a starred or priority-company role, so it waits for your own approval. "
                        "Prepare it and use Approve & send."
                    )
            db.add_event(f"Applying: {job.company} / {job.title}", "info", job_id)
            from pathlib import Path
            chosen = resume_override if resume_override and Path(resume_override).is_file() else None
            resume = snapshot_resume_for_application(
                chosen or resume_for_job(profile, job, settings), application_id,
            )
            result = apply_to_job(job, profile, settings, resume, dry_run=dry_run)
            if result.status not in ("applied", "dry_run", "needs_human", "failed"):
                raise ValueError(f"Unknown application outcome: {result.status}")
        except Exception as error:  # noqa: BLE001
            if isinstance(error, PostingRefreshError):
                result = ApplyResult(status="needs_human", message=str(error), resume_path=resume,
                                     log=[traceback.format_exc()[-2000:]])
            else:
                result = ApplyResult(status="failed", message=str(error), resume_path=resume,
                                     log=[traceback.format_exc()[-2000:]])
        try:
            # If an exception follows a submit click, preserve the uncertainty rather than offering a retry.
            record = db.get_application(application_id)
            if record and record.get("submission_attempted"):
                result.submission_attempted = True
                if result.status == "failed":
                    result.status = "needs_human"
                    result.message = "Submission was attempted but its result is unknown. " + result.message
            # An engine/plugin bug must not turn a form-fill result into a
            # confirmed application. Require the durable pre-click marker as
            # well as the engine's employer confirmation signal.
            if result.status == "applied" and not result.submission_attempted:
                result.status = "needs_human"
                result.message = (
                    "The application engine reported success without a durable "
                    "submission attempt record. Review the employer site before retrying."
                )
            db.finish_application(application_id, result.status, result.log, result.screenshots, result.unanswered,
                                  "" if result.status in ("applied", "dry_run") else result.message,
                                  result.resume_path or resume or None, result.submission_attempted,
                                  questions=result.questions)
            level = {"applied": "success", "dry_run": "info", "needs_human": "warn", "failed": "error"}[result.status]
            db.add_event(f"{result.status}: {job_id}: {result.message}", level, job_id)
            return {"application_id": application_id, **result.model_dump()}
        finally:
            STATE.running_applies.discard(job_id)
            STATE.running_apply = next(iter(STATE.running_applies), None)


def _auto_apply_preflight_window(candidates: list[Job], budget: int) -> list[Job]:
    """Return a bounded, rotating slice of ranked candidates for public preflight.

    The scan run id is durable across restarts and advances once per scheduled
    scan. Rotating the probe window prevents a fixed set of high-ranked forms
    with missing answers from starving roles later in the ranked list. The
    returned slice remains in match order so answerable roles in the slice keep
    the normal ranking preference.
    """
    if len(candidates) <= budget:
        return candidates
    with db.conn() as connection:
        row = connection.execute("SELECT COALESCE(MAX(id), 0) AS id FROM runs").fetchone()
    scan_id = int(row["id"] if row else 0)
    start = (max(0, scan_id - 1) * budget) % len(candidates)
    end = start + budget
    if end <= len(candidates):
        return candidates[start:end]
    # Wrap at the end of the ranked list, then restore its original order.
    selected = candidates[start:] + candidates[:end - len(candidates)]
    positions = {job.id: index for index, job in enumerate(candidates)}
    return sorted(selected, key=lambda job: positions[job.id])


def auto_apply_batch() -> list[dict]:
    settings = db.get_settings()
    if not settings.get("auto_apply") or not settings.get("setup_complete"):
        return []
    rescore()
    priority = settings.get("priority_companies") or []
    # Priority companies and starred roles are never applied to unattended.
    candidates = [job for job in db.list_jobs(status="new", min_score=float(settings.get("min_score_to_apply", 60)), limit=100000)
                  if job.ats in ("greenhouse", "ashby", "lever") and job.score_json.get("auto_apply_eligible", True)
                  and not held_from_automation(job, priority)]
    results = []
    attempted_companies: set[str] = set()
    max_applications = int(settings.get("max_apply_per_run", 3))
    profile = db.get_profile()
    # A public Greenhouse preflight can reject an unanswerable form without
    # reserving the browser or using one of this cycle's application slots.
    # Bound public API requests even when the candidate has few saved answers.
    preflight_budget = max(24, max_applications * 8)
    for job in _auto_apply_preflight_window(candidates, preflight_budget):
        if len(results) >= max_applications:
            break
        if not db.get_settings().get("auto_apply") or STATE.stop:
            break
        company_key = job.company.strip().casefold()
        if company_key and company_key in attempted_companies:
            continue
        readiness = preflight_job(job, profile, settings)
        if readiness.status == "missing_facts":
            log.info("Skipped %s before application: required answers missing for %s",
                     job.id, ", ".join(readiness.missing))
            continue
        result = apply_one(job.id, require_eligible=True)
        results.append(result)
        if company_key:
            attempted_companies.add(company_key)
        if result.get("status") == "blocked":
            break
    return results


async def loop() -> None:
    """Scan while the server runs, using the latest interval after each cycle."""
    STATE.stop = False
    await asyncio.sleep(2)
    while not STATE.stop:
        cycle_started = time.time()
        interval = max(5, int(db.get_settings().get("scan_interval_min", 60))) * 60
        STATE.next_scan_at = datetime.fromtimestamp(cycle_started + interval, tz=timezone.utc).isoformat(timespec="seconds")
        try:
            summary = await asyncio.to_thread(scan_once)
            if summary.get("status") in ("ok", "partial"):
                results = await asyncio.to_thread(auto_apply_batch)
                if summary.get("run_id"):
                    with db.conn() as c:
                        c.execute("UPDATE runs SET applied=? WHERE id=?",
                                  (sum(result.get("status") == "applied" for result in results), summary["run_id"]))
        except Exception as error:  # noqa: BLE001
            STATE.last_error = str(error)
            db.add_event(f"Background cycle failed: {error}", "error")
        while not STATE.stop:
            interval = max(5, int(db.get_settings().get("scan_interval_min", 60))) * 60
            # Application time is part of the hourly cycle, not added to it.
            next_time = cycle_started + interval
            STATE.next_scan_at = datetime.fromtimestamp(next_time, tz=timezone.utc).isoformat(timespec="seconds")
            if time.time() >= next_time:
                break
            await asyncio.sleep(1)
    STATE.next_scan_at = None
