"""Connect the native window to Starployment's durable discovery and apply pipeline."""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox

from .. import config, db, email_tracker, governor, jarvis, prepare, scheduler
from ..setup import merge_resume_into_library, save_resume_library, save_setup
from .ui import JobHubWindow, SetupWizard

# Settings keys that change job rankings. Saving anything else skips the
# full-catalog rescore, which can take seconds on a large job table.
MAIL_RETRY_MS = 5 * 60_000   # after the mail server could not be reached
# ALL+: a cycle that ran past its interval starts the next one this soon, not a whole interval later.
PLUS_OVERRUN_PAUSE_SECONDS = 30
# Short tasks the candidate is waiting on; they get their own thread so they
# never queue behind preparation or Luna, which can hold the assist pool for minutes.
QUICK_TASKS = frozenset({"preview", "vault_auth", "agent_status", "agent_test", "mail_test", "sheet_test", "import_resume"})

SCORING_KEYS = frozenset({
    "target_titles", "preferred_locations", "exclude_title_words", "include_internships",
    "remote_ok", "min_salary", "years_experience",
})


class WorkerSignals(QObject):
    finished = Signal(str, object)
    failed = Signal(str, str)
    progress = Signal(str, int, int)
    assist_finished = Signal(str, object, object)
    assist_failed = Signal(str, object, str)
    send_done = Signal(int, object, str)      # draft id, result, error


