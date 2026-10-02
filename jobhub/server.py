"""FastAPI app: JSON API for the web UI + static files + background scheduler."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import config, db, scheduler
from .llm import get_llm


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    db.recover_interrupted()
    scheduler.STATE.stop = False
    task = None
    if getattr(app.state, "scheduler_enabled", True):
        task = asyncio.create_task(scheduler.loop())
    # warm LLM detection off the request path
    asyncio.get_event_loop().run_in_executor(None, get_llm)
    try:
        yield
    finally:
        scheduler.STATE.stop = True
        scheduler.STATE.next_scan_at = None
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Starployment", version="0.4.0", lifespan=lifespan)
app.mount("/screenshots", StaticFiles(directory=str(config.SCREENSHOTS)), name="screenshots")
app.mount("/static", StaticFiles(directory=str(config.WEB)), name="static")


@app.middleware("http")
async def local_requests_only(request: Request, call_next):
    """Reject DNS rebinding and web pages attempting to operate this local agent."""
    if request.url.hostname not in ("localhost", "127.0.0.1", "::1"):
        return JSONResponse({"detail": "Starployment only accepts localhost requests"}, status_code=403)
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme not in ("http", "https") or parsed.netloc != request.url.netloc:
            return JSONResponse({"detail": "Cross-origin requests are not permitted"}, status_code=403)
    if request.headers.get("sec-fetch-site") == "cross-site":
        return JSONResponse({"detail": "Cross-site requests are not permitted"}, status_code=403)
    return await call_next(request)


@app.get("/")
def index():
    return FileResponse(str(config.WEB / "index.html"))


@app.get("/api/state")
def state():
    prof = db.get_profile()
    llm = get_llm()
    runs = db.list_runs(5)
    last_scan = scheduler.STATE.last_scan_at or (runs[0]["finished_at"] if runs else None)
    return {
        "profile": {"name": prof.full_name, "email": prof.email, "resume": Path(prof.resume_path).name,
                    "skills": len(prof.skills), "projects": len(prof.projects), "experience": len(prof.experience)} if prof else None,
        "settings": db.get_settings(),
        "scheduler": {"enabled": getattr(app.state, "scheduler_enabled", True),
                      "running_scan": scheduler.STATE.running_scan, "running_apply": scheduler.STATE.running_apply,
                      "last_scan_at": last_scan, "next_scan_at": scheduler.STATE.next_scan_at,
                      "last_error": scheduler.STATE.last_error},
        "llm": llm.describe(),
        "jobs": db.job_stats(),
        "applications": db.application_stats(),
        "runs": runs,
        "headless": config.HEADLESS,
    }


@app.get("/api/jobs")
def jobs(status: str = "new", q: str | None = None, limit: int = Query(150, ge=1, le=1000), min_score: float | None = None, ats: str | None = None):
    return [j.model_dump(exclude={"description", "raw"}) | {"desc_len": len(j.description)} for j in
            db.list_jobs(status=status, q=q, limit=limit, min_score=min_score, ats=ats)]


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    j = db.get_job(job_id)
    if not j:
        raise HTTPException(404)
    return j.model_dump()


@app.post("/api/jobs/import")
def import_job(data: dict):
    """Import one currently listed public ATS posting from its URL."""
    url = data.get("url") if isinstance(data, dict) else None
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(422, "A public job URL is required")
    from .sources.import_job import JobImportError, import_job as do_import
    try:
        imported, created = do_import(url)
    except JobImportError as error:
        raise HTTPException(422, str(error)) from error
    from .scoring import score_job
    score, detail = score_job(imported, db.get_profile(), db.get_settings())
    db.update_scores({imported.id: (score, detail)})
    imported.score, imported.score_json = score, detail
    db.add_event(f"Job imported: {imported.company} / {imported.title}", "info", imported.id)
    return {"job": imported.model_dump(), "created": created}


@app.post("/api/jobs/{job_id}/apply")
async def apply(job_id: str, bg: BackgroundTasks, dry_run: bool | None = None):
    if not db.get_job(job_id):
        raise HTTPException(404)
    try:
        application_id = scheduler.queue_application(job_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    bg.add_task(asyncio.to_thread, scheduler.apply_one, job_id, dry_run, application_id)
    return {"queued": True, "application_id": application_id}


@app.post("/api/jobs/{job_id}/status")
def set_status(job_id: str, status: str):
    if status not in ("new", "skipped", "applied", "needs_human", "failed"):
        raise HTTPException(400)
    if not db.get_job(job_id):
        raise HTTPException(404, "Job not found")
    try:
        db.set_job_status(job_id, status)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return {"ok": True}


@app.post("/api/jobs/{job_id}/tailor")
async def tailor(job_id: str):
    from .tailor import resume_for_job
    j, p = db.get_job(job_id), db.get_profile()
    if not j or not p:
        raise HTTPException(404)
    try:
        j = await asyncio.to_thread(scheduler.enrich_job, j)
    except scheduler.PostingRefreshError as error:
        raise HTTPException(409, str(error)) from error
    path = await asyncio.to_thread(resume_for_job, p, j, db.get_settings())
    return {"path": path, "url": f"/api/resume/file?path={quote(str(path), safe='')}"}


@app.get("/api/resume/file")
def resume_file(path: str):
    p = Path(path).resolve()
    if not (any(p.is_relative_to(root.resolve()) for root in (config.RESUMES, config.TAILORED))
            and p.is_file() and p.suffix.lower() == ".pdf"):
        raise HTTPException(404)
    return FileResponse(str(p), media_type="application/pdf")


@app.get("/api/applications")
def applications(limit: int = Query(100, ge=1, le=1000)):
    return db.list_applications(limit)


@app.get("/api/events")
def events(limit: int = Query(150, ge=1, le=2000)):
    return db.list_events(limit)


@app.post("/api/scan")
async def scan(bg: BackgroundTasks):
    if scheduler.STATE.running_scan:
        return {"started": False, "reason": "already running"}
    bg.add_task(asyncio.to_thread, scheduler.scan_once)
    return {"started": True}


@app.post("/api/auto")
async def auto(bg: BackgroundTasks):
    bg.add_task(asyncio.to_thread, scheduler.auto_apply_batch)
    return {"started": True}


@app.get("/api/profile")
def profile():
    p = db.get_profile()
    return p.model_dump(exclude={"raw_text"}) if p else None


@app.put("/api/profile")
def put_profile(data: dict):
    from .models import Profile
    cur = db.get_profile()
    if set(data) - (set(Profile.model_fields) - {"raw_text", "resume_path"}):
        raise HTTPException(422, "Only editable profile fields may be changed; upload a PDF to change the resume")
    merged = (cur.model_dump() if cur else {}) | data
    try:
        p = Profile.model_validate(merged)
    except ValidationError as error:
        raise HTTPException(422, str(error)) from error
    db.save_profile(p)
    scheduler.rescore()
    db.add_event("Profile edited in UI")
    return p.model_dump(exclude={"raw_text"})


@app.post("/api/profile/resume")
async def upload_resume(file: UploadFile, use_llm: bool = True):
    from .profile import ingest_resume
    if Path(file.filename or "").suffix.lower() != ".pdf":
        raise HTTPException(422, "Upload a PDF resume")
    content = await file.read(10 * 1024 * 1024 + 1)
    await file.close()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "Resume exceeds the 10 MB limit")
    if not content.startswith(b"%PDF-"):
        raise HTTPException(422, "The uploaded file is not a PDF")
    filename = Path(file.filename or "resume.pdf").stem[:80]
    dest = config.RESUMES / f"{filename}-{uuid4().hex[:10]}.pdf"
    dest.write_bytes(content)
    try:
        p = await asyncio.to_thread(ingest_resume, dest, use_llm)
    except Exception as error:
        dest.unlink(missing_ok=True)
        raise HTTPException(422, f"Resume could not be read: {error}") from error
    await asyncio.to_thread(scheduler.rescore)
    return p.model_dump(exclude={"raw_text"})


@app.get("/api/settings")
def settings():
    return db.get_settings()


@app.put("/api/settings")
def put_settings(patch: dict):
    try:
        s = db.set_settings(patch)
    except (ValidationError, ValueError) as error:
        raise HTTPException(422, str(error)) from error
    scheduler.rescore()
    db.add_event("Settings updated: " + ", ".join(patch.keys()))
    return s
