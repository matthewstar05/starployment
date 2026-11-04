"""Hourly discovery and a durable, serialized application workflow."""
from __future__ import annotations

import asyncio
import threading
import time
import traceback
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from . import config, db, governor, jarvis
from .apply import apply_to_job
from .models import ApplyResult, Job, job_fingerprint
from .priority import held_from_automation
from .readiness import preflight_job
from .scoring import score_all
from .sources import SOURCES
from .tailor import resume_for_job, snapshot_resume_for_application

# Applications may run side by side, each in its own browser (see engine.browser_profile_slot).
# The candidate's "parallel_sends" setting decides how many; this is the hard ceiling.
MAX_PARALLEL_SENDS = 24
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


class PostingGone(PostingRefreshError):
    """Before anything was sent, the posting turned out closed (404/410) or no longer eligible.

    Automatic runs skip these quietly instead of leaving an attempt for the candidate to review."""


def _closed_posting(error: BaseException) -> bool:
    """True when the ATS answered that the posting no longer exists (404/410, or Ashby's empty listing)."""
    seen = error
    while seen is not None:
        response = getattr(seen, "response", None)
        if getattr(response, "status_code", None) in (404, 410) or "Public posting was not found" in str(seen):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


# Discovered company boards read per provider in one scan. Boards rotate, least
# recently read first, so every discovered board is read every few scans.
DISCOVERED_BOARDS_PER_SCAN = 120
# A Workday board needs a request per page and per description: read fewer at a time.
DISCOVERED_WORKDAY_PER_SCAN = 25
BOARD_SOURCES = ("greenhouse", "lever", "ashby", "workday")


def _scan_settings(settings: dict) -> dict:
    """This scan's view of settings: configured boards plus a rotating slice of discovered ones."""
    import copy
    scan = copy.deepcopy(settings)
    if settings.get("discover_boards", True):
        companies = settings.get("companies") or {}
        scan["_discovered_boards"] = {
            ats: db.boards_for_scan(ats, set(companies.get(ats) or []),
                                    DISCOVERED_WORKDAY_PER_SCAN if ats == "workday" else DISCOVERED_BOARDS_PER_SCAN)
            for ats in BOARD_SOURCES if settings.get("sources", {}).get(ats)
        }
    with db.conn() as c:
        scan["_known_postings"] = {r["canonical_key"] for r in c.execute(
            "SELECT canonical_key FROM jobs WHERE ats='greenhouse' AND length(description) > 0").fetchall()}
    return scan


# Settings that change a score (desktop/controller.SCORING_KEYS lists the same ones).
_SCORING_SETTINGS = ("target_titles", "preferred_locations", "exclude_title_words", "include_internships",
                     "remote_ok", "min_salary", "years_experience", "favorite_locations")
# A full rescore also refreshes time-based parts (freshness, posting age); between
# full passes only postings inserted or changed since the last pass are scored.
FULL_RESCORE_SECONDS = 6 * 3600
_rescored: dict = {"key": None, "at": 0.0, "since": ""}


def rescore(force: bool = False) -> None:
    """Refresh rankings for postings that changed, or for all after preferences change.

    Scoring the whole library read every description and held the database for
    seconds, twice per cycle; most cycles change only a few postings.
    """
    import hashlib
    import json as _json
    from .models import now_iso
    profile, settings = db.get_profile(), db.get_settings()
    key = (str(config.DB_PATH), hashlib.sha256(
        ((profile.model_dump_json() if profile else "") + _json.dumps(
            {k: settings.get(k) for k in _SCORING_SETTINGS}, sort_keys=True, default=str)).encode()).hexdigest())
    started = now_iso()
    full = (force or key != _rescored["key"] or not _rescored["since"]
            or time.time() - _rescored["at"] > FULL_RESCORE_SECONDS)
    jobs = db.list_jobs(status="all", limit=100000) if full else db.jobs_changed_since(_rescored["since"])
    if jobs:
        db.update_scores(score_all(jobs, profile, settings))
    _rescored["since"] = started
    if full:
        _rescored.update(key=key, at=time.time())


