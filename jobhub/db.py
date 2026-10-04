"""SQLite persistence. Single-file DB in data/jobhub.db, WAL mode, dict rows."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from . import config
from .models import Job, Profile, board_of, job_fingerprint, now_iso, posted_epoch

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
CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE, status TEXT DEFAULT 'preparing',
  created_at TEXT, updated_at TEXT, resume_path TEXT DEFAULT '', resume_note TEXT DEFAULT '',
  questions TEXT DEFAULT '[]', question_source TEXT DEFAULT '', error TEXT DEFAULT '',
  application_id INTEGER, approved_at TEXT
);
CREATE TABLE IF NOT EXISTS mail_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, message_key TEXT UNIQUE, received_at TEXT, sender TEXT,
  subject TEXT, stage TEXT, application_id INTEGER, confidence REAL DEFAULT 0, created_at TEXT,
  dismissed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS resume_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE, label TEXT, category TEXT DEFAULT 'general',
  is_default INTEGER DEFAULT 0, added_at TEXT
);
CREATE TABLE IF NOT EXISTS boards (
  ats TEXT, slug TEXT, discovered_at TEXT, last_scanned_at TEXT, last_ok_at TEXT,
  failures INTEGER DEFAULT 0, jobs_found INTEGER DEFAULT 0, PRIMARY KEY (ats, slug)
);
"""

