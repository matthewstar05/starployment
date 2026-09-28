"""SQLite persistence. Single-file DB in data/jobhub.db, WAL mode, dict rows."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from . import config
from .models import Job, Profile, job_fingerprint, now_iso

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, source TEXT, ats TEXT, company TEXT, title TEXT, location TEXT,
  remote INTEGER DEFAULT 0, salary_min INTEGER, salary_max INTEGER, url TEXT UNIQUE,
  apply_url TEXT, description TEXT, posted_at TEXT, discovered_at TEXT,
  score REAL DEFAULT 0, score_json TEXT DEFAULT '{}', status TEXT DEFAULT 'new',
  raw TEXT DEFAULT '{}', updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, status TEXT, method TEXT,
  started_at TEXT, finished_at TEXT, resume_path TEXT, log TEXT DEFAULT '[]',
  screenshots TEXT DEFAULT '[]', unanswered TEXT DEFAULT '[]', error TEXT,
  candidate_note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS profile (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, found INTEGER DEFAULT 0,
  new_jobs INTEGER DEFAULT 0, applied INTEGER DEFAULT 0, status TEXT, log TEXT DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, msg TEXT, job_id TEXT);
"""

# Preserve every historical ID/application while presenting one vacancy per ATS posting.
# An applied or review-needed record always wins over a duplicate newly discovered row.
DEDUPED_JOBS = """
WITH ranked_jobs AS (
  SELECT jobs.*, ROW_NUMBER() OVER (
    PARTITION BY COALESCE(canonical_key, url, id)
    ORDER BY CASE status WHEN 'applied' THEN 0 WHEN 'applying' THEN 1 WHEN 'queued' THEN 2
      WHEN 'needs_human' THEN 3 WHEN 'failed' THEN 4 WHEN 'skipped' THEN 5 ELSE 6 END,
      score DESC, discovered_at DESC, id
  ) AS posting_rank FROM jobs
)
"""


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    config.secure_file(config.DB_PATH)
    for sidecar in ("-wal", "-shm"):
        path = config.DB_PATH.with_name(config.DB_PATH.name + sidecar)
        if path.exists():
            config.secure_file(path)
    return con


_con: sqlite3.Connection | None = None
_con_path = None


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    global _con, _con_path
    with _lock:
        if _con is not None and _con_path != config.DB_PATH:
            _con.close()
            _con = None
        if _con is None:
            _con = _connect()
            _con_path = config.DB_PATH
            _con.executescript(SCHEMA)
        try:
            yield _con
            _con.commit()
        except Exception:
            _con.rollback()
            raise


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
        for table, fields in {
            "jobs": {"canonical_key": "TEXT"},
            "applications": {"submission_attempted": "INTEGER DEFAULT 0", "worker_pid": "INTEGER",
                              "candidate_note": "TEXT DEFAULT ''"},
            "runs": {"worker_pid": "INTEGER"},
        }.items():
            existing = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for name, definition in fields.items():
                if name not in existing:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        for row in c.execute("SELECT * FROM jobs WHERE canonical_key IS NULL").fetchall():
            job = _job_from_row(row)
            c.execute("UPDATE jobs SET canonical_key=? WHERE id=?", (job_fingerprint(job), job.id))
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_canonical ON jobs(canonical_key)")
    seed_settings()


def close() -> None:
    """Close the shared connection, also useful for isolated tests."""
    global _con, _con_path
    with _lock:
        if _con is not None:
            _con.close()
        _con = _con_path = None


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


# ---------------- settings ----------------
def seed_settings() -> None:
    with conn() as c:
        for k, v in config.DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(v)))


def get_settings() -> dict[str, Any]:
    with conn() as c:
        rows = c.execute("SELECT key, value FROM settings").fetchall()
    s = config.merge_settings(config.DEFAULT_SETTINGS, {})
    for r in rows:
        try:
            s = config.merge_settings(s, {r["key"]: json.loads(r["value"])})
        except Exception:
            pass
    return s


def set_settings(patch: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        updated = config.validate_settings(config.merge_settings(get_settings(), patch))
        with conn() as c:
            for k in patch:
                c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(updated[k])))
    return get_settings()


# ---------------- profile ----------------
def save_profile(p: Profile) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO profile(id, data, updated_at) VALUES (1, ?, ?)",
                  (p.model_dump_json(), now_iso()))


def get_profile() -> Profile | None:
    with conn() as c:
        r = c.execute("SELECT data FROM profile WHERE id = 1").fetchone()
    return Profile.model_validate_json(r["data"]) if r else None


