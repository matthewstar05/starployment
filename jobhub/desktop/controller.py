"""Connect the native window to JobHub's durable discovery and apply pipeline."""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox

from .. import db, scheduler
from ..setup import save_setup
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


class DesktopController(QObject):
    """All browser and network work runs on one worker, away from Qt's UI thread."""

    def __init__(self, window: JobHubWindow, *, scheduler_enabled: bool = True,
                 show_setup_on_start: bool = True, force_setup_on_start: bool = False):
        super().__init__(window)
        db.init()
        db.recover_interrupted()
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
        self._worker = threading.Thread(target=self._work, name="jobhub-desktop-worker", daemon=True)
        self._worker.start()

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
        self._status("Applying… JobHub will verify the employer's response", 0, "busy")

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
        self._status(f"{kind.title()} failed: {error}", 15_000, "error")
        db.add_event(f"Desktop {kind} failed: {error}", "error")
        self.refresh()

    def refresh(self, *, include_account: bool = False) -> None:
        try:
            job_query = self.window.jobs.query
            job_status = self.window.jobs.selected_status
            total_jobs = db.count_jobs(status=job_status, q=job_query)
            page_size = self.window.jobs.page_size
            self.window.jobs.page = min(
                self.window.jobs.page,
                max(0, (total_jobs - 1) // page_size),
            )
            settings = db.get_settings()
            self.window.set_data(
                profile=db.get_profile() if include_account else None,
                settings=settings if include_account else None,
                jobs=db.list_jobs(
                    status=job_status,
                    q=job_query,
                    limit=page_size,
                    offset=self.window.jobs.page * page_size,
                ),
                total_jobs=total_jobs,
                top_jobs=db.list_jobs(status="new", limit=5),
                applications=db.list_applications(limit=100),
                stats=db.job_stats(),
                events=db.list_events(limit=60),
            )
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
            profile, settings, missing = save_setup(
                profile_patch=profile_patch,
                settings_patch=payload.get("settings") or {},
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
            self._status("Setup saved. JobHub is ready" + (f" · {requeued} jobs requeued" if requeued else ""), tone="success")
            if self.scheduler_enabled:
                self._next_cycle_at = time.time() + 2
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self._wizard, "Could not save setup", str(exc))

    def save_profile(self, profile_patch: dict) -> None:
        try:
            patch = dict(profile_patch)
            skills = patch.pop("skills", None)
            profile, _, missing = save_setup(profile_patch=patch)
            if skills is not None:
                profile.skills = [str(x).strip() for x in skills if str(x).strip()]
                db.save_profile(profile)
                scheduler.rescore()
            if not missing:
                db.requeue_preflight_needs_human()
            self.refresh(include_account=True)
            if missing:
                self._status("Profile saved · setup still needs " + ", ".join(missing), tone="warning")
            else:
                self._status("Profile saved", tone="success")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self.window, "Could not save profile", str(exc))

    def save_settings(self, patch: dict) -> None:
        try:
            patch = dict(patch)
            imap_password = str(patch.pop("_imap_password", ""))
            forget_imap_password = bool(patch.pop("_forget_imap_password", False))
            if imap_password and forget_imap_password:
                raise ValueError("Choose either a new mailbox password or remove the saved one")
            if imap_password or forget_imap_password or patch.get("email_verification", {}).get("enabled"):
                from ..config import merge_settings, validate_settings
                from ..apply.email_verification import delete_password, has_password, store_password

                candidate = validate_settings(merge_settings(db.get_settings(), patch))
                verification = candidate["email_verification"]
                username = verification["imap_username"]
                if imap_password and not username:
                    raise ValueError("Enter the mailbox sign-in email before saving its app password")
                if verification["enabled"] and (
                    forget_imap_password or (not imap_password and not has_password(username))
                ):
                    raise ValueError("Add a mailbox app password before enabling email verification")
                if imap_password:
                    store_password(username, imap_password)
                elif forget_imap_password:
                    previous = db.get_settings().get("email_verification", {}).get("imap_username", "")
                    if previous or username:
                        delete_password(previous or username)
            previous_appearance = db.get_settings().get("appearance")
            settings = db.set_settings(patch)
            if "appearance" in patch and settings["appearance"] != previous_appearance:
                from .styles import apply_appearance

                self.window.crossfade(lambda: apply_appearance(QApplication.instance(), settings["appearance"]))
            if SCORING_KEYS & patch.keys():
                scheduler.rescore()
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
        scheduler.STATE.stop = True
        self._tasks.put(None)
