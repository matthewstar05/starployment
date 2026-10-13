"""Read-only automation inventory and answer-readiness sample. Never opens a send browser.

    .venv/bin/python scripts/automation_audit.py --per-ats 40

The live database is backed up through a read-only connection. Candidate facts
stay in memory; public APIs receive posting IDs only. All app writes go to a
throwaway store. Counts describe support and preflight readiness, never confirmed
applications. The private report is excluded from source and packaging.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["JOBHUB_DATA_DIR"] = str(Path(tempfile.mkdtemp(prefix="starployment-readiness-")) / "data")

from jobhub import config, db, prepare  # noqa: E402
from jobhub.apply.adapters import adapter_for  # noqa: E402
from jobhub.locations import is_location_acceptable  # noqa: E402
from jobhub.models import Profile, job_fingerprint  # noqa: E402
from jobhub.priority import held_from_automation  # noqa: E402
from jobhub.scoring import score_job  # noqa: E402


def audit(path: Path, per_ats: int, workers: int, all_jobs: bool = False) -> dict:
    snapshot = config.DATA / "snapshot.db"
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as source, sqlite3.connect(snapshot) as copy:
        source.backup(copy)
    with sqlite3.connect(snapshot) as source:
        source.row_factory = sqlite3.Row
        jobs = [db._job_from_row(r) for r in source.execute("SELECT * FROM jobs ORDER BY score DESC, id")]
        profile_row = source.execute("SELECT data FROM profile WHERE id=1").fetchone()
        profile = Profile.model_validate_json(profile_row["data"]) if profile_row else Profile()
        settings = config.merge_settings(config.DEFAULT_SETTINGS, {})
        for row in source.execute("SELECT key,value FROM settings"):
            settings = config.merge_settings(settings, {row["key"]: json.loads(row["value"])})
    db.init()
    unique = {}
    for job in jobs:
        unique.setdefault(job_fingerprint(job), job)
    inventory = Counter()
    supported = Counter()
    blocked_hosts = Counter()
    samples = defaultdict(list)
    seen_companies = defaultdict(set)
    for job in unique.values():
        inventory[job.ats] += 1
        adapter = adapter_for(job)
        if adapter is None:
            from urllib.parse import urlsplit
            blocked_hosts[urlsplit(job.apply_url or job.url).hostname or "unknown"] += 1
            continue
        supported[adapter.name] += 1
        if job.status not in ("new", "prepared") or not is_location_acceptable(job.location):
            continue
        company = job.company.strip().casefold()
        if not all_jobs and (company in seen_companies[adapter.name] or len(samples[adapter.name]) >= per_ats):
            continue
        seen_companies[adapter.name].add(company)
        job.ats = adapter.name
        samples[adapter.name].append(job)

    def check(job):
        questions, method = prepare.discover_questions(job)
        record = {"job_id": job.id, "ats": job.ats, "company": job.company, "url": job.apply_url or job.url,
                  "held_for_review": held_from_automation(job, settings.get("priority_companies") or [])}
        if method in ("unavailable", "unsupported") or not questions:
            return {**record, "status": "unavailable", "missing": []}
        prepare.answer_questions(questions, profile, settings, job)
        missing = [q["label"] for q in questions if prepare.missing(q)]
        score, details = score_job(job, profile, settings)
        return {**record, "status": "needs_answers" if missing else "ready", "missing": missing,
                "meets_auto_apply_score": (score >= float(settings.get("min_score_to_apply", 60)) and
                                           details.get("auto_apply_eligible") is True),
                "questions": len(questions)}

    records = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index, result in enumerate(pool.map(check, [j for group in samples.values() for j in group]), 1):
            records.append(result)
            if index % 20 == 0:
                print(f"Checked {index} public forms", flush=True)
    readiness = defaultdict(Counter)
    missing = Counter()
    queue_ready = Counter()
    for record in records:
        readiness[record["ats"]][record["status"]] += 1
        if record["status"] == "ready" and not record["held_for_review"]:
            readiness[record["ats"]]["ready_without_priority_hold"] += 1
            if record.get("meets_auto_apply_score"):
                queue_ready[record["ats"]] += 1
        missing.update(record["missing"])
    return {"timestamp": datetime.now(timezone.utc).isoformat(), "rows": len(jobs), "unique_vacancies": len(unique),
            "inventory_by_ats": dict(inventory), "supported_by_adapter": dict(supported),
            "unsupported_hosts": dict(blocked_hosts.most_common()),
            "sample_policy": ("All unique supported new/prepared vacancies in eligible locations" if all_jobs else
                              f"Up to {per_ats} highest-scoring eligible-location postings per ATS, one per employer"),
            "readiness": {k: dict(v) for k, v in readiness.items()}, "missing_questions": dict(missing.most_common()),
            "automatic_queue_readiness": {"min_score": float(settings.get("min_score_to_apply", 60)),
                                          "ready_without_priority_hold_and_score_eligible": dict(queue_ready),
                                          "total": sum(queue_ready.values()),
                                          "automatic_sending_enabled": settings.get("auto_apply", False)},
            "records": records,
            "limits": ["Public form answer readiness is not browser validation or confirmed submission.",
                       "Unavailable or empty forms are excluded from ready counts.",
                       "Priority holds, per-run caps, live validation and CAPTCHA still apply.",
                       "No Luna drafts, assumed facts, uploads, mailbox access or submissions were performed."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--per-ats", type=int, default=40)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--all", action="store_true", help="Check every eligible unique vacancy instead of sampling")
    args = parser.parse_args()
    path = args.db
    if path is None:
        support = Path.home() / "Library/Application Support"
        path = next((support / name / "jobhub.db" for name in ("Starployment", "JobHub")
                     if (support / name / "jobhub.db").exists()), None)
    if path is None:
        parser.error("No installed database found; pass --db")
    report = audit(path, max(0, args.per_ats), max(1, args.workers), args.all)
    output = ROOT / "tmp" / "automation_audit.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2))
    output.chmod(0o600)
    print(json.dumps({k: report[k] for k in ("rows", "unique_vacancies", "supported_by_adapter", "readiness")}, indent=2))
    print(f"Report: {output}")


if __name__ == "__main__":
    main()
