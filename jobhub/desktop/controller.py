"""Connect the native window to Starployment's durable discovery and apply pipeline."""
from __future__ import annotations

import queue
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox

from .. import config, db, prepare, scheduler
from ..setup import merge_resume_into_library, save_resume_library, save_setup
from .ui import JobHubWindow, SetupWizard

# Settings keys that change job rankings. Saving anything else skips the
# full-catalog rescore, which can take seconds on a large job table.
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


class DesktopController(QObject):
    """All browser and network work runs on one worker, away from Qt's UI thread."""

    def __init__(self, window: JobHubWindow, *, scheduler_enabled: bool = True,
                 show_setup_on_start: bool = True, force_setup_on_start: bool = False):
        super().__init__(window)
        db.init()
        db.recover_interrupted()
        db.recover_drafts()
        try:
            prepare.repair_drafts()   # drafts from earlier versions may repeat checkbox options
        except Exception as exc:  # noqa: BLE001 - never block startup on a cleanup
            db.add_event(f"Could not tidy prepared applications: {exc}", "warn")
        app = QApplication.instance()
        if app is not None:
            from .styles import apply_appearance

            apply_appearance(app, db.get_settings().get("appearance", "system"))
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
        self._worker = threading.Thread(target=self._work, name="starployment-browser-worker", daemon=True)
        self._worker.start()
        # Preparation, Luna, inbox checks, and connection tests never use the
        # browser, so they run beside it and never delay a send or a scan.
        self._assist = ThreadPoolExecutor(max_workers=2, thread_name_prefix="starployment-assist")
        self._prep_stop = threading.Event()
        self._preparing = False
        self._sending: set[int] = set()
        self._mail_state: dict = {"last_checked": None, "unmatched": 0}

        window.scan_requested.connect(self.scan)
        window.apply_requested.connect(self.apply)
        window.status_requested.connect(self.set_job_status)
        window.import_requested.connect(self.import_job)
        window.save_profile_requested.connect(self.save_profile)
        window.save_settings_requested.connect(self.save_settings)
        window.appearance_requested.connect(self.set_appearance)
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
        files.default_requested.connect(lambda fid: self._resume_file_update(fid, is_default=True))
        files.remove_requested.connect(self.remove_resume_file)
        files.open_requested.connect(self.open_resume)
        files.open_folder_requested.connect(self.open_resume_folder)
        files.strategy_changed.connect(self.set_resume_strategy)
        window.settings.resume_strategy_changed.connect(self.set_resume_strategy)
        # Connections
        self._wire_connections(window.settings.luna, window.settings.email)
        self.refresh(include_account=True)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh)
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
            auto_apply=bool(settings.get("auto_apply")),
        )

    def _work(self) -> None:
        while not self._stopping.is_set():
            try:
                task = self._tasks.get(timeout=0.5)
            except queue.Empty:
                continue
            if task is None:
                return
            kind, payload = task
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
                    if db.get_settings().get("email_tracking", {}).get("enabled"):
                        self._submit_assist("mail_sync", self._mail_sync_job, False)
                elif kind == "apply":
                    result = scheduler.apply_one(payload)
                elif kind == "send":
                    result = prepare.send_draft(int(payload))
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
        interval = max(5, int(settings.get("scan_interval_min", 60))) * 60
        self._next_cycle_at = started + interval
        scheduler.STATE.next_scan_at = str(int(self._next_cycle_at))
        self._tasks.put(("cycle", None))
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
            partial = scan.get("status") == "partial"
            self._status(summary + (" · some sources had issues" if partial else ""),
                         tone="warning" if partial else "success")
            # After a long scan or wake from sleep, start one fresh interval
            # instead of trying to replay every missed hourly cycle.
            if time.time() >= self._next_cycle_at:
                self._next_cycle_at = time.time() + max(5, int(db.get_settings().get("scan_interval_min", 60))) * 60
        elif kind == "import":
            result = result if isinstance(result, dict) else {}
            self._status(f"Added {result.get('company', '')} · {result.get('title', '')}", tone="success")
            self.window.show_jobs("all")
        elif kind == "send":
            result = result if isinstance(result, dict) else {}
            self._sending.discard(int(result.get("draft_id") or 0))
            status = str(result.get("status", ""))
            detail = result.get("message") or result.get("error") or ""
            label = {"applied": "Application sent and confirmed", "dry_run": "Practice run finished, nothing submitted",
                     "needs_human": "Application needs your review", "failed": "Application issue",
                     "blocked": "Could not send"}.get(status, "Application finished")
            tone = {"applied": "success", "dry_run": "success", "needs_human": "warning"}.get(status, "error")
            remaining = len(self._sending)
            suffix = f" · {remaining} more sending" if remaining else ""
            self._status(label + (f" · {detail}" if detail and status != "applied" else "") + suffix,
                         9000 if not remaining else 0, tone if not remaining else "busy")
        else:
            result = result if isinstance(result, dict) else {}
            status = str(result.get("status", "finished"))
            detail = result.get("message") or result.get("error") or ""
            tone = {"applied": "success", "dry_run": "success", "needs_human": "warning", "failed": "error"}.get(status, "info")
            label = {"applied": "Application confirmed", "dry_run": "Practice run finished",
                     "needs_human": "Application needs your review", "failed": "Application issue"}.get(status, f"Application {status}")
            self._status(label + (f" · {detail}" if detail else ""), 9000, tone)
        self.refresh()

    def _task_failed(self, kind: str, error: str) -> None:
        if kind == "cycle":
            self._pending_scan = False
        if kind == "send":
            self._sending.clear()
        self._status(f"{kind.title()} failed: {error}", 15_000, "error")
        db.add_event(f"Desktop {kind} failed: {error}", "error")
        self.refresh()

    def refresh(self, *, include_account: bool = False) -> None:
        try:
            jobs_view = self.window.jobs
            job_query = jobs_view.query
            job_status = jobs_view.selected_status
            posted_hours = jobs_view.posted_hours or None
            total_jobs = db.count_jobs(status=job_status, q=job_query, posted_within_hours=posted_hours)
            page_size = self.window.jobs.page_size
            self.window.jobs.page = min(
                self.window.jobs.page,
                max(0, (total_jobs - 1) // page_size),
            )
            settings = db.get_settings()
            self.window.set_data(
                profile=db.get_profile() if include_account else None,
                settings=settings if include_account or self._settings_changed(settings) else None,
                jobs=db.list_jobs(
                    status=job_status,
                    q=job_query,
                    limit=page_size,
                    offset=self.window.jobs.page * page_size,
                    posted_within_hours=posted_hours,
                    order=jobs_view.sort_key,
                    descending=jobs_view.sort_descending,
                ),
                total_jobs=total_jobs,
                top_jobs=db.list_jobs(status="new", limit=5),
                applications=db.list_applications(limit=300),
                stats={**db.job_stats(), **db.fresh_counts()},
                events=db.list_events(limit=60),
                drafts=db.list_drafts(),
                tracker=db.list_tracker(),
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
                save_resume_library(library)
                if "skills" in library:
                    scheduler.rescore()
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
            self._store_mail_secret(patch)
            previous_appearance = db.get_settings().get("appearance")
            settings = db.set_settings(patch)
            if "appearance" in patch and settings["appearance"] != previous_appearance:
                from .styles import apply_appearance

                self.window.crossfade(lambda: apply_appearance(QApplication.instance(), settings["appearance"]))
            if SCORING_KEYS & patch.keys():
                scheduler.rescore()
            if "answers" in patch:
                prepare.reanswer_open_drafts()
            self.refresh(include_account=True)
            self._status("Settings saved", tone="success")
            if settings.get("setup_complete") and self.scheduler_enabled and not self._next_cycle_at:
                self._next_cycle_at = time.time() + 2
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self.window, "Could not save settings", str(exc))

    def open_url(self, url: str) -> None:
        if url.startswith(("https://", "http://")):
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
        self._prep_stop.set()
        scheduler.STATE.stop = True
        self._tasks.put(None)
        self._assist.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ assist worker
    _last_settings_signature: object = None

    def _settings_changed(self, settings: dict) -> bool:
        """Background work (Luna remembering answers) can change settings; refresh views when it does."""
        import json
        signature = json.dumps(settings, sort_keys=True, default=str)
        changed = signature != self._last_settings_signature
        self._last_settings_signature = signature
        return changed and not (self.window.settings.dirty or self.window.profile.dirty)

    def _submit_assist(self, kind: str, function, context: object = None, *args) -> None:
        if self._stopping.is_set():
            return   # the window is closing; late UI signals must not start new work

        def run():
            try:
                result = function(*args)
            except Exception as exc:  # noqa: BLE001
                self.signals.assist_failed.emit(kind, context, str(exc))
            else:
                self.signals.assist_finished.emit(kind, context, result)
        try:
            self._assist.submit(run)
        except RuntimeError:   # shut down between the check and the submit
            pass

    def _assist_finished(self, kind: str, context: object, result: object) -> None:
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
            starred = int(report.get("starred", 0))
            if starred:
                text += f" · {starred} starred for you to send one by one"
            if not report.get("considered"):
                text = "No new matches fit this plan. Widen the posting window, lower the minimum match, or run a scan."
            self._status(text, 9000, "success" if prepared else "warning")
            if prepared:
                self.window.show_applications("review")
        elif kind == "prepare_one":
            self._status("Application prepared · review it in Applications", tone="success")
            self.window.show_draft_for_job(str(context))
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
            return
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
        if kind == "plan":
            self._preparing = False
        if kind in ("agent_test", "mail_test") and context is not None:
            context.set_test_result(False, error)
            return
        if kind == "agent_status" and context is not None:
            panel, backend = context
            panel.set_status(backend, False, "Could not check the connection", error[:200], "test")
            return
        label = {"plan": "Preparing applications", "prepare_one": "Preparing the application", "luna": "AI Autofill",
                 "mail_sync": "Inbox check", "preview": "Resume preview", "import_resume": "Resume import"}.get(kind, kind)
        self._status(f"{label} failed: {error}", 15_000, "error")
        db.add_event(f"Desktop {kind} failed: {error}", "error")
        self.refresh()

    def _prep_progress(self, text: str, done: int, total: int) -> None:
        self._status(f"{text} · {done} of {total}", 0, "busy")

    # ------------------------------------------------------------------ apply flow
    def start_plan(self, plan: dict) -> None:
        """Prepare the top matches for review (Overview → Apply)."""
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
        try:
            db.set_settings({"apply_plan": {"count": int(plan["count"]), "min_score": float(plan["min_score"]),
                                            "one_per_company": bool(plan["one_per_company"]),
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
                                         posted_within_hours=hours, order=order)

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
        if job.ats not in prepare.SUPPORTED_ATS:
            self._status("This posting uses a form Starployment cannot fill. Open the posting to apply yourself.",
                         tone="warning")
            return
        self._status(f"Preparing {job.company} · {job.title}…", 0, "busy")

        def run() -> int:
            settings = db.get_settings()
            draft_id = prepare.prepare_job(job, profile, settings)
            if prepare.luna_ready(settings):
                try:
                    fill = (prepare.autofill if (settings.get("luna") or {}).get("autofill_on_prepare", True)
                            else prepare.luna_fill)
                    fill(draft_id, settings)
                except Exception as error:  # noqa: BLE001
                    db.update_draft(draft_id, error=f"Luna could not draft answers: {str(error)[:200]}")
            return draft_id

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
            settings.get("priority_companies") or [])
        if not candidates:
            self._status("Nothing is ready to send. Use Autofill all or answer the open questions first."
                         + (f" {len(held)} starred application{'s' if len(held) != 1 else ''} wait for you to "
                            "approve each one." if held else ""))
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
        self.prepare_job(job_id)

    def _queue_send(self, draft_ids: list[int]) -> None:
        if not db.get_settings().get("setup_complete"):
            self._status("Complete setup before sending")
            return
        for draft_id in draft_ids:
            if draft_id in self._sending:
                continue
            self._sending.add(draft_id)
            self._tasks.put(("send", draft_id))
        practice = db.get_settings().get("dry_run")
        count = len(self._sending)
        self._status(("Practice run" if practice else "Sending") + (f" · {count} application{'s' if count != 1 else ''}"
                     if count > 1 else "") + "…", 0, "busy")
        self.refresh()

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
                        if include_attempts else "") + ". Everything submitted was kept.", tone="success")
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

    def _mail_sync_job(self):
        from .. import email_tracker
        return email_tracker.sync(db.get_settings())

    def sync_email(self, manual: bool = False) -> None:
        if not db.get_settings().get("email_tracking", {}).get("enabled"):
            self._status("Turn on reply tracking in Settings → Connections → Email", tone="info")
            self.window.show_connections()
            return
        if manual:
            self._status("Checking your inbox…", 0, "busy")
        self._submit_assist("mail_sync", self._mail_sync_job, manual)

    # ------------------------------------------------------------------ profile
    def import_resume(self, path: str) -> None:
        if self.window.profile.dirty:
            self._status("Save or discard your Profile changes before importing", tone="warning")
            return
        self._submit_assist("import_resume", merge_resume_into_library, None, path)

    def preview_resume(self, job_id: str) -> None:
        job, profile = db.get_job(job_id), db.get_profile()
        if job is None or profile is None:
            return
        if self.window.profile.dirty:
            self._status("Previewing your saved library. Save changes to include them.", tone="info")

        def build() -> str:
            from ..tailor import resume_choice
            settings = dict(db.get_settings(), resume_strategy="tailored")
            path, _ = resume_choice(profile, job, settings, out_dir=config.TAILORED / "previews")
            return path

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

    def remove_resume_file(self, file_id: int) -> None:
        path = db.remove_resume_file(file_id)
        if path:
            target = Path(path)
            try:
                if target.parent.resolve() == config.RESUME_LIBRARY.resolve() and target.is_file():
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
            self._status("API key saved in your system credential vault", tone="success")
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
                    return True, "Connected. Save settings to keep this app password in your system vault."
                except Exception as error:  # noqa: BLE001
                    return False, f"The mail server rejected the sign-in. Check the address and app password. ({str(error)[:120]})"
            if not username or not _keyring().get_password(SERVICE, username):
                return False, "Enter your email address and app password, then test."
            return email_tracker.test_mailbox(mailbox)

        self._submit_assist("mail_test", run, panel)
