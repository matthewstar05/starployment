"""Application dispatcher: pick an adapter by ATS/URL and run the shared form engine."""
from __future__ import annotations

from .. import db
from ..models import ApplyResult, Job, Profile
from .adapters import adapter_for
from .engine import FormEngine


def apply_to_job(job: Job, profile: Profile, settings: dict, resume_path: str, dry_run: bool | None = None,
                 assist: bool = False) -> ApplyResult:
    dry = False if assist else settings.get("dry_run", False) if dry_run is None else dry_run
    adapter = adapter_for(job)
    if adapter is None:
        return ApplyResult(status="needs_human", message=f"No adapter for ATS '{job.ats}' ({job.apply_url}). Open manually.")
    if adapter.assist_only and not assist and not (settings.get("workday") or {}).get("send"):
        # Workday: every application sits behind the employer's account. Unattended sends need
        # Settings → Workday → Send Workday applications (apply/workday.py signs in from the vault).
        return ApplyResult(status="needs_human", message="This employer's Workday application needs you signed in. "
                                                         "Choose Finish in browser.")
    engine_class = FormEngine
    if adapter.name == "workday":
        from .workday import WorkdayEngine
        engine_class = WorkdayEngine
    engine = engine_class(job=job, profile=profile, settings=settings, resume_path=resume_path, adapter=adapter, dry_run=dry,
                        assist=assist)
    try:
        return engine.run()
    except Exception as e:  # noqa: BLE001
        db.add_event(f"apply crashed for {job.company} / {job.title}: {e}", "error", job.id)
        return ApplyResult(
            status="needs_human" if engine.submission_attempted else "failed",
            message=f"Application engine stopped: {str(e).splitlines()[0][:240]}",
            screenshots=engine.screenshots, log=engine.log, unanswered=engine.unanswered,
            resume_path=resume_path, submission_attempted=engine.submission_attempted,
        )