# ---------------- jobs ----------------
def upsert_jobs(jobs: list[Job]) -> int:
    """Insert new jobs; refresh mutable fields of known ones. Returns count of new rows."""
    new = 0
    with conn() as c:
        for j in jobs:
            key = job_fingerprint(j)
            exists = c.execute("SELECT id FROM jobs WHERE id = ? OR url = ? OR canonical_key = ? LIMIT 1", (j.id, j.url, key)).fetchone()
            if exists:
                c.execute(
                    "UPDATE jobs SET company=CASE WHEN length(?)>0 THEN ? ELSE company END, "
                    "title=CASE WHEN length(?)>0 THEN ? ELSE title END, "
                    "location=CASE WHEN length(?)>0 THEN ? ELSE location END, remote=CASE WHEN length(?)>0 THEN ? ELSE remote END, "
                    "posted_at=COALESCE(?, posted_at), salary_min=COALESCE(?, salary_min), salary_max=COALESCE(?, salary_max), "
                    "description=CASE WHEN length(?)>0 THEN ? ELSE description END, "
                    "ats=CASE WHEN ? IN ('greenhouse','lever','ashby') THEN ? ELSE ats END, "
                    "apply_url=CASE WHEN ? IN ('greenhouse','lever','ashby') THEN ? ELSE apply_url END, "
                    "updated_at=? WHERE id=?",
                    (j.company, j.company, j.title, j.title, j.location, j.location, j.location, j.remote, j.posted_at,
                     j.salary_min, j.salary_max, j.description, j.description, j.ats, j.ats,
                     j.ats, j.apply_url, now_iso(), exists["id"]))
                j.id = exists["id"]
                continue
            new += 1
            c.execute(
                "INSERT INTO jobs(id, source, ats, company, title, location, remote, salary_min, salary_max, url, "
                "apply_url, description, posted_at, discovered_at, score, score_json, status, raw, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (j.id, j.source, j.ats, j.company, j.title, j.location, int(j.remote), j.salary_min,
                 j.salary_max, j.url, j.apply_url, j.description, j.posted_at, j.discovered_at, j.score,
                 json.dumps(j.score_json), j.status, json.dumps(j.raw), now_iso()))
            c.execute("UPDATE jobs SET canonical_key=? WHERE id=?", (key, j.id))
    return new


def update_scores(scores: dict[str, tuple[float, dict]]) -> None:
    with conn() as c:
        for jid, (score, detail) in scores.items():
            c.execute("UPDATE jobs SET score=?, score_json=? WHERE id=?", (score, json.dumps(detail), jid))


def _job_from_row(r: sqlite3.Row) -> Job:
    d = dict(r)
    d["remote"] = bool(d.get("remote"))
    for key in ("score_json", "raw"):
        try:
            d[key] = json.loads(d.get(key) or "{}")
        except json.JSONDecodeError:
            # Earlier versions truncated raw JSON mid-string. Preserve the stored row and keep it readable.
            d[key] = {}
    d.pop("updated_at", None)
    return Job.model_validate(d)


def list_jobs(status: str | None = None, q: str | None = None, limit: int = 200,
              min_score: float | None = None, ats: str | None = None,
              offset: int = 0) -> list[Job]:
    sql, args = DEDUPED_JOBS + " SELECT * FROM ranked_jobs WHERE posting_rank=1", []
    if status and status != "all":
        sql += " AND status = ?"; args.append(status)
    if q:
        sql += " AND (title LIKE ? OR company LIKE ? OR location LIKE ?)"; args += [f"%{q}%"] * 3
    if min_score is not None:
        sql += " AND score >= ?"; args.append(min_score)
    if ats:
        sql += " AND ats = ?"; args.append(ats)
    sql += " ORDER BY score DESC, discovered_at DESC LIMIT ? OFFSET ?"
    args.extend((limit, max(0, offset)))
    with conn() as c:
        return [_job_from_row(r) for r in c.execute(sql, args).fetchall()]


def count_jobs(status: str | None = None, q: str | None = None) -> int:
    """Count the same deduplicated vacancy set shown by ``list_jobs``."""
    sql, args = DEDUPED_JOBS + " SELECT COUNT(*) FROM ranked_jobs WHERE posting_rank=1", []
    if status and status != "all":
        sql += " AND status = ?"
        args.append(status)
    if q:
        sql += " AND (title LIKE ? OR company LIKE ? OR location LIKE ?)"
        args.extend([f"%{q}%"] * 3)
    with conn() as c:
        return int(c.execute(sql, args).fetchone()[0])