def hosted_posting_url(job: Job) -> str:
    """The job's link on its public job board.

    Many Greenhouse employers (Stripe, Airbnb, Coinbase...) list their own
    careers page as the posting URL; the board link is the apply URL.
    """
    from .sources.import_job import JobImportError, parse_job_url
    for url in (job.apply_url, job.url):
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
    if job.ats not in ("greenhouse", "lever", "ashby", "workable", "rippling"):
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
        if _closed_posting(error):
            raise PostingGone("The employer has closed this posting.") from error
        raise PostingRefreshError("Could not verify the current public posting. Review the listing before continuing.") from error


SCAN_SECONDS = 15 * 60


def _until_timeout(iterator):
    """Yield from an ``as_completed`` iterator, stopping quietly at its timeout."""
    from concurrent.futures import TimeoutError as FuturesTimeout
    try:
        yield from iterator
    except FuturesTimeout:
        return


def scan_once() -> dict:
    if not _scan_lock.acquire(blocking=False):
        return {"skipped": "scan already running"}
    STATE.running_scan = True
    run_id = None
    log: list[str] = []
    found = new = 0
    status = "ok"
    try:
        current = db.get_settings()
        if jarvis.active(current) and jarvis.hold("scan", current):
            db.add_event("JARVIS deferred its scan while the computer is busy")
            return {"status": "deferred", "found": 0, "new": 0}
        run_id = db.create_run()
        settings = _scan_settings(db.get_settings())
        enabled = [n for n, on in settings.get("sources", {}).items() if on and n in SOURCES]
        db.add_event(f"Scan started: {', '.join(enabled) or 'no enabled sources'}")
        jobs: list[Job] = []
        t0 = time.time()
        successful = 0
        boards_read: dict[str, set[str]] = {}
        # A scan always finishes: a source still running after SCAN_SECONDS (a read the network
        # dropped mid-reply, say) is reported and left behind, so it cannot hold up every later
        # cycle. The pool is not waited on for the same reason.
        ex = ThreadPoolExecutor(max_workers=jarvis.parallel(settings, "_source_workers", 6, 6),
                                initializer=governor.same_qos(), thread_name_prefix="source")
        futures = {ex.submit(SOURCES[n], settings): n for n in enabled}
        try:
            finished = as_completed(futures, timeout=SCAN_SECONDS)
            for future in _until_timeout(finished):
                name = futures[future]
                try:
                    result = future.result()
                    jobs.extend(result)
                    successful += 1
                    log.append(f"{name}: {len(result)} jobs")
                    if name in db.FULL_LISTING_ATS:
                        boards_read[name] = set(getattr(result, "boards_read", ()) or ())
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
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        stuck = [name for future, name in futures.items() if not future.done()]
        if stuck:
            status = "partial"
            log.append(f"{', '.join(stuck)}: ERROR still running after {SCAN_SECONDS // 60} minutes")
            db.add_event(f"Scan went ahead without {', '.join(stuck)}: no reply within {SCAN_SECONDS // 60} "
                         "minutes", "warn")
        if enabled and not successful:
            status = "error"
        try:
            from .sources.base import resolve_hosted_greenhouse
            hosted = resolve_hosted_greenhouse(jobs)
            if hosted:
                log.append(f"linked {hosted} careers-page posting(s) to their Greenhouse boards")
        except Exception as error:  # noqa: BLE001 - never lose a scan over this
            log.append(f"careers-page links: WARNING {error}")
        from .locations import is_location_acceptable
        by_key: dict[str, Job] = {}
        for job in jobs:
            if not job.url or not is_location_acceptable(job.location):
                continue
            key = job_fingerprint(job)
            if key not in by_key or len(job.description) > len(by_key[key].description):
                by_key[key] = job
        jobs = list(by_key.values())
        found = len(jobs)
        new = db.upsert_jobs(jobs)
        db.mark_seen([job.id for job in jobs], t0)   # upsert_jobs points each job at its stored row
        try:
            pruned = db.prune_jobs(settings, boards_read=boards_read, scan_started=t0)
            if pruned["closed"] or pruned["stale"]:
                log.append(f"pruned {pruned['closed']} closed and {pruned['stale']} long-unseen posting(s)")
                db.add_event(f"Removed {pruned['closed'] + pruned['stale']} postings that are no longer hiring "
                             f"({pruned['closed']} closed on their boards, {pruned['stale']} not seen for weeks)")
            db.compact()
        except Exception as error:  # noqa: BLE001 - pruning must never cost a scan
            log.append(f"pruning: WARNING {error}")
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
              resume_override: str | None = None, assist: bool = False, required_jarvis_mode: str | None = None) -> dict:
    """Reserve once, record before submitting, and persist the outcome atomically.

    ``extra_answers`` are answers the candidate approved for this one
    application (question label -> answer). They apply to this attempt only.
    ``resume_override`` is the resume chosen when the application was prepared.
    ``assist`` opens a visible window the candidate finishes and submits themselves.
    """
    if application_id is None:
        try:
            application_id = queue_application(job_id)
        except ValueError as error:
            return {"error": str(error), "status": "blocked"}
    earlier = (db.get_application(application_id) or {}).get("log") or [] if assist else []
    with _apply_slots:
        if not db.start_application(application_id, job_id):
            return {"error": "Application reservation is no longer available", "status": "blocked"}
        STATE.running_apply = job_id
        STATE.running_applies.add(job_id)
        resume = ""
        skipped = False
        try:
            job = db.get_job(job_id)
            profile = db.get_profile()
            if job is None or profile is None:
                raise ValueError("Job or profile no longer exists")
            settings = db.get_settings()
            job = enrich_job(job)
            if required_jarvis_mode and db.get_settings().get("jarvis_mode") != required_jarvis_mode:
                # JARVIS was switched (or turned off) while this posting was refreshed: not a
                # reason to set the posting aside, only to not send it now.
                raise PostingRefreshError("JARVIS authorization or eligibility changed before sending.")
            if require_eligible:
                settings = db.get_settings()
                min_score = float(settings.get("min_score_to_apply", 60))
                if (job.score_json.get("auto_apply_eligible") is not True or job.score < min_score
                        or not jarvis.automatic(settings) or not settings.get("setup_complete")):
                    raise PostingGone(
                        "The current posting no longer meets your automatic application criteria. "
                        "Review the refreshed role before applying."
                    )
                if held_from_automation(job, settings.get("priority_companies") or []):
                    raise PostingRefreshError(
                        "This is a starred or priority-company role, so it waits for your own approval. "
                        "Prepare it and use Approve & send."
                    )
            if required_jarvis_mode:
                if settings.get("jarvis_mode") != required_jarvis_mode or not jarvis.eligible(job, settings):
                    raise PostingRefreshError("JARVIS authorization or eligibility changed before sending.")
                if jarvis.hold("send", settings):
                    raise PostingRefreshError("JARVIS stopped before sending because the computer is busy.")
                settings = {**settings, "show_browser": False}
            if extra_answers:
                import copy
                settings = copy.deepcopy(settings)
                answers = settings.setdefault("answers", {})
                answers["field_answers"] = {**(answers.get("field_answers") or {}), **extra_answers}
                # Kept apart as well: the candidate approved these for this application, so a
                # "Yes" may tick an acknowledgement box (saved answers never can).
                answers["approved_answers"] = dict(extra_answers)
            db.add_event(f"Applying: {job.company} / {job.title}", "info", job_id)
            from pathlib import Path
            chosen = resume_override if resume_override and Path(resume_override).is_file() else None
            resume = snapshot_resume_for_application(
                chosen or resume_for_job(profile, job, settings), application_id,
            )
            result = apply_to_job(job, profile, settings, resume, dry_run=dry_run, assist=assist)
            if result.status not in ("applied", "dry_run", "needs_human", "failed"):
                raise ValueError(f"Unknown application outcome: {result.status}")
        except Exception as error:  # noqa: BLE001
            if isinstance(error, PostingGone) and require_eligible:
                # Nothing was sent (the refresh runs before the submit boundary): an automatic
                # run skips a closed or no-longer-eligible posting instead of asking for a review.
                gone = str(error)
                result = ApplyResult(status="failed", message=gone, resume_path=resume, log=[gone])
                skipped = True
            elif isinstance(error, PostingRefreshError):
                result = ApplyResult(status="needs_human", message=str(error), resume_path=resume,
                                     log=[traceback.format_exc()[-2000:]])
            else:
                result = ApplyResult(status="failed", message=str(error), resume_path=resume,
                                     log=[traceback.format_exc()[-2000:]])
        # A browser engine that found the posting closed (Workday's "page doesn't exist") before
        # anything was sent: an automatic run skips it, like a closed board posting, instead of
        # leaving an Issue for the morning.
        if (require_eligible and not skipped and result.status == "failed" and not result.submission_attempted
                and str(result.message).startswith("The employer has closed this posting")):
            skipped = True
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
            if earlier:   # finishing an earlier attempt in the browser keeps its evidence
                result.log = [*earlier, "Finishing in a browser window with you", *result.log]
            db.finish_application(application_id, result.status, result.log, result.screenshots, result.unanswered,
                                  "" if result.status in ("applied", "dry_run") else result.message,
                                  result.resume_path or resume or None, result.submission_attempted,
                                  questions=result.questions)
            if skipped and not result.submission_attempted:
                db.hide_application(application_id)
                db.set_job_status(job_id, "skipped")
                db.add_event(f"Skipped: {result.message}", "info", job_id)
                return {"application_id": application_id, **result.model_dump(), "status": "skipped"}
            level = {"applied": "success", "dry_run": "info", "needs_human": "warn", "failed": "error"}[result.status]
            db.add_event(f"{result.status}: {job_id}: {result.message}", level, job_id)
            return {"application_id": application_id, **result.model_dump()}
        finally:
            STATE.running_applies.discard(job_id)
            STATE.running_apply = next(iter(STATE.running_applies), None)


