"""Application dispatcher: pick an adapter by ATS/URL and run the shared form engine."""
from __future__ import annotations

from .. import db
from ..models import ApplyResult, Job, Profile
from .adapters import adapter_for
from .engine import FormEngine


def apply_to_job(job: Job, profile: Profile, settings: dict, resume_path: str, dry_run: bool | None = None) -> ApplyResult:
    dry = settings.get("dry_run", False) if dry_run is None else dry_run
    adapter = adapter_for(job)
    if adapter is None:
        return ApplyResult(status="needs_human", message=f"No adapter for ATS '{job.ats}' ({job.apply_url}). Open manually.")
    engine = FormEngine(job=job, profile=profile, settings=settings, resume_path=resume_path, adapter=adapter, dry_run=dry)
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