class DesktopController(QObject):
    jobs_loaded = Signal(object, object)      # (cache key, job data) from the background loader
    """Scans, imports, and retries run on one worker; approved applications are sent by a
    small pool of background browsers (Settings → Automation → Send at once). Nothing
    blocks Qt's UI thread."""

    def __init__(self, window: JobHubWindow, *, scheduler_enabled: bool = True,
                 show_setup_on_start: bool = True, force_setup_on_start: bool = False):
        super().__init__(window)
        db.init()
        db.recover_interrupted()
        db.recover_drafts()
        try:
            prepare.repair_drafts()   # drafts from earlier versions may repeat checkbox options
            db.hide_superseded_attempts()   # earlier attempts of postings that were since sent
            db.hide_closed_posting_attempts()   # automatic attempts stopped by a closed posting
        except Exception as exc:  # noqa: BLE001 - never block startup on a cleanup
            db.add_event(f"Could not tidy prepared applications: {exc}", "warn")
        app = QApplication.instance()
        if app is not None:
            from .styles import apply_appearance, apply_typeface

            startup = db.get_settings()
            apply_typeface(app, startup.get("font", "Claude"), restyle=False)   # appearance restyles next
            apply_appearance(app, startup.get("appearance", "system"))
        self.window = window
        self.scheduler_enabled = scheduler_enabled
        self._stopping = threading.Event()
        self._tasks: queue.Queue[tuple[str, Any] | None] = queue.Queue()
        self._pending_scan = False
        self._next_cycle_at = 0.0
        self.signals = WorkerSignals()
        self.signals.finished.connect(self._task_finished)
        self.signals.failed.connect(self._task_failed)
        self.signals.progress.connect(self._prep_progress)
        self.signals.assist_finished.connect(self._assist_finished)
        self.signals.assist_failed.connect(self._assist_failed)
        self.signals.send_done.connect(self._send_done)
        self._worker = threading.Thread(target=self._work, name="starployment-browser-worker", daemon=True)
        self._worker.start()
        # Preparation, Luna, inbox checks, and connection tests never use the
        # browser, so they run beside it and never delay a send or a scan.
        self._assist = ThreadPoolExecutor(max_workers=4, thread_name_prefix="starployment-assist")
        self._quick = ThreadPoolExecutor(max_workers=1, thread_name_prefix="starployment-quick")
        # Reloads the job library's page and counts after background changes, so the
        # window never waits on those whole-library queries while scans and sends run.
        self._job_loader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="starployment-jobs")
        self._job_loading: object = None
        self.jobs_loaded.connect(self._jobs_ready)
        self._prep_stop = threading.Event()
        self._preparing = False
        self._preparing_jobs: set[str] = set()
        self._sending: set[int] = set()          # queued or in flight
        self._in_browser: set[int] = set()       # drafts the candidate finishes in a visible window
        self._send_queue: deque[int] = deque()
        self._send_active: dict[int, str] = {}   # draft id -> employer, while its browser runs
        self._send_counts = {"sent": 0, "other": 0}
        self._mail_state: dict = {"last_checked": None, "unmatched": 0}
        self._mail_syncing = threading.Event()
        self._mail_retry_pending = False
        self._portal_checking = threading.Event()
        self._sheet_syncing = threading.Event()
        self._sheet_after_checks = False

        window.scan_requested.connect(self.scan)
        window.apply_requested.connect(self.apply)
        window.status_requested.connect(self.set_job_status)
        window.import_requested.connect(self.import_job)
        window.save_profile_requested.connect(self.save_profile)
        window.save_settings_requested.connect(self.save_settings)
        window.jarvis_mode_requested.connect(self.set_jarvis_mode)
        window.appearance_requested.connect(self.set_appearance)
        window.typeface_requested.connect(self.set_typeface)
        window.setup_requested.connect(self.open_setup)
        window.answer_questions_requested.connect(self.open_setup)
        window.open_url_requested.connect(self.open_url)
        window.apps.note_save_requested.connect(self.save_application_note)
        window.apps.resume_requested.connect(self.open_resume)
        window.jobs.query_changed.connect(self.refresh)
        # Apply flow and prepared applications
        window.dashboard.apply_plan_requested.connect(self.start_plan)
        window.dashboard.prepare_requested.connect(self.prepare_job)
        window.jobs.prepare_requested.connect(self.prepare_job)
        window.apps.prepare_requested.connect(self.prepare_job)
        window.apps.approve_send_requested.connect(self.approve_and_send)
        window.apps.save_draft_requested.connect(self.save_draft)
        window.apps.luna_requested.connect(self.luna_draft)
        window.apps.remove_draft_requested.connect(self.remove_draft)
        window.apps.send_approved_requested.connect(self.send_approved)
        window.apps.reset_requested.connect(self.reset_unfinished)
        window.apps.autofill_all_requested.connect(self.autofill_all)
        window.apps.reopen_requested.connect(self.reopen_rejected)
        window.apps.browser_draft_requested.connect(self.finish_draft_in_browser)
        window.apps.browser_attempt_requested.connect(self.finish_attempt_in_browser)
        # Stars, marking as sent, and removing from the Applications list
        window.jobs.star_requested.connect(self.set_star)
        window.apps.star_requested.connect(self.set_star)
        window.jobs.mark_sent_requested.connect(self.mark_sent)
        window.apps.mark_sent_requested.connect(self.mark_sent)
        window.apps.hide_requested.connect(self.hide_application)
        window.apps.discard_draft_requested.connect(self.discard_draft)
        # Tracker
        window.tracker.stage_change_requested.connect(self.set_stage)
        window.tracker.note_save_requested.connect(self.save_application_note)
        window.tracker.sync_email_requested.connect(lambda: self.sync_email(manual=True))
        window.tracker.add_manual_requested.connect(self.add_manual_application)
        # Profile: resume builder and resume files
        window.profile.import_resume_requested.connect(self.import_resume)
        window.profile.preview_requested.connect(self.preview_resume)
        files = window.profile.files
        files.add_requested.connect(self.add_resume_file)
        files.category_changed.connect(lambda fid, cat: self._resume_file_update(fid, category=cat))
        files.rename_requested.connect(lambda fid, name: self._resume_file_update(fid, label=name))
        files.default_requested.connect(lambda fid: self._resume_file_update(fid, is_default=True))
        files.enabled_changed.connect(lambda fid, en: self._resume_file_update(fid, enabled=int(en)))
        files.set_main_requested.connect(self.set_main_resume_from_bank)
        files.remove_requested.connect(self.remove_resume_file)
        files.open_requested.connect(self.open_resume)
        files.open_folder_requested.connect(self.open_resume_folder)
        files.strategy_changed.connect(self.set_resume_strategy)
        window.settings.resume_strategy_changed.connect(self.set_resume_strategy)
        window.settings.passwords_requested.connect(self.show_saved_sign_ins)
        from .. import vault
        vault.clear_export()   # an export file left by a closed session never lingers
        # Connections
        self._wire_connections(window.settings.luna, window.settings.email)
        self._wire_sheets(getattr(window.settings, "sheets", None))
        self.refresh(include_account=True)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(lambda: self.refresh(wait=False))
        self._refresh_timer.start(15_000)
        self._cycle_timer = QTimer(self)
        self._cycle_timer.timeout.connect(self._cycle_due)
        self._cycle_timer.start(2_000)
        if scheduler_enabled and db.get_settings().get("setup_complete"):
            self._next_cycle_at = time.time() + 3
        if show_setup_on_start and (force_setup_on_start or not db.get_settings().get("setup_complete")):
            QTimer.singleShot(0, self.open_setup)

    def _status(self, message: str, timeout: int = 6000, tone: str = "info") -> None:
        """Report progress in the window's toast. ``timeout`` 0 keeps it visible."""
        self.window.notify(message, tone, timeout)

    def _update_scan_state(self, settings: dict | None = None) -> None:
        settings = settings if settings is not None else db.get_settings()
        self.window.set_scan_state(
            scanning=self._pending_scan,
            next_scan_at=self._next_cycle_at or None,
            setup_complete=bool(settings.get("setup_complete")),
            scheduler_enabled=self.scheduler_enabled,
            auto_apply=jarvis.automatic(settings),
        )
        self._keep_awake(self.scheduler_enabled and jarvis.automatic(settings) and not self._stopping.is_set())

    _caffeinate: subprocess.Popen | None = None

    def _keep_awake(self, on: bool) -> None:
        """While automatic applications are on, keep the Mac from idle-sleeping so scans and sends
        keep running overnight. The display may still sleep; the assertion ends with the app."""
        if sys.platform != "darwin" or QApplication.platformName() == "offscreen":
            return
        running = self._caffeinate is not None and self._caffeinate.poll() is None
        if on and not running:
            try:
                self._caffeinate = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self._caffeinate = None
        elif not on and running:
            self._caffeinate.terminate()
            self._caffeinate = None

    def _work(self) -> None:
        while not self._stopping.is_set():
            try:
                task = self._tasks.get(timeout=0.5)
            except queue.Empty:
                continue
            if task is None:
                return
            kind, payload = task
            # Background cycles yield to whatever the user is doing: ALL runs at macOS background
            # QoS (efficiency cores, throttled I/O), others at utility. Work the user asked for
            # (an Apply click, an import) runs at the normal level.
            settings = db.get_settings() if kind == "cycle" else None
            governor.set_thread_qos(("background" if jarvis.mode_of(settings) in ("new", "all") else "utility")
                                    if settings is not None else "initiated")
            try:
                if kind == "cycle":
                    report = scheduler.scan_once()
                    results = []
                    if report.get("status") in {"ok", "partial"} and db.get_settings().get("setup_complete"):
                        results = scheduler.auto_apply_batch()
                        if report.get("run_id"):
                            with db.conn() as con:
                                con.execute("UPDATE runs SET applied=? WHERE id=?",
                                            (sum(result.get("status") == "applied" for result in results), report["run_id"]))
                    result = {"scan": report, "applications": results}
                elif kind == "apply":
                    result = scheduler.apply_one(payload)
                elif kind == "import":
                    from ..sources.import_job import fetch_job
                    job = fetch_job(payload)
                    added = db.upsert_jobs([job])
                    scheduler.rescore()
                    result = {"job_id": job.id, "company": job.company, "title": job.title, "new": bool(added)}
                else:
                    raise ValueError(f"Unknown worker task: {kind}")
                self.signals.finished.emit(kind, result)
            except Exception as exc:  # noqa: BLE001
                self.signals.failed.emit(kind, str(exc))

    def _cycle_due(self) -> None:
        if not self.scheduler_enabled or self._pending_scan or self._next_cycle_at <= 0:
            return
        if time.time() >= self._next_cycle_at:
            self.scan()

    def scan(self) -> None:
        settings = db.get_settings()
        if not settings.get("setup_complete"):
            self._status("Complete setup before scanning for jobs")
            self.open_setup()
            return
        if self._pending_scan:
            self._status("A job scan is already running")
            return
        self._pending_scan = True
        started = time.time()
        interval = jarvis.interval_minutes(settings) * 60
        self._next_cycle_at = started + interval
        scheduler.STATE.next_scan_at = str(int(self._next_cycle_at))
        self._tasks.put(("cycle", None))
        # The inbox is checked on the same schedule as the job scan. It runs
        # beside the scan, so a slow or failed scan never delays it.
        if settings.get("email_tracking", {}).get("enabled"):
            self.sync_email()
        self.check_workday()
        # Confirmed applications are copied after the inbox and Workday checks, so a
        # stage those checks just learned is included in this cycle's sheet update.
        if (settings.get("google_sheets") or {}).get("enabled"):
            self._sheet_after_checks = True
            self._maybe_sync_sheet()
        self._status("Searching your sources for new jobs…", 0, "busy")
        self._update_scan_state(settings)

    def apply(self, job_id: str) -> None:
        if not db.get_settings().get("setup_complete"):
            self._status("Complete setup before applying")
            self.open_setup()
            return
        self._tasks.put(("apply", job_id))
        self._status("Applying… Starployment will verify the employer's response", 0, "busy")

    def import_job(self, url: str) -> None:
        if not url.startswith("https://"):
            self._status("Enter an https:// employer posting URL", tone="warning")
            return
        self._tasks.put(("import", url))
        self._status("Importing posting…", 0, "busy")

    def set_job_status(self, job_id: str, status: str) -> None:
        """Skip a job or restore it to New from the Jobs context menu."""
        try:
            db.set_job_status(job_id, status)
        except ValueError as exc:
            self._status(str(exc), tone="warning")
            return
        self._status("Job skipped" if status == "skipped" else "Job restored to New", tone="success")
        self.refresh()

    def set_appearance(self, appearance: str) -> None:
        """Apply and persist a theme choice immediately, with a crossfade."""
        try:
            settings = db.set_settings({"appearance": appearance})
        except Exception as exc:  # noqa: BLE001
            self._status(f"Could not save appearance: {exc}", tone="error")
            return
        from .styles import apply_appearance

        self.window.crossfade(lambda: apply_appearance(QApplication.instance(), settings["appearance"]))

    def set_typeface(self, choice: str) -> None:
        """Apply and persist the font choice immediately, with a crossfade."""
        try:
            settings = db.set_settings({"font": choice})
        except Exception as exc:  # noqa: BLE001
            self._status(f"Could not save the font: {exc}", tone="error")
            return
        from .styles import apply_typeface

        self.window.crossfade(lambda: apply_typeface(QApplication.instance(), settings["font"]))

    def _task_finished(self, kind: str, result: object) -> None:
        if kind == "cycle":
            self._pending_scan = False
            scan = result.get("scan", {}) if isinstance(result, dict) else {}
            applications = result.get("applications", []) if isinstance(result, dict) else []
            new = int(scan.get("new", 0) or 0)
            attempts = len(applications)
            summary = f"Found {int(scan.get('found', 0) or 0):,} jobs, {new:,} new"
            if attempts:
                summary += f", {attempts} application attempt{'s' if attempts != 1 else ''}"
            if scan.get("status") == "deferred":
                self._next_cycle_at = time.time() + 60
            partial = scan.get("status") == "partial"
            self._status("JARVIS is waiting for your Mac to have more headroom" if scan.get("status") == "deferred"
                         else summary + (" · some sources had issues" if partial else ""),
                         tone="warning" if partial else "success")
            # After a long scan or wake from sleep, start one fresh interval
            # instead of trying to replay every missed hourly cycle. ALL+ (nights away) goes
            # straight on instead: a cycle that ran past its interval used to idle a whole one
            # more (2026-11-08: about five minutes of every eleven sent nothing).
            if time.time() >= self._next_cycle_at:
                current = db.get_settings()
                self._next_cycle_at = time.time() + (PLUS_OVERRUN_PAUSE_SECONDS if jarvis.plus(current)
                                                     else jarvis.interval_minutes(current) * 60)
        elif kind == "import":
            result = result if isinstance(result, dict) else {}
            self._status(f"Added {result.get('company', '')} · {result.get('title', '')}", tone="success")
            self.window.show_jobs("all")
        else:
            result = result if isinstance(result, dict) else {}
            status = str(result.get("status", "finished"))
            detail = result.get("message") or result.get("error") or ""
            tone = {"applied": "success", "dry_run": "success", "needs_human": "warning", "failed": "error"}.get(status, "info")
            label = {"applied": "Application confirmed", "dry_run": "Practice run finished",
                     "needs_human": "Application needs your review", "failed": "Application issue"}.get(status, f"Application {status}")
            self._status(label + (f" · {detail}" if detail else ""), 9000, tone)
        self.refresh(wait=False)

    def _task_failed(self, kind: str, error: str) -> None:
        if kind == "cycle":
            self._pending_scan = False
        self._status(f"{kind.title()} failed: {error}", 15_000, "error")
        db.add_event(f"Desktop {kind} failed: {error}", "error")
        self.refresh(wait=False)

    def _job_data(self, settings: dict, wait: bool = True) -> dict:
        """The job library's page, counts, and stats; reused while no job changed.

        These queries read the whole library, so repeating them on every
        15-second refresh made the window stutter. Posting-age counts are
        recomputed at least every five minutes.
        """
        jobs_view = self.window.jobs
        job_query = jobs_view.query
        job_status = jobs_view.selected_status
        posted_hours = jobs_view.posted_hours or None
        page_size = jobs_view.page_size
        key = (db.jobs_version(), int(time.time() // 300), job_status, job_query, posted_hours,
               jobs_view.exclude_starred, jobs_view.sort_key, jobs_view.sort_descending, page_size, jobs_view.page,
               tuple(settings.get("priority_companies") or ()))
        cached = getattr(self, "_job_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        if not wait and cached is not None and cached[0][2:] == key[2:] and not self._stopping.is_set():
            # Only the library changed (a scan, a send, a status): keep showing the current
            # page and reload it in the background. View changes still load at once below.
            if self._job_loading != key:
                self._job_loading = key
                try:
                    self._job_loader.submit(lambda: self.jobs_loaded.emit(key, self._load_job_data(key)))
                except RuntimeError:   # shutting down
                    pass
            return cached[1]
        data = self._load_job_data(key)
        self._job_cache = (key, data)
        return data

    @staticmethod
    def _load_job_data(key: tuple) -> dict:
        _version, _bucket, job_status, job_query, posted_hours, exclude_starred, sort_key, descending, \
            page_size, page, _priority = key
        total_jobs = db.count_jobs(status=job_status, q=job_query, posted_within_hours=posted_hours,
                                   exclude_starred=exclude_starred)
        page = min(page, max(0, (total_jobs - 1) // page_size))
        return {
            "page": page,
            "total_jobs": total_jobs,
            "jobs": db.list_jobs(status=job_status, q=job_query, limit=page_size, offset=page * page_size,
                                 posted_within_hours=posted_hours, order=sort_key,
                                 descending=descending, exclude_starred=exclude_starred),
            "top_jobs": db.list_jobs(status="new", limit=5),
            "stats": {**db.job_stats(), **db.fresh_counts(),
                      **{"unstarred_" + name: value for name, value in db.fresh_counts(exclude_starred=True).items()
                         if name.startswith("preparable_")}},
        }

    def _jobs_ready(self, key: tuple, data: dict) -> None:
        if self._job_loading == key:
            self._job_loading = None
        cached = getattr(self, "_job_cache", None)
        # A view change since the load started already read its own data.
        if cached is not None and cached[0][2:] == key[2:] and not self._stopping.is_set():
            self._job_cache = (key, data)
            self.refresh(wait=False)

    def _changed(self, name: str, key: tuple, load):
        """``load()`` when ``key`` differs from the last refresh's, else None (the view keeps its data).

        Applications, drafts, and the Tracker parse every row's JSON; reading them
        again on every 15-second refresh while nothing changed kept the window busy.
        """
        cache = self.__dict__.setdefault("_refresh_keys", {})
        if cache.get(name) == key:
            return None
        value = load()
        cache[name] = key
        return value

    def refresh(self, *, include_account: bool = False, wait: bool = True) -> None:
        """Feed the window fresh data. ``wait=False`` (timer and background completions) lets
        the job library reload off the Qt thread while the current page stays on screen."""
        try:
            settings = db.get_settings()
            job_data = self._job_data(settings, wait)
            self.window.jobs.page = job_data["page"]
            self.window.apps.set_sending(self._sending)
            jobs_version = db.jobs_version()
            apps_version, drafts_version = db.table_version("applications"), db.table_version("drafts")
            if include_account:
                self.__dict__["_refresh_keys"] = {}
            applications = self._changed("applications", (apps_version, jobs_version),
                                         lambda: db.list_applications(limit=300))
            drafts = self._changed("drafts", (drafts_version, jobs_version), db.list_drafts)
            if drafts is not None and applications is None:
                applications = db.list_applications(limit=300)   # the Applications list shows both together
            self.window.set_data(
                profile=db.get_profile() if include_account else None,
                settings=settings if include_account or self._settings_changed(settings) else None,
                jobs=job_data["jobs"],
                total_jobs=job_data["total_jobs"],
                top_jobs=job_data["top_jobs"],
                applications=applications,
                stats=dict(job_data["stats"]),
                events=db.list_events(limit=60),
                drafts=drafts,
                tracker=self._changed("tracker", (apps_version, jobs_version), db.list_tracker),
                inbox=self._changed("inbox", (db.table_version("mail_events"),), lambda: db.unmatched_stage_mail(
                    int((settings.get("email_tracking") or {}).get("lookback_days", 30)))),
                resume_files=db.list_resume_files(),
                mail=self._mail_state,
            )
            if include_account:
                self.window.settings.set_board_count(sum(db.board_counts().values()))
            self._update_scan_state(settings)
        except Exception as exc:  # noqa: BLE001
            self._status(f"Could not refresh local data: {exc}", tone="error")

    def open_setup(self, pending_questions: list[str] | None = None) -> None:
        if getattr(self, "_wizard", None) is not None and self._wizard.isVisible():
            self._wizard.raise_()
            return
        profile = db.get_profile()
        self._wizard = SetupWizard(profile.model_dump() if profile else {}, db.get_settings(),
                                   self.window, pending_questions=pending_questions)
        self._wizard.completed.connect(self.complete_setup)
        self._wire_connections(self._wizard.luna, self._wizard.email)
        self._wizard.exec()
        self._wizard = None

    def complete_setup(self, payload: dict) -> None:
        try:
            profile_patch = dict(payload.get("profile") or {})
            profile_patch.pop("resume_path", None)
            resume_path = str(payload.get("resume_path") or "").strip()
            current = db.get_profile()
            if current and Path(resume_path) == Path(current.resume_path):
                resume_path = ""
            settings_patch = dict(payload.get("settings") or {})
            self._store_mail_secret(settings_patch)
            profile, settings, missing = save_setup(
                profile_patch=profile_patch,
                settings_patch=settings_patch,
                resume_pdf=resume_path or None,
            )
            self.refresh(include_account=True)
            if missing:
                QMessageBox.information(self._wizard, "More setup needed",
                                        "Saved your details. Add " + ", ".join(missing) + " before unattended scanning and applying.")
                self._status("Setup saved · more details needed", tone="warning")
                return
            requeued = db.requeue_preflight_needs_human()
            if requeued:
                self.refresh()
            if self._wizard is not None:
                self._wizard.accept()
            self._status("Setup saved. Starployment is ready" + (f" · {requeued} jobs requeued" if requeued else ""), tone="success")
            if self.scheduler_enabled:
                self._next_cycle_at = time.time() + 2
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self._wizard, "Could not save setup", str(exc))

    def save_profile(self, payload: dict) -> None:
        """Save Profile edits: details, the resume builder library, and saved answers."""
        try:
            payload = dict(payload)
            if "profile" not in payload:   # older flat payload: profile fields plus skills
                skills = payload.pop("skills", None)
                payload = {"profile": payload, "library": {"skills": skills} if skills is not None else {}}
            profile_patch = dict(payload.get("profile") or {})
            settings_patch = {"answers": payload["answers"]} if payload.get("answers") is not None else None
            profile, _, missing = save_setup(profile_patch=profile_patch, settings_patch=settings_patch)
            library = payload.get("library") or {}
            if library:
                before = list((db.get_profile() or type("P", (), {"skills": []})()).skills)
                save_resume_library(library)
                if "skills" in library and list(library.get("skills") or []) != before:
                    self._rescore_in_background()
            if not missing:
                db.requeue_preflight_needs_human()
            refilled = prepare.reanswer_open_drafts()
            self.refresh(include_account=True)
            if missing:
                self._status("Profile saved · setup still needs " + ", ".join(missing), tone="warning")
            elif refilled:
                self._status(f"Profile saved · updated answers in {refilled} prepared application"
                             f"{'s' if refilled != 1 else ''}", tone="success")
            else:
                self._status("Profile saved", tone="success")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self.window, "Could not save profile", str(exc))

    def _store_mail_secret(self, patch: dict) -> None:
        """Move a transient mailbox password into the OS vault; it never reaches SQLite."""
        imap_password = str(patch.pop("_imap_password", ""))
        forget_imap_password = bool(patch.pop("_forget_imap_password", False))
        if imap_password and forget_imap_password:
            raise ValueError("Choose either a new mailbox password or remove the saved one")
        needs_vault = (imap_password or forget_imap_password
                       or patch.get("email_verification", {}).get("enabled")
                       or patch.get("email_tracking", {}).get("enabled"))
        if not needs_vault:
            return
        from ..config import merge_settings, validate_settings
        from ..apply.email_verification import delete_password, has_password, store_password

        candidate = validate_settings(merge_settings(db.get_settings(), patch))
        verification = candidate["email_verification"]
        username = verification["imap_username"]
        if imap_password and not username:
            raise ValueError("Enter the mailbox address before saving its app password")
        wants_mailbox = verification["enabled"] or candidate["email_tracking"]["enabled"]
        if wants_mailbox and (forget_imap_password or (not imap_password and not has_password(username))):
            raise ValueError("Add your mailbox app password before turning on email features")
        if imap_password:
            store_password(username, imap_password)
        elif forget_imap_password:
            previous = db.get_settings().get("email_verification", {}).get("imap_username", "")
            if previous or username:
                delete_password(previous or username)

    def save_settings(self, patch: dict) -> None:
        try:
            patch = dict(patch)
            previous = db.get_settings()
            protect = patch.get("keychain_protection", previous.get("keychain_protection", False))
            if protect and not previous.get("keychain_protection", False):
                # Hand saved secrets to Keychain before reads start looking there.
                from ..apply.email_verification import _FileVault
                _FileVault().move_to_system_vault()
                db.set_settings({"keychain_protection": True})
            self._store_mail_secret(patch)
            settings = db.set_settings(patch)
            if ("appearance" in patch and settings["appearance"] != previous.get("appearance")) or \
                    ("font" in patch and settings.get("font") != previous.get("font")):
                from .styles import apply_appearance, apply_typeface

                def restyle() -> None:
                    apply_typeface(QApplication.instance(), settings.get("font", "Claude"), restyle=False)
                    apply_appearance(QApplication.instance(), settings["appearance"])

                self.window.crossfade(restyle)
            # Settings sends every value on save; re-rank only when a ranking input changed.
            if any(previous.get(key) != settings.get(key) for key in SCORING_KEYS & patch.keys()):
                self._rescore_in_background()
            if "answers" in patch:
                prepare.reanswer_open_drafts()
            self.refresh(include_account=True)
            self._status("Settings saved", tone="success")
            if (settings.get("setup_complete") and self.scheduler_enabled
                    and (not self._next_cycle_at or settings.get("jarvis_mode") != previous.get("jarvis_mode"))):
                self._next_cycle_at = time.time() + 2
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self.window, "Could not save settings", str(exc))

    def set_jarvis_mode(self, mode: str) -> None:
        """The sidebar's JARVIS menu: save only the mode, leaving other unsaved Settings edits alone."""
        self.save_settings({"jarvis_mode": mode})
        label = {"off": "JARVIS is off", "new": "JARVIS NEW is on: recent postings with saved answers",
                 "all": "JARVIS ALL is on: new and older open postings, sent without review",
                 "all_plus": "JARVIS ALL+ is on: a wider net at full speed, sent without review"}.get(mode, "")
        if label:
            self._status(label, tone="success" if mode != "off" else "info")

    def _rescore_in_background(self) -> None:
        """Re-rank every saved job off the UI thread (thousands of jobs take seconds)."""
        self._status("Updating job rankings…", 0, "busy")
        self._submit_assist("rescore", scheduler.rescore, None)

    def open_url(self, url: str) -> None:
        # message:// opens one email in Apple Mail by its Message-ID.
        if url.startswith(("https://", "http://", "message://")):
            QDesktopServices.openUrl(QUrl(url))

    def open_resume(self, path: str) -> None:
        resume = Path(path).expanduser()
        if resume.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(resume)))
            return
        self._status("The resume file recorded for this attempt is no longer available", tone="warning")

    def save_application_note(self, app_id: int, note: str) -> None:
        try:
            db.set_application_note(app_id, note)
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self.window, "Could not save note", str(exc))

    def stop(self) -> None:
        self._stopping.set()
        self._keep_awake(False)
        self._send_queue.clear()
        self._prep_stop.set()
        scheduler.STATE.stop = True
        self._tasks.put(None)
        self._assist.shutdown(wait=False, cancel_futures=True)
        self._quick.shutdown(wait=False, cancel_futures=True)
        self._job_loader.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ assist worker
    _last_settings_signature: object = None

    def _settings_changed(self, settings: dict) -> bool:
        """Background work (Luna remembering answers) can change settings; refresh views when it does."""
        import json
        signature = json.dumps(settings, sort_keys=True, default=str)
        changed = signature != self._last_settings_signature
        self._last_settings_signature = signature
        return changed and not (self.window.settings.dirty or self.window.profile.dirty)

    def _submit_assist(self, kind: str, function, context: object = None, *args) -> bool:
        if self._stopping.is_set():
            return False   # the window is closing; late UI signals must not start new work

        def run():
            try:
                result = function(*args)
            except Exception as exc:  # noqa: BLE001
                self.signals.assist_failed.emit(kind, context, str(exc))
            else:
                self.signals.assist_finished.emit(kind, context, result)
        try:
            (self._quick if kind in QUICK_TASKS else self._assist).submit(run)
        except RuntimeError:   # shut down between the check and the submit
            return False
        return True

    # ------------------------------------------------------------ Workday sign-ins
    def show_saved_sign_ins(self) -> None:
        """The vault's Workday accounts; a password only after Touch ID (vault.authenticate)."""
        from .. import vault
        from .views.passwords import PasswordsSheet
        profile = db.get_profile()
        sheet = PasswordsSheet(self.window, default_email=(profile.email if profile else "") or "")
        sheet.set_accounts(vault.list_accounts())
        sheet.unlocked_until = 0.0
        sheet.reveal_requested.connect(lambda tenant: self._unlock_sign_in(sheet, tenant, False))
        sheet.copy_requested.connect(lambda tenant: self._unlock_sign_in(sheet, tenant, True))
        sheet.forget_requested.connect(lambda tenant: self._forget_sign_in(sheet, tenant))
        sheet.save_requested.connect(lambda details: self._save_sign_in(sheet, details))
        sheet.open_requested.connect(self._open_sign_in_site)
        sheet.export_requested.connect(lambda: self._unlock_sign_in(sheet, "", "export"))
        self._sign_ins_sheet = sheet
        sheet.exec()

    def _save_sign_in(self, sheet, details: dict) -> None:
        """A sign-in the candidate typed: stored in the vault, never in SQLite or logs."""
        from .. import vault
        try:
            record = vault.add_own(details["address"], details["email"], details["password"], details.get("company", ""))
        except Exception as exc:  # noqa: BLE001 - a bad address or a locked vault
            sheet.set_message(str(exc)[:200])
            return
        db.release_workday_holds(record.get("host", ""))   # JARVIS may apply there again
        sheet.saved()
        sheet.set_accounts(vault.list_accounts())
        sheet.set_message(f"Saved. Starployment signs in to {record['company']}'s Workday with it.")

    def _open_sign_in_site(self, tenant: str) -> None:
        from .. import vault
        url = vault.site_url(vault.get_account(tenant) or {})
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _export_sign_ins(self, sheet) -> None:
        """Hand every sign-in to the macOS Passwords app through its CSV import, then delete the file."""
        from .. import vault
        try:
            path, count = vault.export_for_passwords()
        except Exception as exc:  # noqa: BLE001
            sheet.set_message(str(exc)[:200])
            return
        if sys.platform == "darwin":
            subprocess.run(["/usr/bin/open", "-a", "Passwords"], check=False)
            subprocess.run(["/usr/bin/open", "-R", str(path)], check=False)
        QTimer.singleShot(vault.EXPORT_SECONDS * 1000, vault.clear_export)
        sheet.set_message(f"{count} sign-in{'s' if count != 1 else ''} ready for Passwords. In Passwords choose "
                          "File → Import Passwords… and pick the file Finder is showing. Starployment deletes "
                          f"that file in {vault.EXPORT_SECONDS // 60} minutes.")

    def _unlock_sign_in(self, sheet, tenant: str, copy: bool) -> None:
        from .. import vault
        if time.monotonic() < getattr(sheet, "unlocked_until", 0.0):
            self._vault_unlocked(sheet, tenant, copy)
            return
        sheet.set_message("Waiting for Touch ID…")
        self._submit_assist("vault_auth", vault.authenticate, (sheet, tenant, copy),
                            "export your Workday sign-ins to Passwords" if copy == "export"
                            else "show a saved Workday password")

    def _vault_unlocked(self, sheet, tenant: str, copy) -> None:
        from .. import vault
        if copy == "export":
            self._export_sign_ins(sheet)
            return
        password = vault.password_for(tenant)
        if not password:
            sheet.set_message("That password could not be read from the vault.")
            return
        if copy:
            clipboard = QApplication.clipboard()
            clipboard.setText(password)
            # Clear it after a minute unless something else was copied since.
            QTimer.singleShot(60_000, lambda: clipboard.text() == password and clipboard.clear())
            sheet.set_message("Copied. The clipboard clears in a minute.")
        else:
            sheet.show_password(tenant, password)
            sheet.set_message("")

    def _forget_sign_in(self, sheet, tenant: str) -> None:
        from .. import vault
        vault.delete_account(tenant)
        sheet.set_accounts(vault.list_accounts())
        sheet.set_message("Forgotten here. The account on the employer's Workday still exists.")

    def _assist_finished(self, kind: str, context: object, result: object) -> None:
        if kind == "vault_auth":
            sheet, tenant, copy = context
            ok, message = result
            if not ok:
                sheet.set_message(message or "Touch ID did not confirm it was you.")
                return
            sheet.unlocked_until = time.monotonic() + 120
            self._vault_unlocked(sheet, tenant, copy)
            return
        if kind == "plan":
            self._preparing = False
            report = result if isinstance(result, dict) else {}
            prepared, failed = int(report.get("prepared", 0)), int(report.get("failed", 0))
            drafted = int(report.get("drafted", 0))
            text = f"Prepared {prepared} application{'s' if prepared != 1 else ''} for your review"
            if drafted:
                text += f" · Luna drafted {drafted} answers"
            if failed:
                text += f" · {failed} could not be prepared"
            fill_failed = int(report.get("fill_failed", 0))
            if fill_failed:
                text += (f" · AI Autofill could not run on {fill_failed}; the reason is on each one. "
                         "Try AI Autofill again from Applications")
            starred = int(report.get("starred", 0))
            if starred:
                text += f" · {starred} starred for you to send one by one"
            if not report.get("considered"):
                text = "No new matches fit this plan. Widen the posting window, lower the minimum match, or run a scan."
            self._status(text, 15_000 if fill_failed else 9000,
                         "success" if prepared and not fill_failed else "warning")
            if prepared:
                self.window.show_applications("review")
        elif kind == "prepare_one":
            self._preparing_jobs.discard(str(context))
            self.window.jobs.set_preparing(self._preparing_jobs)
            if result:
                self._status(f"Application prepared, but AI Autofill could not run: {result} "
                             "Try AI Autofill again from the application.", 15_000, "warning")
            else:
                self._status("Application prepared · review it in Applications", tone="success")
            self.window.show_draft_for_job(str(context))
            self.refresh(include_account=True)
            return
        elif kind == "luna":
            count = int(result or 0)
            self._status(f"AI Autofill answered {count} question{'s' if count != 1 else ''}. "
                         "Check the labeled assumptions before you send." if count
                         else "AI Autofill had nothing to fill.", 9000, "success" if count else "info")
        elif kind == "autofill_all":
            report = result if isinstance(result, dict) else {}
            self._status(f"AI Autofill answered {report.get('answers', 0)} questions across "
                         f"{report.get('applications', 0)} applications"
                         + (f" · {report['failed']} failed" if report.get("failed") else "")
                         + ". Check labeled assumptions before sending.", 10_000,
                         "warning" if report.get("failed") else "success")
        elif kind == "agent_status":
            panel, backend = context
            status = result
            panel.set_status(backend, status.ready, status.title, status.detail, status.action)
        elif kind == "agent_test":
            ok, message = result
            context.set_test_result(ok, message)
        elif kind == "mail_test":
            ok, message = result
            context.set_test_result(ok, message)
        elif kind == "sheet_test":
            ok, message = result
            if context is not None and hasattr(context, "set_test_result"):
                context.set_test_result(ok, message)
            return
        elif kind == "mail_sync":
            report = result
            from datetime import datetime, timezone
            self._mail_state = {"last_checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "unmatched": len(report.unmatched)}
            if report.updates:
                stages = [u["stage"] for u in report.updates]
                tone = "success" if any(s in ("interview", "assessment", "offer") for s in stages) else "info"
                self._status(report.summary, 12_000, tone)
            elif context:
                self._status(report.summary, tone="info")
            self.refresh(include_account=True)
            self._maybe_sync_sheet()
            return
        elif kind == "workday_portal":
            report = result
            if report.updates or report.confirmed:
                tone = "success" if report.confirmed or any(u["stage"] in ("interview", "assessment", "offer")
                                                             for u in report.updates) else "info"
                self._status(report.summary, 12_000, tone)
            elif context:
                self._status(report.summary, tone="info")
            self.refresh(include_account=True)
            self._maybe_sync_sheet()
            return
        elif kind == "sheet_sync":
            report = result
            manual = bool(context)
            sent = int(getattr(report, "sent", 0) or 0)
            summary = str(getattr(report, "summary", "") or "Google Sheet sync finished")
            if manual or sent:
                db.add_event(summary)
                self._status(summary, 12_000, "success" if sent else "info")
            panel = getattr(self.window.settings, "sheets", None)
            setter = getattr(panel, "set_sync_state", None)
            if setter is not None:
                from ..models import now_iso
                setter(now_iso(), int(getattr(report, "total", 0) or 0))
            if manual or sent:
                self.refresh(include_account=True)
            self._maybe_sync_sheet()
            return
        elif kind == "rescore":
            self._status("Job rankings updated", tone="success")
        elif kind == "preview":
            self.open_resume(str(result))
        elif kind == "import_resume":
            _, added = result
            self._status("Imported " + ", ".join(f"{v} {k}" for k, v in added.items() if v) if any(added.values())
                         else "Nothing new found in that resume", tone="success")
            self.refresh(include_account=True)
            return
        self.refresh()

    def _assist_failed(self, kind: str, context: object, error: str) -> None:
        if kind == "vault_auth":
            context[0].set_message(f"Touch ID is unavailable: {error[:120]}")
            return
        if kind == "plan":
            self._preparing = False
        if kind == "prepare_one":
            self._preparing_jobs.discard(str(context))
            self.window.jobs.set_preparing(self._preparing_jobs)
            label = "Preparing the application"
            self._status(f"{label} failed: {error}", 15_000, "error")
            db.add_event(f"Desktop {kind} failed: {error}", "error")
            self.refresh(include_account=True)
            return
        if kind in ("agent_test", "mail_test", "sheet_test") and context is not None and hasattr(context, "set_test_result"):
            context.set_test_result(False, error)
            return
        if kind == "agent_status" and context is not None:
            panel, backend = context
            panel.set_status(backend, False, "Could not check the connection", error[:200], "test")
            return
        if kind == "mail_sync" and error.startswith(email_tracker.OFFLINE):
            # Offline, asleep, or a VPN change: not a fault. Try once more shortly instead of next cycle.
            retry = not self._mail_retry_pending
            self._mail_retry_pending = True
            if retry:
                QTimer.singleShot(MAIL_RETRY_MS, self._retry_mail_sync)
            self._status(f"{error}. Your inbox will be checked again in a few minutes.", 10_000, "warning")
            db.add_event(f"Inbox check postponed: {error}", "warn")
            self.refresh(wait=False)
            self._maybe_sync_sheet()
            return
        if kind == "sheet_sync":
            # Unattended cycles retry next time. A warning in the log, a toast only when asked.
            db.add_event(f"Google Sheet sync failed: {error[:300]}", "warn")
            if context:
                self._status(f"Google Sheet sync failed: {error[:300]}", 12_000, "warning")
            self.refresh(wait=False)
            self._maybe_sync_sheet()
            return
        label = {"plan": "Preparing applications", "prepare_one": "Preparing the application", "luna": "AI Autofill",
                 "mail_sync": "Inbox check", "workday_portal": "Workday check", "preview": "Resume preview", "import_resume": "Resume import"}.get(kind, kind)
        self._status(f"{label} failed: {error}", 15_000, "error")
        db.add_event(f"Desktop {kind} failed: {error}", "error")
        self.refresh(wait=False)
        if kind in ("mail_sync", "workday_portal"):
            self._maybe_sync_sheet()

    def _prep_progress(self, text: str, done: int, total: int) -> None:
        self._status(f"{text} · {done} of {total}", 0, "busy")

    # ------------------------------------------------------------------ apply flow
    def start_plan(self, plan: dict) -> None:
        """Prepare the top matches for review from Overview, Jobs or the menu."""
        settings = db.get_settings()
        if not settings.get("setup_complete"):
            self._status("Complete setup before preparing applications")
            self.open_setup()
            return
        if self._preparing:
            self._status("Applications are already being prepared")
            return
        hours = int(plan.get("posted_within_hours", 0) or 0)
        order = str(plan.get("order") or "match")
        exclude_starred = bool(plan.get("exclude_starred", (settings.get("apply_plan") or {}).get("exclude_starred", True)))
        try:
            db.set_settings({"apply_plan": {"count": int(plan["count"]), "min_score": float(plan["min_score"]),
                                            "one_per_company": bool(plan["one_per_company"]),
                                            "exclude_starred": exclude_starred,
                                            "posted_within_hours": hours, "order": order}})
        except Exception as exc:  # noqa: BLE001
            self._status(f"Could not save the plan: {exc}", tone="error")
            return
        self._preparing = True
        self._prep_stop.clear()
        window = {24: " posted in the last 24 hours", 72: " posted in the last 3 days",
                  168: " posted in the last week"}.get(hours, "")
        self._status(f"Preparing up to {plan['count']} applications{window}…", 0, "busy")

        def progress(text: str, done: int, total: int) -> None:
            self.signals.progress.emit(text, done, total)

        stop = self._prep_stop

        def run() -> dict:
            return prepare.prepare_batch(int(plan["count"]), float(plan["min_score"]), bool(plan["one_per_company"]),
                                         use_luna=bool(plan.get("use_luna", True)), progress=progress, stop=stop,
                                         posted_within_hours=hours, order=order, exclude_starred=exclude_starred)

        self._submit_assist("plan", run, None)

    def prepare_job(self, job_id: str) -> None:
        """Prepare one job for review (Jobs → Prepare)."""
        if not db.get_settings().get("setup_complete"):
            self._status("Complete setup before preparing applications")
            self.open_setup()
            return
        job, profile = db.get_job(job_id), db.get_profile()
        if job is None or profile is None:
            self._status("That job or your profile is missing", tone="warning")
            return
        if job.ats not in prepare.PREPARABLE_ATS:
            self._status("This posting uses a form Starployment cannot fill. Open the posting to apply yourself.",
                         tone="warning")
            return
        self._preparing_jobs.add(job_id)
        self.window.jobs.set_preparing(self._preparing_jobs)
        try:
            db.upsert_draft(job_id, status="preparing")
        except Exception:
            pass
        self.refresh()
        self._status(f"Preparing {job.company} · {job.title}…", 0, "busy")

        def run() -> str:
            """Prepare, then fill. Returns why the fill did not run, or ""."""
            settings = db.get_settings()
            draft_id = prepare.prepare_job(job, profile, settings)
            if prepare.luna_ready(settings):
                try:
                    fill = (prepare.autofill if (settings.get("luna") or {}).get("autofill_on_prepare", True)
                            else prepare.luna_fill)
                    fill(draft_id, settings)
                except Exception as error:  # noqa: BLE001
                    db.update_draft(draft_id, error=f"Luna could not draft answers: {str(error)[:200]}")
                    return str(error)[:200]
            return ""

        self._submit_assist("prepare_one", run, job_id)

    def save_draft(self, draft_id: int, edits: dict) -> None:
        if not edits:
            return
        try:
            draft = db.get_draft(draft_id)
            if draft is None or draft["status"] not in db.DRAFT_OPEN + ("practice", "error"):
                return
            prepare.apply_edits(draft["questions"], edits)
            status = draft["status"] if draft["status"] in ("approved", "practice", "error") else prepare.draft_status(draft["questions"])
            db.update_draft(draft_id, questions=draft["questions"], status=status)
            self._announce_shared(prepare.share_answers(draft_id), saved=True)
            self.refresh(include_account=True)
        except Exception as exc:  # noqa: BLE001
            self._status(f"Could not save answers: {exc}", tone="error")

    def _announce_shared(self, filled: int, saved: bool = False) -> None:
        if filled:
            self._status(f"Answers saved · also filled {filled} matching question{'s' if filled != 1 else ''} "
                         "in your other prepared applications", 7000, "success")
        elif saved:
            self._status("Answers saved", tone="success")

    def approve_and_send(self, draft_id: int, edits: dict) -> None:
        try:
            prepare.approve_and_queue(draft_id, edits)
        except ValueError as exc:
            prepare.share_answers(draft_id)   # what you did answer still helps the other drafts
            self._status(str(exc), 12_000, "warning")
            self.refresh(include_account=True)
            return
        prepare.share_answers(draft_id)
        self._queue_send([draft_id])

    def send_approved(self) -> None:
        settings = db.get_settings()
        candidates, held = prepare.bulk_send_candidates(
            [d for d in db.list_drafts(("approved", "ready")) if d["id"] not in self._sending],
            settings.get("priority_companies") or [], settings)
        if not candidates:
            self._status("Nothing is ready to send. Use Autofill all or answer the open questions first."
                         + (f" {len(held)} application{'s' if len(held) != 1 else ''} (starred, or at an employer "
                            "you are interviewing with) wait for you to approve each one." if held else ""))
            return
        practice = settings.get("dry_run")
        autofilled = sum(1 for d in candidates if any(q.get("source") == "autofill" for q in d["questions"]))
        answer = QMessageBox.question(
            self.window, "Send ready applications",
            f"Approve and send {len(candidates)} application{'s' if len(candidates) != 1 else ''} now?"
            + (f"\n\n{autofilled} include AI Autofill answers, some of them labeled assumptions. "
               "Review any you are unsure about first." if autofilled else "")
            + (f"\n\n{len(held)} starred application{'s are' if len(held) != 1 else ' is'} left for you to "
               "refine and send one by one." if held else "")
            + ("\n\nPractice mode is on, so forms are filled but not submitted." if practice else
               "\n\nEach one is submitted to the employer and confirmed one at a time."))
        if answer != QMessageBox.StandardButton.Yes:
            return
        queued = []
        for draft in candidates:
            try:
                if draft["status"] == "ready":
                    prepare.approve_and_queue(draft["id"], {})
                queued.append(draft["id"])
            except ValueError:
                continue
        self._queue_send(queued)

    def autofill_all(self) -> None:
        settings = db.get_settings()
        if (settings.get("agent") or {}).get("backend", "none") in ("", "none"):
            self._status("Connect Luna in Settings → Connections to use AI Autofill", tone="warning")
            self.window.show_connections()
            return

        def progress(text: str, done: int, total: int) -> None:
            self.signals.progress.emit(text, done, total)

        self._status("AI Autofill is filling your prepared applications…", 0, "busy")
        self._submit_assist("autofill_all", lambda: prepare.autofill_all(progress), None)

    def reopen_rejected(self, app_id: int) -> None:
        try:
            job_id = prepare.reopen_rejected(app_id)
        except ValueError as exc:
            self._status(str(exc), tone="warning")
            return
        draft = db.draft_for_job(job_id)
        if draft and draft["questions"] and draft["status"] in db.DRAFT_OPEN:
            # The refusal named fields: the prepared answers are kept and those fields are marked.
            self._status("Not received, so it is ready to fix and send again · the fields the employer named are "
                         "marked", 9000, "success")
            self.refresh()
            self.window.apps.select_draft_for_job(job_id)
            return
        self.prepare_job(job_id)

    def finish_draft_in_browser(self, draft_id: int, edits: dict) -> None:
        """Open a prepared application in a visible window; the candidate finishes it and presses Submit."""
        answer = QMessageBox.question(
            self.window, "Finish in browser",
            "Starployment will open this application in a browser window and fill in everything it can.\n\n"
            "Finish anything left (a CAPTCHA, an email code, a field it couldn't fill), then press Submit "
            "yourself. Keep the window open until the employer's confirmation appears, and Starployment "
            "records it. This is a real submission, even in practice mode.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            prepare.prepare_for_browser(draft_id, edits)
        except ValueError as exc:
            self._status(str(exc), tone="warning")
            self.refresh()
            return
        prepare.share_answers(draft_id)
        self._in_browser.add(draft_id)
        self._queue_send([draft_id])

    def finish_attempt_in_browser(self, app_id: int) -> None:
        """Reopen an unconfirmed submission (a CAPTCHA, an email code) in a visible window for the candidate."""
        answer = QMessageBox.question(
            self.window, "Finish in browser",
            "Starployment already pressed Submit on this application once, and it may have been received. "
            "Check your email for a confirmation first; if there is one, choose Mark as sent instead.\n\n"
            "Otherwise Starployment opens the form in a browser window, filled in. Solve the check or enter "
            "the code, then press Submit yourself, and keep the window open until the confirmation appears.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            draft_id = prepare.draft_for_attempt(app_id)
        except ValueError as exc:
            self._status(str(exc), tone="warning")
            return
        self._in_browser.add(draft_id)
        self._queue_send([draft_id])

    def _queue_send(self, draft_ids: list[int]) -> None:
        settings = db.get_settings()
        if not settings.get("setup_complete"):
            self._status("Complete setup before sending")
            return
        if not self._sending:
            self._send_counts = {"sent": 0, "other": 0}
        for draft_id in draft_ids:
            if draft_id in self._sending:
                continue
            self._sending.add(draft_id)
            self._send_queue.append(draft_id)
        count, parallel = len(self._sending), self._parallel_sends(settings)
        if draft_ids and all(d in self._in_browser for d in draft_ids) and count == len(draft_ids):
            self._status("Opening a browser window · finish anything left and press Submit there. "
                         "Starployment records the employer's confirmation.", 0, "busy")
        else:
            self._status(("Practice run" if settings.get("dry_run") else "Sending")
                         + (f" · {count} applications, {min(count, parallel)} at a time in the background"
                            if count > 1 else " in the background") + "…", 0, "busy")
        self._pump_sends()
        self.refresh()

    @staticmethod
    def _parallel_sends(settings: dict) -> int:
        return jarvis.parallel(settings, "parallel_sends", 1, scheduler.MAX_PARALLEL_SENDS)

    def _pump_sends(self) -> None:
        """Start queued sends while browser slots are free, never two to one employer at once."""
        if self._stopping.is_set():
            return
        limit = self._parallel_sends(db.get_settings())
        busy = set(self._send_active.values())
        for draft_id in list(self._send_queue):
            if len(self._send_active) >= limit:
                break
            if self._send_active and governor.busy():
                # The computer is busy: keep the sends already running and look again shortly.
                if not self._pump_waiting:
                    self._pump_waiting = True
                    QTimer.singleShot(5000, self._repump)
                break
            draft = db.get_draft(draft_id)
            company = str((draft or {}).get("company") or draft_id).strip().casefold()
            if company in busy:
                continue
            self._send_queue.remove(draft_id)
            self._send_active[draft_id] = company
            busy.add(company)
            threading.Thread(target=self._send_worker, args=(draft_id, limit), daemon=True,
                             name=f"starployment-send-{draft_id}").start()

    _pump_waiting = False

    def _repump(self) -> None:
        self._pump_waiting = False
        self._pump_sends()

    def _send_worker(self, draft_id: int, parallel: int) -> None:
        try:
            result = prepare.send_draft(draft_id, parallel=parallel, assist=draft_id in self._in_browser)
        except Exception as exc:  # noqa: BLE001
            self.signals.send_done.emit(draft_id, None, str(exc))
        else:
            self.signals.send_done.emit(draft_id, result, "")

    def _send_done(self, draft_id: int, result: object, error: str) -> None:
        self._send_active.pop(draft_id, None)
        result = result if isinstance(result, dict) else {}
        status = str(result.get("status", "")) if not error else "failed"
        if status == "busy" and not self._stopping.is_set():
            # Every browser slot was taken (an automatic run, say): try again shortly.
            self._send_queue.appendleft(draft_id)
            QTimer.singleShot(5000, self._pump_sends)
            return
        self._sending.discard(draft_id)
        self._in_browser.discard(draft_id)
        self._send_counts["sent" if status in ("applied", "dry_run") else "other"] += 1
        draft = db.get_draft(draft_id) or {}
        subject = f"{draft.get('company') or 'Application'}"
        detail = error or result.get("message") or result.get("error") or ""
        label = {"applied": "Sent and confirmed", "dry_run": "Practice run finished, nothing submitted",
                 "needs_human": "Needs your review", "failed": "Application issue",
                 "blocked": "Could not send"}.get(status, "Finished")
        remaining = len(self._sending)
        if remaining:
            self._status(f"{label} · {subject}" + (f" · {detail}" if detail and status != "applied" else "")
                         + f" · {remaining} more sending", 0, "busy")
        else:
            done = self._send_counts
            summary = (f"{label} · {subject}" + (f" · {detail}" if detail and status != "applied" else "")
                       if done["sent"] + done["other"] == 1 else
                       f"Finished sending · {done['sent']} sent, {done['other']} need your review")
            tone = "success" if done["other"] == 0 else "warning"
            self._status(summary, 9000, tone)
        self._pump_sends()
        self.refresh(wait=False)

    def luna_draft(self, draft_id: int) -> None:
        settings = db.get_settings()
        if (settings.get("agent") or {}).get("backend", "none") in ("", "none"):
            self._status("Connect Luna in Settings → Connections to use AI Autofill", tone="warning")
            self.window.show_connections()
            return
        self._status("AI Autofill is filling this application…", 0, "busy")
        self._submit_assist("luna", prepare.autofill, draft_id, draft_id, settings)

    def remove_draft(self, draft_id: int) -> None:
        try:
            db.delete_draft(draft_id)
            self._status("Prepared application removed", tone="success")
        except ValueError as exc:
            self._status(str(exc), tone="warning")
        self.refresh()

    def discard_draft(self, draft_id: int) -> None:
        """Remove a prepared application and skip its job, so later plans do not prepare it again."""
        draft = db.get_draft(draft_id)
        try:
            db.delete_draft(draft_id)
            if draft is not None:
                db.set_job_status(str(draft["job_id"]), "skipped")
            self._status("Prepared application removed and its job skipped", tone="success")
        except ValueError as exc:
            self._status(str(exc), tone="warning")
        self.refresh()

    def set_star(self, job_id: str, starred: bool) -> None:
        try:
            db.set_job_star(job_id, bool(starred))
        except ValueError as exc:
            self._status(str(exc), tone="warning")
            return
        job = db.get_job(job_id)
        name = f"{job.company} · {job.title}" if job else "Application"
        self._status(f"Starred {name}. It waits for you to approve it." if starred else f"Removed the star from {name}",
                     tone="success")
        self.refresh()

    def mark_sent(self, job_id: str) -> None:
        """Record an application the candidate sent themselves; it moves to the Tracker."""
        try:
            db.mark_application_sent(job_id)
        except ValueError as exc:
            self._status(str(exc), tone="warning")
            self.refresh()
            return
        job = db.get_job(job_id)
        db.add_event(f"Marked as sent by you: {job.company} / {job.title}" if job else "Marked as sent by you",
                     "success", job_id)
        self._status(f"Marked {job.company if job else 'it'} as sent · it is in the Tracker now", tone="success")
        self.refresh()

    def hide_application(self, app_id: int) -> None:
        try:
            db.hide_application(app_id)
        except ValueError as exc:
            self._status(str(exc), tone="warning")
            return
        self._status("Removed from Applications. Its history is kept.", tone="success")
        self.refresh()

    def reset_unfinished(self, include_attempts: bool) -> None:
        if self._preparing:
            self._prep_stop.set()
        report = db.reset_unfinished(include_attempts=include_attempts)
        self._status(f"Cleared {report['drafts']} prepared application{'s' if report['drafts'] != 1 else ''}"
                     + (f" and {report['attempts']} unfinished attempt{'s' if report['attempts'] != 1 else ''}"
                        if include_attempts else "") + ". Everything submitted was kept."
                     + (f" Kept {report['kept']} sent without a confirmation, since "
                        f"{'it' if report['kept'] == 1 else 'they'} may have reached the employer. "
                        "Check the employer's site, then right-click it for Mark as sent or Remove from list."
                        if report.get("kept") else ""), tone="success")
        db.add_event(f"Reset unfinished applications: {report['drafts']} drafts, {report['attempts']} attempts")
        self.refresh()

    # ------------------------------------------------------------------ tracker
    def set_stage(self, app_id: int, stage: str) -> None:
        try:
            db.set_application_stage(app_id, stage, "manual")
        except ValueError as exc:
            self._status(str(exc), tone="warning")
        self.refresh()

    def add_manual_application(self, data: dict) -> None:
        try:
            db.add_manual_application(**data)
            self._status(f"Tracking {data.get('company')} · {data.get('title')}", tone="success")
        except Exception as exc:  # noqa: BLE001
            self._status(f"Could not add it: {exc}", tone="error")
        self.refresh()

    def _mail_sync_job(self, manual: bool = False):
        from .. import email_tracker
        try:
            # A check the candidate asked for has Luna read unsettled emails now, not in a later batch.
            return email_tracker.sync(db.get_settings(), luna_now=manual)
        finally:
            self._mail_syncing.clear()

    def _retry_mail_sync(self) -> None:
        self._mail_retry_pending = False
        if db.get_settings().get("email_tracking", {}).get("enabled"):
            self.sync_email()

    def check_workday(self, manual: bool = False) -> None:
        """Read the Workday Candidate Home of employers with open applications (workday_portal)."""
        if not (db.get_settings().get("workday") or {}).get("check_portal", True) or self._portal_checking.is_set():
            return
        self._portal_checking.set()

        def run():
            from .. import workday_portal
            try:
                return workday_portal.check(db.get_settings(), manual=manual, stop=self._stopping.is_set)
            finally:
                self._portal_checking.clear()
        self._submit_assist("workday_portal", run, manual)

    def sync_email(self, manual: bool = False) -> None:
        if manual:
            self.check_workday(manual=True)
        if not db.get_settings().get("email_tracking", {}).get("enabled"):
            self._status("Turn on reply tracking in Settings → Connections → Email", tone="info")
            self.window.show_connections()
            return
        if self._mail_syncing.is_set():
            if manual:   # one inbox check at a time; the running one covers this request
                self._status("Your inbox is already being checked", tone="info")
            return
        if manual:
            self._status("Checking your inbox…", 0, "busy")
        self._mail_syncing.set()
        self._submit_assist("mail_sync", self._mail_sync_job, manual, manual)

    def _maybe_sync_sheet(self) -> None:
        """Start the sheet sync once this cycle's inbox and Workday checks have finished."""
        if not self._sheet_after_checks:
            return
        if self._mail_syncing.is_set() or self._portal_checking.is_set() or self._sheet_syncing.is_set():
            return
        if not (db.get_settings().get("google_sheets") or {}).get("enabled"):
            self._sheet_after_checks = False
            return
        self._sheet_after_checks = False
        if not self.sync_sheet():
            self._sheet_after_checks = self._sheet_syncing.is_set()

    def sync_sheet(self, manual: bool = False, full: bool = False) -> bool:
        """Copy confirmed applications to the Google Sheet. One sync runs at a time."""
        if self._stopping.is_set():
            return False
        if not manual and not (db.get_settings().get("google_sheets") or {}).get("enabled"):
            return False
        if self._sheet_syncing.is_set():
            if manual:
                self._status("A Google Sheet sync is already running", tone="info")
            return False
        if manual:
            self._status("Updating your Google Sheet…", 0, "busy")
        self._sheet_syncing.set()

        def run(full=full):
            from .. import sheets_sync
            try:
                return sheets_sync.sync(db.get_settings(), full=full)
            finally:
                self._sheet_syncing.clear()

        if not self._submit_assist("sheet_sync", run, manual):
            self._sheet_syncing.clear()
            return False
        return True

    def _copy_sheet_script(self) -> None:
        from .. import sheets_sync
        try:
            script = sheets_sync.setup_script()
        except Exception as exc:  # noqa: BLE001 - the vault can be unavailable
            self._status(f"Could not prepare the setup script: {exc}", tone="error")
            return
        clipboard = QApplication.clipboard()
        if clipboard is None:
            self._status("Could not reach the clipboard", tone="error")
            return
        clipboard.setText(script)
        self._status("Setup script copied. Paste it into Extensions → Apps Script.", tone="success")

    def _sheet_test(self, panel, values: dict) -> None:
        from .. import sheets_sync

        def run():
            return sheets_sync.test({**db.get_settings(), **(values or {})})

        self._submit_assist("sheet_test", run, panel)

    def _wire_sheets(self, panel) -> None:
        if panel is None:
            return
        copy = getattr(panel, "copy_script_requested", None)
        test = getattr(panel, "test_requested", None)
        sync = getattr(panel, "sync_requested", None)
        if copy is not None:
            copy.connect(self._copy_sheet_script)
        if test is not None:
            test.connect(lambda values, panel=panel: self._sheet_test(panel, values))
        if sync is not None:
            sync.connect(lambda full: self.sync_sheet(manual=True, full=bool(full)))

    # ------------------------------------------------------------------ profile
    def import_resume(self, path: str) -> None:
        if self.window.profile.dirty:
            self._status("Save or discard your Profile changes before importing", tone="warning")
            return
        self._submit_assist("import_resume", merge_resume_into_library, None, path)

    def preview_resume(self, job_id: str = "") -> None:
        base_profile = db.get_profile()
        profile = (self.window.profile.current_profile(base_profile)
                   if hasattr(self.window, "profile") and hasattr(self.window.profile, "current_profile")
                   else base_profile)
        if profile is None or not (profile.full_name or profile.education or profile.experience or profile.projects or profile.skills):
            self._status("Fill in your profile details first", tone="warning")
            return
        job = db.get_job(job_id) if job_id else None
        if job_id and job is None:
            self._status("Job not found for preview", tone="warning")
            return
        if hasattr(self.window, "profile") and self.window.profile.dirty:
            self._status("Previewing with your unsaved edits. Save changes to keep them.", tone="info")

        def build() -> str:
            from ..tailor import profile_preview_resume, resume_choice
            settings = dict(db.get_settings(), resume_strategy="tailored")
            if job is not None:
                path, _ = resume_choice(profile, job, settings, out_dir=config.TAILORED / "previews")
                return path
            return str(profile_preview_resume(profile, settings))

        self._submit_assist("preview", build, None)

    def add_resume_file(self, path: str, category: str) -> None:
        try:
            source = Path(path)
            if source.suffix.lower() != ".pdf" or not source.is_file():
                raise ValueError("Choose a PDF file")
            config.RESUME_LIBRARY.mkdir(parents=True, exist_ok=True)
            target = config.RESUME_LIBRARY / source.name
            counter = 2
            while target.exists() and target.resolve() != source.resolve():
                target = config.RESUME_LIBRARY / f"{source.stem} ({counter}){source.suffix}"
                counter += 1
            if target.resolve() != source.resolve():
                shutil.copy2(source, target)
            config.secure_file(target)
            db.add_resume_file(str(target), source.stem.replace("_", " ").replace("-", " "), category)
            self._status(f"Added {source.name}", tone="success")
        except Exception as exc:  # noqa: BLE001
            self._status(f"Could not add the resume: {exc}", tone="error")
        self.refresh(include_account=True)

    def _resume_file_update(self, file_id: int, **changes) -> None:
        try:
            db.update_resume_file(file_id, **changes)
        except ValueError as exc:
            self._status(str(exc), tone="warning")
        self.refresh(include_account=True)

    def set_main_resume_from_bank(self, file_id: int) -> None:
        try:
            db.set_main_resume(file_id)
            self._status("Updated main resume", tone="success")
        except Exception as exc:  # noqa: BLE001
            self._status(f"Could not update main resume: {exc}", tone="error")
        self.refresh(include_account=True)

    def remove_resume_file(self, file_id: int) -> None:
        profile = db.get_profile()
        main_path = str(profile.resume_path) if profile else ""
        path = db.remove_resume_file(file_id)
        if path:
            target = Path(path)
            try:
                is_main = bool(main_path and str(target.resolve()) == str(Path(main_path).resolve()))
                if target.parent.resolve() == config.RESUME_LIBRARY.resolve() and target.is_file() and not is_main:
                    target.unlink()
            except OSError:
                pass
            self._status("Resume file removed", tone="success")
        self.refresh(include_account=True)

    def open_resume_folder(self) -> None:
        config.RESUME_LIBRARY.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(config.RESUME_LIBRARY)))

    def set_resume_strategy(self, strategy: str) -> None:
        try:
            db.set_settings({"resume_strategy": strategy})
            label = {"tailored": "a generated one-page resume", "library": "the matching resume file",
                     "original": "your main resume"}.get(strategy, strategy)
            self._status(f"New applications will use {label}", tone="success")
        except Exception as exc:  # noqa: BLE001
            self._status(f"Could not change the resume: {exc}", tone="error")
        self.refresh(include_account=True)

    # ------------------------------------------------------------------ connections
    def _wire_connections(self, luna_panel, email_panel) -> None:
        from .. import agents

        luna_panel.status_requested.connect(
            lambda backend, panel=luna_panel: self._submit_assist("agent_status", agents.status, (panel, backend), backend))
        luna_panel.test_requested.connect(
            lambda values, panel=luna_panel: self._submit_assist("agent_test", agents.test_connection, panel,
                                                                 {**db.get_settings(), **values}))
        luna_panel.login_requested.connect(lambda backend, panel=luna_panel: self._agent_login(panel, backend))
        luna_panel.install_requested.connect(self._agent_install)
        luna_panel.save_key_requested.connect(lambda backend, key, panel=luna_panel: self._agent_key(panel, backend, key))
        email_panel.test_requested.connect(lambda values, panel=email_panel: self._mail_test(panel, values))
        email_panel.help_requested.connect(self.open_url)

    def _agent_login(self, panel, backend: str) -> None:
        from .. import agents
        try:
            shown = agents.open_login(backend)
            panel.status.set_status("Finish signing in", f"A Terminal window is running “{shown}”. Follow its steps, "
                                    "then press Check.", "amber", "test", "Check")
        except Exception as exc:  # noqa: BLE001
            panel.status.set_status("Sign in from a terminal", str(exc), "amber", "test", "Check")

    def _agent_install(self, backend: str) -> None:
        from .. import agents
        spec = agents.BACKENDS.get(backend)
        if spec and spec.install_url:
            self.open_url(spec.install_url)
            self._status(f"Install {spec.name}, then come back and press Check", 9000)

    def _agent_key(self, panel, backend: str, key: str) -> None:
        from .. import agents
        try:
            agents.save_api_key(backend, key)
            panel.key.clear()
            panel.status_requested.emit(backend)
            self._status("API key saved on this computer", tone="success")
        except Exception as exc:  # noqa: BLE001
            self._status(f"Could not save the key: {exc}", tone="error")

    def _mail_test(self, panel, values: dict) -> None:
        from .. import email_tracker
        from ..apply.email_verification import SERVICE, _keyring

        values = dict(values)
        password = values.pop("_imap_password", "")
        mailbox = dict(values.get("email_verification") or {})

        def run():
            username = str(mailbox.get("imap_username") or "").strip().casefold()
            if password:
                class Temporary(email_tracker.Mailbox):
                    def __init__(self, settings):
                        self.host = str(settings.get("imap_host") or "").strip()
                        self.port = int(settings.get("imap_port") or 993)
                        self.username = username
                        self.password = password
                try:
                    mailbox_session = Temporary(mailbox).open()
                    mailbox_session.logout()
                    return True, "Connected. Save settings to keep this app password on this computer."
                except Exception as error:  # noqa: BLE001
                    return False, f"The mail server rejected the sign-in. Check the address and app password. ({str(error)[:120]})"
            if not username or not _keyring().get_password(SERVICE, username):
                return False, "Enter your email address and app password, then test."
            return email_tracker.test_mailbox(mailbox)

        self._submit_assist("mail_test", run, panel)