def auto_apply_batch() -> list[dict]:
    """The scheduled cycle's unattended sends. Only JARVIS sends unattended (``jarvis.automatic``):
    with JARVIS OFF, nothing goes out without the candidate pressing send."""
    settings = db.get_settings()
    if not jarvis.automatic(settings) or not settings.get("setup_complete"):
        return []
    return jarvis_batch()


def _set_aside(job: Job, reason: str, kind: str = "transient") -> None:
    """Leave a posting for the candidate without retrying it every cycle (jarvis.set_aside)."""
    if db.hold_for_jarvis(job.id, reason, jarvis.set_aside_seconds(kind)):
        db.add_event(f"JARVIS left this application for review: {reason[:300]}", "warn", job.id)


def _set_aside_kind(job: Job, error: Exception) -> str:
    """How long a failure should keep a posting aside (see jarvis.SET_ASIDE_SECONDS)."""
    from . import prepare
    if isinstance(error, PostingRefreshError):
        return "posting"
    if str(error).startswith("Answer the required questions first"):
        draft = db.draft_for_job(job.id)
        # Only uploads and answers AI Autofill may not give remain: nothing will change
        # until the candidate looks. An open question means an AI call fell short; retry soon.
        if draft and not any(prepare.missing(q) and prepare._open(q) for q in draft["questions"]):
            return "candidate"
    return "transient"