# Preserve every historical ID/application while presenting one vacancy per ATS posting.
# An applied or review-needed record always wins over a duplicate newly discovered row.
DEDUPED_JOBS = """
WITH ranked_jobs AS (
  SELECT jobs.*, ROW_NUMBER() OVER (
    PARTITION BY COALESCE(canonical_key, url, id)
    ORDER BY CASE status WHEN 'applied' THEN 0 WHEN 'applying' THEN 1 WHEN 'queued' THEN 2
      WHEN 'needs_human' THEN 3 WHEN 'prepared' THEN 4 WHEN 'failed' THEN 5 WHEN 'skipped' THEN 6 ELSE 7 END,
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
            # posted_ts: the posting date as UTC epoch seconds, for "posted within" filters and sorting.
            # starred: NULL follows the priority-company list; 1/0 is the candidate's own choice.
            "jobs": {"canonical_key": "TEXT", "posted_ts": "REAL", "starred": "INTEGER"},
            "applications": {"submission_attempted": "INTEGER DEFAULT 0", "worker_pid": "INTEGER",
                              "candidate_note": "TEXT DEFAULT ''", "stage": "TEXT DEFAULT ''",
                              "stage_source": "TEXT DEFAULT ''", "stage_updated_at": "TEXT",
                              "questions": "TEXT DEFAULT '[]'",
                              # hidden: removed from the Applications list; history and the Tracker keep it.
                              "hidden": "INTEGER DEFAULT 0"},
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
        rows = c.execute("SELECT id, posted_at FROM jobs WHERE posted_ts IS NULL AND posted_at IS NOT NULL").fetchall()
        c.executemany("UPDATE jobs SET posted_ts=? WHERE id=?",
                      [(posted_epoch(r["posted_at"]), r["id"]) for r in rows if posted_epoch(r["posted_at"]) is not None])
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_posted ON jobs(posted_ts)")
        if c.execute("SELECT 1 FROM boards LIMIT 1").fetchone() is None:
            # One-time: learn company boards from postings found before discovery existed.
            _insert_boards(c, (board for r in c.execute("SELECT url, apply_url FROM jobs").fetchall()
                               for board in (board_of(r["apply_url"]), board_of(r["url"])) if board))
        _upgrade_settings(c)
    seed_settings()


def _upgrade_settings(c: sqlite3.Connection) -> None:
    """Bring an existing install's saved settings up to the current board catalog, once.

    Only boards new to the catalog are added, so a board the candidate removed
    stays removed. JobSpy defaults move only if they were never changed.
    """
    rows = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM settings").fetchall()}
    if not rows:
        return   # fresh install: the defaults are already current
    try:
        version = int(json.loads(rows.get("catalog_version", "1")))
    except (TypeError, ValueError):
        version = 1
    if version >= config.CATALOG_VERSION:
        return
    try:
        companies = json.loads(rows["companies"]) if "companies" in rows else None
    except json.JSONDecodeError:
        companies = None
    if isinstance(companies, dict):
        for ats, additions in config.CATALOG_ADDITIONS.items():
            current = [str(slug) for slug in companies.get(ats) or []]
            known = {slug.casefold() for slug in current}
            companies[ats] = (current + [slug for slug in additions if slug.casefold() not in known])[:500]
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('companies', ?)", (json.dumps(companies),))
    try:
        jobspy = json.loads(rows["jobspy"]) if "jobspy" in rows else None
    except json.JSONDecodeError:
        jobspy = None
    if isinstance(jobspy, dict):
        if jobspy.get("results_wanted") == 20:
            jobspy["results_wanted"] = 50
        sites = [site for site in jobspy.get("sites") or [] if site != "zip_recruiter"]
        jobspy["sites"] = sites or ["indeed"]
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('jobspy', ?)", (json.dumps(jobspy),))
    c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('catalog_version', ?)",
              (json.dumps(config.CATALOG_VERSION),))


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
                    "posted_at=COALESCE(?, posted_at), posted_ts=COALESCE(?, posted_ts), "
                    "salary_min=COALESCE(?, salary_min), salary_max=COALESCE(?, salary_max), "
                    "description=CASE WHEN length(?)>0 THEN ? ELSE description END, "
                    "ats=CASE WHEN ? IN ('greenhouse','lever','ashby') THEN ? ELSE ats END, "
                    "apply_url=CASE WHEN ? IN ('greenhouse','lever','ashby') THEN ? ELSE apply_url END, "
                    "updated_at=? WHERE id=?",
                    (j.company, j.company, j.title, j.title, j.location, j.location, j.location, j.remote, j.posted_at,
                     posted_epoch(j.posted_at), j.salary_min, j.salary_max, j.description, j.description, j.ats, j.ats,
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
            c.execute("UPDATE jobs SET canonical_key=?, posted_ts=? WHERE id=?", (key, posted_epoch(j.posted_at), j.id))
    return new


def refresh_job_metadata(job_id: str, fresh: Job) -> Job:
    """Replace public posting metadata while preserving the local vacancy record.

    Unlike discovery upserts, this accepts empty public fields as authoritative
    so stale title/description/salary data cannot survive a successful refresh.
    The existing ID, canonical key, status, discovery time, and application
    history remain attached to the same vacancy.
    """
    with conn() as c:
        row = c.execute("SELECT id, canonical_key FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("Job not found")
        key = job_fingerprint(fresh)
        if row["canonical_key"] and row["canonical_key"] != key:
            raise ValueError("Refreshed posting does not match the stored vacancy")
        c.execute(
            "UPDATE jobs SET source=?, ats=?, company=?, title=?, location=?, remote=?, "
            "salary_min=?, salary_max=?, url=?, apply_url=?, description=?, posted_at=?, posted_ts=?, raw=?, "
            "updated_at=? WHERE id=?",
            (fresh.source, fresh.ats, fresh.company, fresh.title, fresh.location, int(fresh.remote),
             fresh.salary_min, fresh.salary_max, fresh.url, fresh.apply_url, fresh.description,
             fresh.posted_at, posted_epoch(fresh.posted_at), json.dumps(fresh.raw), now_iso(), job_id),
        )
    return get_job(job_id)


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
    d.pop("posted_ts", None)
    return Job.model_validate(d)


# Sort orders for list_jobs. ``{d}`` is ASC or DESC; unknown dates and salaries always sort last.
JOB_ORDERS = {
    "match": "score {d}, discovered_at DESC",
    "posted": "posted_ts IS NULL, posted_ts {d}, score DESC",
    "found": "discovered_at {d}, score DESC",
    "salary": "COALESCE(salary_max, salary_min) IS NULL, COALESCE(salary_max, salary_min) {d}, score DESC",
    "company": "company COLLATE NOCASE {d}, score DESC",
    "title": "title COLLATE NOCASE {d}, score DESC",
    "status": "status {d}, score DESC",
}


def _posted_cutoff(hours: float | None) -> float | None:
    return time.time() - float(hours) * 3600 if hours else None


def list_jobs(status: str | None = None, q: str | None = None, limit: int = 200,
              min_score: float | None = None, ats: str | None = None,
              offset: int = 0, posted_within_hours: float | None = None,
              order: str = "match", descending: bool = True) -> list[Job]:
    """Deduplicated vacancies. ``posted_within_hours`` keeps postings published in that window."""
    sql, args = DEDUPED_JOBS + " SELECT * FROM ranked_jobs WHERE posting_rank=1", []
    if status and status != "all":
        sql += " AND status = ?"; args.append(status)
    if q:
        sql += " AND (title LIKE ? OR company LIKE ? OR location LIKE ?)"; args += [f"%{q}%"] * 3
    if min_score is not None:
        sql += " AND score >= ?"; args.append(min_score)
    if ats:
        sql += " AND ats = ?"; args.append(ats)
    cutoff = _posted_cutoff(posted_within_hours)
    if cutoff is not None:
        sql += " AND posted_ts >= ?"; args.append(cutoff)
    if order not in JOB_ORDERS:
        raise ValueError(f"Unknown job order: {order}")
    sql += " ORDER BY " + JOB_ORDERS[order].format(d="DESC" if descending else "ASC") + ", id LIMIT ? OFFSET ?"
    args.extend((limit, max(0, offset)))
    with conn() as c:
        return [_job_from_row(r) for r in c.execute(sql, args).fetchall()]


def count_jobs(status: str | None = None, q: str | None = None, posted_within_hours: float | None = None) -> int:
    """Count the same deduplicated vacancy set shown by ``list_jobs``."""
    sql, args = DEDUPED_JOBS + " SELECT COUNT(*) FROM ranked_jobs WHERE posting_rank=1", []
    if status and status != "all":
        sql += " AND status = ?"
        args.append(status)
    if q:
        sql += " AND (title LIKE ? OR company LIKE ? OR location LIKE ?)"
        args.extend([f"%{q}%"] * 3)
    cutoff = _posted_cutoff(posted_within_hours)
    if cutoff is not None:
        sql += " AND posted_ts >= ?"
        args.append(cutoff)
    with conn() as c:
        return int(c.execute(sql, args).fetchone()[0])


FRESH_WINDOWS = (24, 72, 168)


def fresh_counts() -> dict[str, int]:
    """New vacancies by posting age: ``fresh_24`` etc. for all, ``preparable_24`` etc. on supported forms."""
    now = time.time()
    columns = []
    args: list[float] = []
    for hours in FRESH_WINDOWS:
        columns.append(f"SUM(posted_ts >= ?) AS fresh_{hours}")
        args.append(now - hours * 3600)
    columns.append("SUM(ats IN ('greenhouse','lever','ashby')) AS preparable_0")
    for hours in FRESH_WINDOWS:
        columns.append(f"SUM(ats IN ('greenhouse','lever','ashby') AND posted_ts >= ?) AS preparable_{hours}")
        args.append(now - hours * 3600)
    with conn() as c:
        row = c.execute(DEDUPED_JOBS + " SELECT " + ", ".join(columns) +
                        " FROM ranked_jobs WHERE posting_rank=1 AND status='new'", args).fetchone()
    return {key: int(row[key] or 0) for key in row.keys()}


def set_job_star(job_id: str, starred: bool | None) -> None:
    """Star or unstar a vacancy (every stored duplicate of it). ``None`` returns to the priority list."""
    value = None if starred is None else int(bool(starred))
    with conn() as c:
        row = c.execute("SELECT canonical_key FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("Job not found")
        c.execute("UPDATE jobs SET starred=?, updated_at=? WHERE id=? OR (canonical_key IS NOT NULL AND canonical_key=?)",
                  (value, now_iso(), job_id, row["canonical_key"]))


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
def reserve_application(job_id: str, method: str = "engine", limit: int = 1) -> int:
    """Atomically reserve a browser slot and the posting across API calls and processes.

    ``limit`` is how many applications may be queued or running at once (the
    candidate's parallel sends). One posting never has two at a time, and a
    posting with a submission record is never reserved again.
    """
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
                "SELECT 1 FROM applications a JOIN jobs j ON j.id=a.job_id WHERE j.canonical_key=? "
                "AND a.status IN ('queued','running') LIMIT 1", (job["canonical_key"],)).fetchone():
            raise ValueError("This application is already queued or in progress")
        active = c.execute("SELECT COUNT(*) FROM applications WHERE status IN ('queued','running')").fetchone()[0]
        if active >= max(1, int(limit)):
            raise ValueError("An application is already queued or in progress" if limit <= 1 else
                             f"{active} applications are already being sent; this one waits its turn")
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
                       submission_attempted: bool = False, questions: list[dict] | None = None) -> None:
    with conn() as c:
        if status == "applied":
            row = c.execute("SELECT submission_attempted FROM applications WHERE id=?", (app_id,)).fetchone()
            if row is None:
                raise ValueError("Application reservation not found")
            if not row["submission_attempted"] and not submission_attempted:
                raise ValueError("Cannot record an applied status without a durable submission attempt")
        c.execute(
            "UPDATE applications SET status=?, finished_at=?, log=?, screenshots=?, unanswered=?, error=?, "
            "resume_path=COALESCE(?, resume_path),submission_attempted=MAX(submission_attempted,?), "
            "questions=?, stage=CASE WHEN ?='applied' AND COALESCE(stage,'')='' THEN 'applied' ELSE stage END, "
            "stage_updated_at=CASE WHEN ?='applied' AND COALESCE(stage,'')='' THEN ? ELSE stage_updated_at END "
            "WHERE id=?",
            (status, now_iso(), json.dumps(log), json.dumps(screenshots), json.dumps(unanswered), error,
             resume_path, int(submission_attempted), json.dumps(questions or []), status, status, now_iso(), app_id))
        new_status = {"applied": "applied", "dry_run": "new", "needs_human": "needs_human", "failed": "failed"}[status]
        c.execute("UPDATE jobs SET status=?,updated_at=? WHERE id=(SELECT job_id FROM applications WHERE id=?)",
                  (new_status, now_iso(), app_id))
        if status == "applied":
            _hide_superseded(c, app_id)


def _hide_superseded(c: sqlite3.Connection, app_id: int) -> int:
    """Once a posting has a confirmed application, its earlier attempts leave the Applications list.

    They stay in history (never deleted); only ``hidden`` changes.
    """
    cur = c.execute(
        "UPDATE applications SET hidden=1 WHERE id<>? AND status<>'applied' AND COALESCE(hidden,0)=0 "
        "AND status NOT IN ('queued','running') AND job_id IN (SELECT j.id FROM jobs j WHERE j.canonical_key="
        "(SELECT j2.canonical_key FROM jobs j2 JOIN applications a ON a.job_id=j2.id WHERE a.id=?) "
        "OR j.id=(SELECT job_id FROM applications WHERE id=?))", (app_id, app_id, app_id))
    return cur.rowcount


def hide_superseded_attempts() -> int:
    """Repair: hide earlier attempts for every posting that already has a confirmed application."""
    hidden = 0
    with conn() as c:
        for row in c.execute("SELECT MAX(a.id) AS id FROM applications a JOIN jobs j ON j.id=a.job_id "
                             "WHERE a.status='applied' GROUP BY COALESCE(j.canonical_key, j.id)").fetchall():
            hidden += _hide_superseded(c, row["id"])
    return hidden


_APPLICATION_SELECT = (
    "SELECT a.*, j.company, j.title, j.url, j.location, j.ats, j.canonical_key, j.posted_at, j.score, j.starred "
    "FROM applications a LEFT JOIN jobs j ON j.id = a.job_id"
)


def list_applications(limit: int = 100) -> list[dict]:
    """Newest attempts first, including ones hidden from the Applications list (``hidden`` = 1)."""
    with conn() as c:
        rows = c.execute(_APPLICATION_SELECT + " ORDER BY a.id DESC LIMIT ?", (limit,)).fetchall()
    return [_application_dict(r) for r in rows]


def _application_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in ("log", "screenshots", "unanswered", "questions"):
        try:
            d[k] = json.loads(d.get(k) or "[]")
        except (TypeError, json.JSONDecodeError):
            d[k] = []
    return d


def get_application(app_id: int) -> dict | None:
    with conn() as c:
        row = c.execute(_APPLICATION_SELECT + " WHERE a.id=?", (app_id,)).fetchone()
    return _application_dict(row) if row else None


def hide_application(app_id: int) -> None:
    """Remove an attempt from the Applications list. The record, its evidence, and the Tracker row stay."""
    with conn() as c:
        row = c.execute("SELECT status FROM applications WHERE id=?", (app_id,)).fetchone()
        if row is None:
            raise ValueError("Application record not found")
        if row["status"] in ("queued", "running"):
            raise ValueError("An application that is being sent cannot be removed")
        c.execute("UPDATE applications SET hidden=1 WHERE id=?", (app_id,))


def mark_application_sent(job_id: str) -> int:
    """Record that the candidate sent this application themselves, outside Starployment.

    Like ``add_manual_application``, this is the candidate's own record
    (``method='manual'``), not a browser submission. It moves the vacancy to
    the Tracker, closes its prepared draft, and blocks any later automatic
    attempt for the same posting. Returns the new application id.
    """
    with conn() as c:
        job = c.execute("SELECT status, canonical_key FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            raise ValueError("Job not found")
        if job["status"] in ("queued", "applying") or c.execute(
                "SELECT 1 FROM applications WHERE job_id=? AND status IN ('queued','running')", (job_id,)).fetchone():
            raise ValueError("This application is being sent right now")
        if c.execute("SELECT 1 FROM applications a JOIN jobs j ON j.id=a.job_id WHERE (a.job_id=? OR "
                     "(j.canonical_key IS NOT NULL AND j.canonical_key=?)) AND a.status='applied' LIMIT 1",
                     (job_id, job["canonical_key"])).fetchone():
            raise ValueError("This application is already recorded as sent")
        draft = c.execute("SELECT id, status, resume_path FROM drafts WHERE job_id=?", (job_id,)).fetchone()
        if draft is not None and draft["status"] == "sending":
            raise ValueError("This application is being sent right now")
        stamp = now_iso()
        cur = c.execute(
            "INSERT INTO applications(job_id, status, method, started_at, finished_at, submission_attempted, stage, "
            "stage_source, stage_updated_at, resume_path, log) VALUES (?, 'applied', 'manual', ?, ?, 1, 'applied', "
            "'manual', ?, ?, ?)",
            (job_id, stamp, stamp, stamp, draft["resume_path"] if draft is not None else "",
             json.dumps(["Marked as sent by the candidate (applied outside Starployment)"])))
        app_id = int(cur.lastrowid)
        # Earlier unfinished attempts for this vacancy are superseded by the candidate's record.
        c.execute("UPDATE applications SET hidden=1 WHERE job_id=? AND id<>?", (job_id, app_id))
        c.execute("UPDATE jobs SET status='applied', updated_at=? WHERE id=?", (stamp, job_id))
        if draft is not None:
            c.execute("UPDATE drafts SET status='sent', application_id=?, error='', updated_at=? WHERE id=?",
                      (app_id, stamp, draft["id"]))
        return app_id


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


# ---------------- prepared applications (drafts) ----------------
# A draft is pre-application work: a chosen resume plus the questions the
# employer's form is known to ask, each with a proposed answer. Nothing in a
# draft has been sent. Sending goes through scheduler.apply_one, so every
# submission still crosses the durable submit boundary above.
DRAFT_OPEN = ("preparing", "ready", "needs_answers", "approved")
DRAFT_STATUSES = DRAFT_OPEN + ("sending", "sent", "practice", "unconfirmed", "error")


def _draft_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["questions"] = json.loads(d.get("questions") or "[]")
    except (TypeError, json.JSONDecodeError):
        d["questions"] = []
    return d


_DRAFT_SELECT = (
    "SELECT d.*, j.company, j.title, j.location, j.remote, j.url, j.apply_url, j.ats, j.score, "
    "j.salary_min, j.salary_max, j.status AS job_status, j.canonical_key, j.posted_at, j.starred "
    "FROM drafts d LEFT JOIN jobs j ON j.id=d.job_id"
)


def upsert_draft(job_id: str, *, status: str, resume_path: str = "", resume_note: str = "",
                 questions: list[dict] | None = None, question_source: str = "", error: str = "") -> int:
    """Create or refresh the open draft for a job. Sent drafts are never rewritten."""
    if status not in DRAFT_STATUSES:
        raise ValueError(f"Unknown draft status: {status}")
    with conn() as c:
        job = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            raise ValueError("Job not found")
        existing = c.execute("SELECT id, status FROM drafts WHERE job_id=?", (job_id,)).fetchone()
        if existing and existing["status"] not in DRAFT_OPEN + ("error", "practice"):
            raise ValueError("This application was already sent")
        payload = (status, now_iso(), resume_path, resume_note, json.dumps(questions or []), question_source, error)
        if existing:
            c.execute("UPDATE drafts SET status=?, updated_at=?, resume_path=?, resume_note=?, questions=?, "
                      "question_source=?, error=?, approved_at=NULL WHERE id=?", (*payload, existing["id"]))
            draft_id = int(existing["id"])
        else:
            cur = c.execute("INSERT INTO drafts(status, updated_at, resume_path, resume_note, questions, "
                            "question_source, error, job_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (*payload, job_id, now_iso()))
            draft_id = int(cur.lastrowid)
        if job["status"] in ("new", "needs_human", "failed"):
            c.execute("UPDATE jobs SET status='prepared', updated_at=? WHERE id=?", (now_iso(), job_id))
    return draft_id


def get_draft(draft_id: int) -> dict | None:
    with conn() as c:
        row = c.execute(_DRAFT_SELECT + " WHERE d.id=?", (draft_id,)).fetchone()
    return _draft_dict(row) if row else None


def draft_for_job(job_id: str) -> dict | None:
    with conn() as c:
        row = c.execute(_DRAFT_SELECT + " WHERE d.job_id=?", (job_id,)).fetchone()
    return _draft_dict(row) if row else None


def list_drafts(statuses: tuple[str, ...] | None = None, limit: int = 1000) -> list[dict]:
    sql, args = _DRAFT_SELECT, []
    if statuses:
        sql += f" WHERE d.status IN ({','.join('?' * len(statuses))})"
        args.extend(statuses)
    sql += " ORDER BY COALESCE(j.score, 0) DESC, d.id LIMIT ?"
    args.append(limit)
    with conn() as c:
        return [_draft_dict(r) for r in c.execute(sql, args).fetchall()]


def update_draft(draft_id: int, **fields: Any) -> None:
    allowed = {"status", "resume_path", "resume_note", "questions", "question_source", "error",
               "application_id", "approved_at"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError("Unsupported draft fields: " + ", ".join(sorted(unknown)))
    if "status" in fields and fields["status"] not in DRAFT_STATUSES:
        raise ValueError(f"Unknown draft status: {fields['status']}")
    if "questions" in fields:
        fields["questions"] = json.dumps(fields["questions"] or [])
    if not fields:
        return
    columns = ", ".join(f"{key}=?" for key in fields)
    with conn() as c:
        cur = c.execute(f"UPDATE drafts SET {columns}, updated_at=? WHERE id=?", (*fields.values(), now_iso(), draft_id))
        if not cur.rowcount:
            raise ValueError("Draft not found")


def draft_counts() -> dict[str, int]:
    with conn() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM drafts GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def delete_draft(draft_id: int) -> None:
    """Remove an unsent draft and return its job to New."""
    with conn() as c:
        row = c.execute("SELECT job_id, status, resume_path FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise ValueError("Draft not found")
        if row["status"] in ("sending", "sent", "unconfirmed"):
            raise ValueError("A sent application cannot be removed")
        c.execute("DELETE FROM drafts WHERE id=?", (draft_id,))
        c.execute("UPDATE jobs SET status='new', updated_at=? WHERE id=? AND status='prepared'", (now_iso(), row["job_id"]))
    _remove_draft_resume(row["resume_path"])


def _remove_draft_resume(path: str | None) -> None:
    """Delete a generated draft resume, never a candidate upload or a sent snapshot."""
    if not path:
        return
    from pathlib import Path
    candidate = Path(path)
    drafts_dir = (config.TAILORED / "drafts").resolve()
    try:
        if candidate.resolve().parent == drafts_dir and candidate.is_file() and not candidate.is_symlink():
            candidate.unlink()
    except OSError:
        pass


def recover_drafts() -> None:
    """Drafts caught mid-send by a shutdown wait for the candidate, never auto-resend."""
    with conn() as c:
        for row in c.execute("SELECT d.id, d.application_id, a.submission_attempted, a.status AS app_status FROM drafts d "
                             "LEFT JOIN applications a ON a.id=d.application_id WHERE d.status='sending'").fetchall():
            if row["submission_attempted"]:
                status, error = "unconfirmed", "Sending was interrupted after Submit. Check the employer site before retrying."
            else:
                status, error = "approved", "Sending was interrupted before Submit. Send again when ready."
            c.execute("UPDATE drafts SET status=?, error=?, updated_at=? WHERE id=?", (status, error, now_iso(), row["id"]))
        c.execute("UPDATE drafts SET status='error', error='Preparation was interrupted. Prepare it again.', updated_at=? "
                  "WHERE status='preparing'", (now_iso(),))


def reset_unfinished(include_attempts: bool = True) -> dict[str, int]:
    """Clear unsent work so it can be prepared again with new resume or answers.

    Removes every draft that was not sent, and (optionally) practice runs and
    attempts that stopped before Submit. Anything that crossed the submit
    boundary, or was confirmed, is kept: those are history, not drafts.
    """
    removed_drafts = removed_attempts = 0
    resumes: list[str] = []
    with conn() as c:
        rows = c.execute("SELECT id, job_id, resume_path FROM drafts WHERE status NOT IN ('sending','sent','unconfirmed')").fetchall()
        for row in rows:
            c.execute("DELETE FROM drafts WHERE id=?", (row["id"],))
            c.execute("UPDATE jobs SET status='new', updated_at=? WHERE id=? AND status='prepared'", (now_iso(), row["job_id"]))
            resumes.append(row["resume_path"])
            removed_drafts += 1
        if include_attempts:
            attempts = c.execute(
                "SELECT a.id, a.job_id FROM applications a WHERE a.status IN ('dry_run','failed','needs_human') "
                "AND COALESCE(a.submission_attempted,0)=0").fetchall()
            for row in attempts:
                c.execute("DELETE FROM applications WHERE id=?", (row["id"],))
                removed_attempts += 1
            # Return those vacancies to New unless any record for the same
            # posting reached Submit (those must stay review-only).
            c.execute(
                "UPDATE jobs SET status='new', updated_at=? WHERE status IN ('needs_human','failed') AND NOT EXISTS ("
                "SELECT 1 FROM applications a JOIN jobs j2 ON j2.id=a.job_id WHERE j2.canonical_key=jobs.canonical_key "
                "AND (a.status IN ('applied','queued','running') OR a.submission_attempted=1))", (now_iso(),))
    for path in resumes:
        _remove_draft_resume(path)
    return {"drafts": removed_drafts, "attempts": removed_attempts}


def mark_not_received(app_id: int, reason: str) -> str:
    """Clear the submit boundary for an attempt the employer explicitly refused to receive.

    Only for a recorded, explicit rejection (for example a spam-filter message
    saying the application could not be submitted). Returns the job id.
    """
    with conn() as c:
        row = c.execute("SELECT job_id, status, submission_attempted FROM applications WHERE id=?", (app_id,)).fetchone()
        if row is None:
            raise ValueError("Application record not found")
        if row["status"] != "needs_human" or not row["submission_attempted"]:
            raise ValueError("Only an unconfirmed submission can be marked as not received")
        c.execute("UPDATE applications SET status='failed', submission_attempted=0, error=?, finished_at=? WHERE id=?",
                  (reason, now_iso(), app_id))
        c.execute("UPDATE drafts SET status='error', error=? WHERE job_id=? AND status='unconfirmed'", (reason, row["job_id"]))
        c.execute("UPDATE jobs SET status='new', updated_at=? WHERE id=? AND NOT EXISTS (SELECT 1 FROM applications a "
                  "JOIN jobs j2 ON j2.id=a.job_id WHERE j2.canonical_key=jobs.canonical_key "
                  "AND (a.status='applied' OR a.submission_attempted=1))", (now_iso(), row["job_id"]))
        return row["job_id"]


# ---------------- tracker (submitted applications and their stage) ----------------
STAGES = ("applied", "assessment", "interview", "offer", "rejected", "withdrawn")
STAGE_RANK = {"": 0, "applied": 1, "assessment": 2, "interview": 3, "offer": 4, "rejected": 5, "withdrawn": 5}


def list_tracker(limit: int = 5000) -> list[dict]:
    """One row per vacancy with a submitted (or attempted) application, newest first."""
    with conn() as c:
        rows = c.execute(
            "SELECT a.id, a.job_id, a.status, a.method, a.started_at, a.finished_at, a.submission_attempted, "
            "a.stage, a.stage_source, a.stage_updated_at, a.candidate_note, a.resume_path, "
            "j.company, j.title, j.location, j.remote, j.salary_min, j.salary_max, j.url, j.ats, j.source, "
            "j.score, j.posted_at, j.canonical_key FROM applications a JOIN jobs j ON j.id=a.job_id "
            "WHERE (a.status='applied' OR a.submission_attempted=1) "
            "AND a.id=(SELECT MAX(a2.id) FROM applications a2 JOIN jobs j3 ON j3.id=a2.job_id "
            "          WHERE j3.canonical_key=j.canonical_key AND (a2.status='applied' OR a2.submission_attempted=1)) "
            "ORDER BY COALESCE(a.finished_at, a.started_at) DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["remote"] = bool(d.get("remote"))
        d["stage"] = d.get("stage") or ("applied" if d["status"] == "applied" else "")
        out.append(d)
    return out


def set_application_stage(app_id: int, stage: str, source: str = "manual") -> bool:
    """Record a hiring stage. Email updates only move forward and never override a manual choice."""
    if stage not in STAGES:
        raise ValueError(f"Unknown stage: {stage}")
    if source not in ("manual", "email"):
        raise ValueError("Unknown stage source")
    with conn() as c:
        row = c.execute("SELECT stage, stage_source, status, submission_attempted FROM applications WHERE id=?",
                        (app_id,)).fetchone()
        if row is None:
            raise ValueError("Application record not found")
        if row["status"] != "applied" and not row["submission_attempted"]:
            raise ValueError("Only submitted applications have a hiring stage")
        current = row["stage"] or ""
        # An inbox signal can only advance a record. It never reopens an
        # offer or a closed application, and never undoes a manual choice.
        if source == "email" and (current in ("offer", "rejected", "withdrawn")
                                  or STAGE_RANK[stage] <= STAGE_RANK.get(current, 0)):
            return False
        c.execute("UPDATE applications SET stage=?, stage_source=?, stage_updated_at=? WHERE id=?",
                  (stage, source, now_iso(), app_id))
    return True


def add_manual_application(*, company: str, title: str, url: str = "", location: str = "",
                           salary_min: int | None = None, salary_max: int | None = None,
                           applied_at: str | None = None, stage: str = "applied") -> int:
    """Track an application the candidate sent outside Starployment."""
    from .models import Job
    company, title = company.strip(), title.strip()
    if not company or not title:
        raise ValueError("Company and role are required")
    if stage not in STAGES:
        raise ValueError(f"Unknown stage: {stage}")
    import hashlib
    stamp = applied_at or now_iso()
    link = url.strip() or f"manual://{hashlib.sha1(f'{company}|{title}|{stamp}'.encode()).hexdigest()[:16]}"
    job = Job(source="manual", ats="unknown", company=company, title=title, url=link, location=location.strip(),
              salary_min=salary_min, salary_max=salary_max, status="applied")
    upsert_jobs([job])
    with conn() as c:
        c.execute("UPDATE jobs SET status='applied', updated_at=? WHERE id=?", (now_iso(), job.id))
        cur = c.execute(
            "INSERT INTO applications(job_id, status, method, started_at, finished_at, submission_attempted, stage, "
            "stage_source, stage_updated_at, log) VALUES (?, 'applied', 'manual', ?, ?, 1, ?, 'manual', ?, ?)",
            (job.id, stamp, stamp, stage, now_iso(), json.dumps(["Recorded manually by the candidate"])))
        return int(cur.lastrowid)


# ---------------- inbox events ----------------
def record_mail_event(*, message_key: str, received_at: str, sender: str, subject: str, stage: str,
                      application_id: int | None, confidence: float) -> bool:
    """Store only the sender, subject, date, and classification; never message bodies."""
    with conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO mail_events(message_key, received_at, sender, subject, stage, application_id, "
            "confidence, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (message_key, received_at, sender[:320], subject[:300], stage, application_id, confidence, now_iso()))
        return bool(cur.rowcount)


def known_mail_keys() -> set[str]:
    with conn() as c:
        return {r["message_key"] for r in c.execute("SELECT message_key FROM mail_events").fetchall()}


def list_mail_events(limit: int = 200) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT m.*, j.company, j.title FROM mail_events m LEFT JOIN applications a ON a.id=m.application_id "
            "LEFT JOIN jobs j ON j.id=a.job_id WHERE m.dismissed=0 ORDER BY m.received_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------------- company boards found in other listings ----------------
# Every posting URL on a public Greenhouse, Lever, or Ashby board names that
# company's board. Scans then read the whole board, which finds roles the
# aggregator never listed. Boards rotate so each scan stays bounded.
BOARD_RETRY_AFTER = timedelta(days=7)


def _insert_boards(c: sqlite3.Connection, boards) -> int:
    before = c.total_changes
    c.executemany("INSERT OR IGNORE INTO boards(ats, slug, discovered_at) VALUES (?, ?, ?)",
                  [(ats, slug, now_iso()) for ats, slug in set(boards)])
    return c.total_changes - before


def discover_boards(jobs: list[Job]) -> int:
    """Remember the boards behind these postings. Returns how many boards are new."""
    found = {board for job in jobs for board in (board_of(job.apply_url), board_of(job.url)) if board}
    if not found:
        return 0
    with conn() as c:
        return _insert_boards(c, found)


def boards_for_scan(ats: str, exclude: set[str], limit: int) -> list[str]:
    """Discovered boards to read this scan, least recently scanned first.

    A board that failed three times in a row rests for a week before retrying.
    """
    retry_before = (datetime.now(timezone.utc) - BOARD_RETRY_AFTER).isoformat(timespec="seconds")
    excluded = {slug.casefold() for slug in exclude}
    with conn() as c:
        rows = c.execute(
            "SELECT slug FROM boards WHERE ats=? AND (failures < 3 OR last_scanned_at < ?) "
            "ORDER BY last_scanned_at IS NOT NULL, last_scanned_at, slug LIMIT ?",
            (ats, retry_before, limit + len(excluded))).fetchall()
    return [r["slug"] for r in rows if r["slug"].casefold() not in excluded][:limit]


def record_board_scans(ats: str, results: dict[str, int | None]) -> None:
    """``results`` maps a board to its relevant job count, or None when it could not be read."""
    stamp = now_iso()
    with conn() as c:
        for slug, found in results.items():
            if found is None:
                c.execute("UPDATE boards SET last_scanned_at=?, failures=failures+1 WHERE ats=? AND slug=?",
                          (stamp, ats, slug))
            else:
                c.execute("UPDATE boards SET last_scanned_at=?, last_ok_at=?, failures=0, jobs_found=? "
                          "WHERE ats=? AND slug=?", (stamp, stamp, int(found), ats, slug))


def board_counts() -> dict[str, int]:
    """Discovered boards that are still being read, per job-board provider."""
    with conn() as c:
        rows = c.execute("SELECT ats, COUNT(*) n FROM boards WHERE failures < 3 GROUP BY ats").fetchall()
    return {r["ats"]: r["n"] for r in rows}


# ---------------- resume files (role-specific uploads) ----------------
def add_resume_file(path: str, label: str, category: str = "general") -> int:
    with conn() as c:
        first = c.execute("SELECT COUNT(*) FROM resume_files").fetchone()[0] == 0
        cur = c.execute("INSERT OR REPLACE INTO resume_files(path, label, category, is_default, added_at) "
                        "VALUES (?,?,?,?,?)", (path, label, category, int(first), now_iso()))
        return int(cur.lastrowid)


def list_resume_files() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM resume_files ORDER BY is_default DESC, category, label").fetchall()
    return [dict(r) for r in rows]


def update_resume_file(file_id: int, *, category: str | None = None, label: str | None = None,
                       is_default: bool | None = None) -> None:
    with conn() as c:
        if c.execute("SELECT 1 FROM resume_files WHERE id=?", (file_id,)).fetchone() is None:
            raise ValueError("Resume file not found")
        if category is not None:
            c.execute("UPDATE resume_files SET category=? WHERE id=?", (category, file_id))
        if label is not None:
            c.execute("UPDATE resume_files SET label=? WHERE id=?", (label.strip() or "Resume", file_id))
        if is_default:
            c.execute("UPDATE resume_files SET is_default=CASE WHEN id=? THEN 1 ELSE 0 END", (file_id,))


def remove_resume_file(file_id: int) -> str | None:
    with conn() as c:
        row = c.execute("SELECT path, is_default FROM resume_files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            return None
        c.execute("DELETE FROM resume_files WHERE id=?", (file_id,))
        if row["is_default"]:
            c.execute("UPDATE resume_files SET is_default=1 WHERE id=(SELECT MIN(id) FROM resume_files)")
        return row["path"]