def get_job(job_id: str) -> Job | None:
    with conn() as c:
        r = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _job_from_row(r) if r else None


def set_job_status(job_id: str, status: str) -> None:
    with conn() as c:
        job = c.execute("SELECT status, canonical_key FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise ValueError("Job not found")
        if job["status"] in ("queued", "applying"):
            raise ValueError("An active application cannot be changed")
        attempted = c.execute(
            "SELECT 1 FROM applications a JOIN jobs j ON j.id=a.job_id WHERE j.canonical_key=? "
            "AND (a.status='applied' OR a.submission_attempted=1) LIMIT 1", (job["canonical_key"],)).fetchone()
        if (job["status"] == "applied" or attempted) and status not in ("applied", "needs_human"):
            raise ValueError("This job has a submission record and cannot be reset for another application")
        c.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (status, now_iso(), job_id))


def requeue_preflight_needs_human() -> int:
    """Retry review-needed forms after candidate facts change, if no submit was attempted."""
    with conn() as c:
        rows = c.execute(
            "SELECT j.id FROM jobs j JOIN applications a ON a.job_id=j.id "
            "WHERE j.status='needs_human' AND a.status='needs_human' AND a.submission_attempted=0 "
            "AND a.id=(SELECT MAX(a2.id) FROM applications a2 WHERE a2.job_id=j.id)"
        ).fetchall()
    count = 0
    for row in rows:
        try:
            set_job_status(row["id"], "new")
            count += 1
        except ValueError:
            # Another application may already have crossed the submit boundary
            # for the same canonical vacancy. It must remain review-only.
            pass
    return count


def job_stats() -> dict[str, int]:
    with conn() as c:
        rows = c.execute(DEDUPED_JOBS + " SELECT status, COUNT(*) n FROM ranked_jobs WHERE posting_rank=1 GROUP BY status").fetchall()
    d = {r["status"]: r["n"] for r in rows}
    d["total"] = sum(d.values())
    return d