def _headroom_for(mode: str) -> bool:
    """Inside a JARVIS cycle, wait out a busy spell instead of dropping the rest of the cycle.

    Returns False when the cycle should end: the app is stopping or the mode changed. The wait
    is bounded by ``jarvis.hold``, which lets work go ahead once the Mac has stayed busy for
    its deferral limit; JARVIS sits idle meanwhile, so waiting adds no load.
    """
    while True:
        current = db.get_settings()
        if STATE.stop or current.get("jarvis_mode") != mode:
            return False
        if not jarvis.hold("apply", current):
            return True
        time.sleep(jarvis.WAIT_POLL_SECONDS)


def _after_workday(job: Job, result: dict) -> None:
    """A Workday send that stopped for the candidate (an employer-wide page or question, a sign-in)
    would most likely stop the same way at that employer's other postings: set them aside, so an
    employer costs one attempt, not one per posting. They come back after
    jarvis.SET_ASIDE_SECONDS or when the candidate fixes the sign-in."""
    if job.ats != "workday" or result.get("status") != "needs_human":
        return
    message = str(result.get("message") or "")
    from urllib.parse import urlparse
    from .apply.adapters import adapter_for
    adapter = adapter_for(job)
    host = (urlparse(adapter.apply_url if adapter else job.apply_url).hostname or "").casefold()
    others = [job_id for job_id in db.workday_jobs_at(host) if job_id != job.id]
    reason = (f"Every {job.company} Workday application stops at the same place: {message[:220]}")
    seconds = jarvis.set_aside_seconds("candidate")
    fresh = sum(db.hold_for_jarvis(job_id, reason, seconds) for job_id in others)
    if fresh:
        db.add_event(f"JARVIS set aside {fresh} more {job.company} posting(s): {message[:200]}", "warn", job.id)


