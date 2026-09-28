"""CLI: `jobhub serve|scan|apply|tailor|profile|doctor|jobs`."""
from __future__ import annotations

import argparse
import json
import sys

from . import config, db


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jobhub", description="Autonomous job-application hub")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the web UI + hourly scheduler"); s.add_argument("--port", type=int, default=config.PORT)
    s.add_argument("--no-scheduler", action="store_true")
    sub.add_parser("scan", help="scan all sources once and score")
    a = sub.add_parser("apply", help="apply to a job id"); a.add_argument("job_id"); a.add_argument("--dry-run", action="store_true")
    t = sub.add_parser("tailor", help="build a tailored resume PDF for a job id"); t.add_argument("job_id")
    p = sub.add_parser("profile", help="ingest a resume PDF"); p.add_argument("pdf"); p.add_argument("--no-llm", action="store_true")
    j = sub.add_parser("jobs", help="list top jobs"); j.add_argument("-n", type=int, default=25); j.add_argument("--status", default="new")
    sub.add_parser("doctor", help="check backends, browser, profile")
    sub.add_parser("auto", help="run one scan + auto-apply batch (what the hourly loop does)")
    args = ap.parse_args(argv)
    db.init()

    if args.cmd == "serve":
        import uvicorn
        from .server import app
        app.state.scheduler_enabled = not args.no_scheduler
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
        return 0
    if args.cmd == "scan":
        from .scheduler import scan_once
        print(json.dumps(scan_once(), indent=2)); return 0
    if args.cmd == "auto":
        from .scheduler import auto_apply_batch, scan_once
        print(json.dumps(scan_once(), indent=2)); print(json.dumps(auto_apply_batch(), indent=2, default=str)); return 0
    if args.cmd == "apply":
        from .scheduler import apply_one
        res = apply_one(args.job_id, dry_run=args.dry_run)
        print(json.dumps({k: v for k, v in res.items() if k != "log"}, indent=2, default=str))
        for line in res.get("log", []): print("  ", line)
        return 0 if res.get("status") in ("applied", "dry_run") else 1
    if args.cmd == "tailor":
        from .tailor import resume_for_job
        job = db.get_job(args.job_id); prof = db.get_profile()
        if not job or not prof: print("missing job or profile"); return 1
        print(resume_for_job(prof, job, db.get_settings())); return 0
    if args.cmd == "profile":
        from .profile import ingest_resume
        prof = ingest_resume(args.pdf, use_llm=not args.no_llm)
        print(prof.model_dump_json(indent=2, exclude={"raw_text"})); return 0
    if args.cmd == "jobs":
        for j in db.list_jobs(status=args.status, limit=args.n):
            print(f"{j.score:5.1f}  {j.id}  {j.ats:10s} {j.company[:22]:22s} {j.title[:48]:48s} {j.location[:28]}")
        return 0
    if args.cmd == "doctor":
        from .llm import get_llm
        llm = get_llm()
        print("LLM backend:", llm.describe())
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True); b.close()
            print("Playwright chromium: ok")
        except Exception as e:  # noqa: BLE001
            print("Playwright chromium: FAIL", e, "-> run: uv run playwright install chromium")
        prof = db.get_profile()
        print("Profile:", (prof.full_name + " <" + prof.email + ">") if prof else "none (upload a resume)")
        print("Jobs in DB:", db.job_stats())
        print("Settings: auto_apply =", db.get_settings().get("auto_apply"), "dry_run =", db.get_settings().get("dry_run"))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