# ---------------- applications ----------------
def reserve_application(job_id: str, method: str = "engine") -> int:
    """Atomically reserve the browser and posting across API calls and processes."""
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        job = c.execute("SELECT status, canonical_key FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise ValueError("Job not found")
        duplicate = c.execute(
            "SELECT 1 FROM applications a JOIN jobs j ON j.id=a.job_id WHERE j.canonical_key=? "
            "AND (a.status='applied' OR a.submission_attempted=1) LIMIT 1", (job["canonical_key"],)).fetchone()
        already_applied = c.execute("SELECT 1 FROM jobs WHERE canonical_key=? AND status='applied' LIMIT 1",
                                    (job["canonical_key"],)).fetchone()
        if already_applied or duplicate:
            raise ValueError("This posting already has a submission record; review its application history")
        if job["status"] in ("queued", "applying") or c.execute(
                "SELECT 1 FROM applications WHERE status IN ('queued','running') LIMIT 1").fetchone():
            raise ValueError("An application is already queued or in progress")
        cur = c.execute(
            "INSERT INTO applications(job_id,status,method,started_at,worker_pid) VALUES (?,'queued',?,?,?)",
            (job_id, method, now_iso(), os.getpid()))
        c.execute("UPDATE jobs SET status='queued',updated_at=? WHERE id=?", (now_iso(), job_id))
        return int(cur.lastrowid)


def start_application(app_id: int, job_id: str) -> bool:
    with conn() as c:
        cur = c.execute("UPDATE applications SET status='running',worker_pid=? WHERE id=? AND job_id=? AND status='queued'",
                        (os.getpid(), app_id, job_id))
        if cur.rowcount:
            c.execute("UPDATE jobs SET status='applying',updated_at=? WHERE id=?", (now_iso(), job_id))
        return cur.rowcount == 1


def mark_submission_attempted(job_id: str) -> None:
    """Durably record the submit boundary before browser interaction can send data."""
    with conn() as c:
        c.execute("UPDATE applications SET submission_attempted=1 WHERE job_id=? AND status='running'", (job_id,))


def recover_interrupted() -> None:
    """Leave interrupted attempts for review, never resubmit them after restart."""
    def alive(pid):
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    with conn() as c:
        for row in c.execute("SELECT * FROM applications WHERE status IN ('queued','running')").fetchall():
            if alive(row["worker_pid"]):
                continue
            msg = "Application interrupted by a previous shutdown. Review the employer site before retrying."
            # Legacy running attempts did not persist the submit boundary, so their outcome is unknown.
            attempted = row["submission_attempted"] or (row["status"] == "running" and not row["worker_pid"])
            c.execute("UPDATE applications SET status='needs_human',finished_at=?,error=?,submission_attempted=? WHERE id=?",
                      (now_iso(), msg, int(attempted), row["id"]))
            c.execute("UPDATE jobs SET status='needs_human',updated_at=? WHERE id=?", (now_iso(), row["job_id"]))
        c.execute("UPDATE jobs SET status='needs_human',updated_at=? WHERE status IN ('queued','applying') "
                  "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id=jobs.id AND a.status IN ('queued','running'))", (now_iso(),))
        for row in c.execute("SELECT id,worker_pid FROM runs WHERE status='running'").fetchall():
            if not alive(row["worker_pid"]):
                c.execute("UPDATE runs SET status='interrupted',finished_at=? WHERE id=?", (now_iso(), row["id"]))


def create_application(job_id: str, method: str, resume_path: str = "") -> int:
    app_id = reserve_application(job_id, method)
    start_application(app_id, job_id)
    with conn() as c:
        c.execute("UPDATE applications SET resume_path=? WHERE id=?", (resume_path, app_id))
    return app_id


def finish_application(app_id: int, status: str, log: list[str], screenshots: list[str],
                       unanswered: list[str], error: str = "", resume_path: str | None = None,
                       submission_attempted: bool = False) -> None:
    with conn() as c:
        if status == "applied":
            row = c.execute("SELECT submission_attempted FROM applications WHERE id=?", (app_id,)).fetchone()
            if row is None:
                raise ValueError("Application reservation not found")
            if not row["submission_attempted"] and not submission_attempted:
                raise ValueError("Cannot record an applied status without a durable submission attempt")
        c.execute(
            "UPDATE applications SET status=?, finished_at=?, log=?, screenshots=?, unanswered=?, error=?, "
            "resume_path=COALESCE(?, resume_path),submission_attempted=MAX(submission_attempted,?) WHERE id=?",
            (status, now_iso(), json.dumps(log), json.dumps(screenshots), json.dumps(unanswered), error,
             resume_path, int(submission_attempted), app_id))
        new_status = {"applied": "applied", "dry_run": "new", "needs_human": "needs_human", "failed": "failed"}[status]
        c.execute("UPDATE jobs SET status=?,updated_at=? WHERE id=(SELECT job_id FROM applications WHERE id=?)",
                  (new_status, now_iso(), app_id))


def list_applications(limit: int = 100) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT a.*, j.company, j.title, j.url, j.location, j.ats FROM applications a "
            "LEFT JOIN jobs j ON j.id = a.job_id ORDER BY a.id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("log", "screenshots", "unanswered"):
            d[k] = json.loads(d.get(k) or "[]")
        out.append(d)
    return out


def get_application(app_id: int) -> dict | None:
    for a in list_applications(limit=10000):
        if a["id"] == app_id:
            return a
    return None


def set_application_note(app_id: int, note: str) -> None:
    """Save a candidate's private note without changing attempt evidence or status."""
    if not isinstance(note, str) or len(note) > 4000:
        raise ValueError("Notes must be 4,000 characters or fewer")
    with conn() as c:
        cur = c.execute("UPDATE applications SET candidate_note=? WHERE id=?", (note.strip(), app_id))
        if not cur.rowcount:
            raise ValueError("Application record not found")


def application_stats() -> dict[str, int]:
    with conn() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM applications GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


# ---------------- runs & events ----------------
def create_run() -> int:
    with conn() as c:
        return int(c.execute("INSERT INTO runs(started_at, status,worker_pid) VALUES (?, 'running',?)",
                             (now_iso(), os.getpid())).lastrowid)


def finish_run(run_id: int, found: int, new_jobs: int, applied: int, status: str, log: list[str]) -> None:
    with conn() as c:
        c.execute("UPDATE runs SET finished_at=?, found=?, new_jobs=?, applied=?, status=?, log=? WHERE id=?",
                  (now_iso(), found, new_jobs, applied, status, json.dumps(log), run_id))


def list_runs(limit: int = 20) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r); d["log"] = json.loads(d.get("log") or "[]"); out.append(d)
    return out


def add_event(msg: str, level: str = "info", job_id: str | None = None) -> None:
    with conn() as c:
        c.execute("INSERT INTO events(ts, level, msg, job_id) VALUES (?,?,?,?)", (now_iso(), level, msg, job_id))
        c.execute("DELETE FROM events WHERE id < (SELECT MAX(id) FROM events) - 2000")


def list_events(limit: int = 200) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