def _jarvis_attempt(job: Job, mode: str, sweep: bool, profile) -> tuple[str, dict | None]:
    """One JARVIS application: ("result", outcome), ("skip", None) to try another, or ("stop", None)."""
    from . import prepare
    current = db.get_settings()
    job = db.get_job(job.id)
    if job is None or not jarvis.eligible(job, current) or not jarvis.not_repeated([job]):
        return "skip", None
    if job.ats == "workday":
        from . import vault
        blocker = vault.sign_in_blocker(job.apply_url or job.url)
        if blocker:
            _set_aside(job, blocker, "candidate")
            return "skip", None
    if not sweep:
        readiness = preflight_job(job, profile, current)
        if readiness.status == "missing_facts":
            _set_aside(job, "Saved answers are missing for: " + ", ".join(readiness.missing or ())[:250],
                       "candidate")
            return "skip", None
        result = apply_one(job.id, require_eligible=True, required_jarvis_mode=mode)
        _after_workday(job, result)
        return ("skip", None) if result.get("status") == "skipped" else ("result", result)
    try:
        draft = db.draft_for_job(job.id)
        # Errors, interrupted preparations, and uncertain submissions wait for the owner.
        if draft and (draft["status"] not in ("ready", "needs_answers", "approved")
                      or draft.get("application_id")):
            return "skip", None
        if draft is None:
            job = enrich_job(job)
            if not jarvis.eligible(job, db.get_settings()):
                return "skip", None
            draft_id = prepare.prepare_job(job, profile, current)
        else:
            draft_id = draft["id"]
        if not _headroom_for(mode):
            return "stop", None
        draft = db.get_draft(draft_id)
        if any(prepare._open(q) for q in draft["questions"]):
            prepare.autofill(draft_id, current)
        if not _headroom_for(mode):
            return "stop", None
        current = db.get_settings()
        if not jarvis.eligible(db.get_job(job.id), current):
            return "skip", None
        prepare.approve_and_queue(draft_id)
        db.add_event(f"JARVIS {jarvis.label(mode)} authorized this draft, including its application-only "
                     "AI Autofill answers", "info", job.id)
        result = prepare.send_draft(draft_id, parallel=jarvis.parallel(current, "parallel_sends", 4, 24),
                                    unattended=True)
    except PostingGone:
        db.set_job_status(job.id, "skipped")
        return "skip", None
    except Exception as error:  # one unavailable form/agent must not stop the overnight cycle
        _set_aside(job, str(error), _set_aside_kind(job, error))
        return "skip", None
    _after_workday(job, result)
    return ("skip", None) if result.get("status") == "skipped" else ("result", result)


def jarvis_batch() -> list[dict]:
    """Standing, mode-scoped authorization, always through the durable send pipeline.

    AI answers remain on their own draft. Neither candidate facts nor the answer
    bank are changed. An attempt of any outcome is never retried by JARVIS. A posting
    it had to leave for the candidate is set aside (``jarvis.set_aside``), so later
    cycles spend their time on postings that can still go, not the same blocked ones.
    """
    settings = db.get_settings()
    mode = jarvis.mode_of(settings)
    if not jarvis.active(settings) or not settings.get("setup_complete") or STATE.stop:
        return []
    if jarvis.hold("apply", settings):
        db.add_event("JARVIS deferred applications while the computer is busy")
        return []
    rescore()
    profile = db.get_profile()
    if profile is None:
        return []
    with db.conn() as connection:
        attempted = {row["job_id"] for row in connection.execute("SELECT job_id FROM applications")}
    floor = float(settings.get("min_score_to_apply", 60))
    sweep = jarvis.sweeping(mode)
    candidates = [job for status in (("new", "prepared") if sweep else ("new",))
                  for job in db.list_jobs(status=status, min_score=floor, limit=100000)
                  if job.id not in attempted and jarvis.eligible(job, settings)]
    if sweep:
        candidates = jarvis.still_listed(candidates)
    waiting = jarvis.set_aside(job.id for job in candidates)
    candidates = jarvis.diversify(jarvis.not_repeated(
        jarvis.rank([job for job in candidates if job.id not in waiting], mode)), settings)
    maximum = int(settings.get("max_apply_per_run", 3))
    # NEW sends one at a time. ALL and ALL+ run whole applications (prepare, AI Autofill,
    # send) side by side, up to their send concurrency (ALL half of parallel_sends, ALL+ all
    # of it), each in its own browser; one employer at a time, as before.
    workers = jarvis.parallel(settings, "parallel_sends", 4, MAX_PARALLEL_SENDS) if sweep else 1
    results: list[dict] = []
    employers: set[str] = set()
    stop = threading.Event()
    lock = threading.Lock()

    def attempt(job: Job, company: str) -> None:
        try:
            outcome = _jarvis_attempt(job, mode, sweep, profile)
        except Exception:  # noqa: BLE001 - one failure never ends the overnight cycle
            log.exception("JARVIS attempt failed")
            outcome = ("skip", None)
        with lock:
            kind, result = outcome
            if kind == "stop":
                stop.set()
            if kind != "result":
                employers.discard(company)   # nothing went to this employer: another role may go
                return
            results.append(result)
            if result.get("status") in ("blocked", "busy"):
                stop.set()

    # Set-aside postings are already gone, so take the ranking in order (newest and best
    # first); the probe bound keeps one cycle's public requests and agent calls in check.
    from concurrent.futures import FIRST_COMPLETED, wait as wait_for
    inflight: set = set()
    with ThreadPoolExecutor(max_workers=workers, initializer=governor.same_qos(),
                            thread_name_prefix="jarvis") as pool:
        for job in candidates[:max(24, maximum * 8)]:
            # Wait for a free worker and a place in this run's budget; an attempt that ends
            # without sending (skipped, set aside) gives its place back.
            while True:
                inflight = {future for future in inflight if not future.done()}
                with lock:
                    room = not stop.is_set() and len(results) + len(inflight) < maximum
                if (room and len(inflight) < workers) or not inflight:
                    break
                _done, inflight = wait_for(inflight, return_when=FIRST_COMPLETED)
            if not room or not _headroom_for(mode):
                break
            company = job.company.strip().casefold()
            with lock:
                if company in employers:
                    continue
                employers.add(company)
            inflight.add(pool.submit(attempt, job, company))
        wait_for(inflight)
    db.add_event(f"JARVIS {jarvis.label(mode)} finished: {len(results)} attempt(s), "
                 f"{sum(r.get('status') == 'applied' for r in results)} confirmed"
                 + (f"; {len(waiting)} set aside for you" if waiting else ""))
    return results


DEFERRED_RETRY_SECONDS = 60


async def loop() -> None:
    """Scan while the server runs, using the latest interval after each cycle."""
    STATE.stop = False
    await asyncio.sleep(2)
    while not STATE.stop:
        cycle_started = time.time()
        interval = jarvis.interval_minutes(db.get_settings()) * 60
        STATE.next_scan_at = datetime.fromtimestamp(cycle_started + interval, tz=timezone.utc).isoformat(timespec="seconds")
        retry_at = None
        try:
            summary = await asyncio.to_thread(scan_once)
            if summary.get("status") == "deferred":
                # A busy Mac postpones the scan, not the whole cycle: ask again shortly, so
                # jarvis.hold's deferral limit can let it go ahead instead of idling an interval.
                retry_at = time.time() + DEFERRED_RETRY_SECONDS
            elif summary.get("status") in ("ok", "partial"):
                results = await asyncio.to_thread(auto_apply_batch)
                if summary.get("run_id"):
                    with db.conn() as c:
                        c.execute("UPDATE runs SET applied=? WHERE id=?",
                                  (sum(result.get("status") == "applied" for result in results), summary["run_id"]))
        except Exception as error:  # noqa: BLE001
            STATE.last_error = str(error)
            db.add_event(f"Background cycle failed: {error}", "error")
        while not STATE.stop:
            interval = jarvis.interval_minutes(db.get_settings()) * 60
            # Application time is part of the hourly cycle, not added to it.
            next_time = retry_at if retry_at is not None else cycle_started + interval
            STATE.next_scan_at = datetime.fromtimestamp(next_time, tz=timezone.utc).isoformat(timespec="seconds")
            if time.time() >= next_time:
                break
            await asyncio.sleep(1)
    STATE.next_scan_at = None
