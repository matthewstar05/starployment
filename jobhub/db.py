"""SQLite persistence. Single-file DB in data/jobhub.db, WAL mode, dict rows."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
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
CREATE TABLE IF NOT EXISTS mail_seen (
  message_key TEXT PRIMARY KEY, rules INTEGER, seen_at TEXT
);
-- Postings JARVIS left for the candidate, with the draft's updated_at then, so it is not retried every cycle.
CREATE TABLE IF NOT EXISTS jarvis_holds (
  job_id TEXT PRIMARY KEY, reason TEXT, held_at REAL, until REAL, draft_updated TEXT DEFAULT ''
);
-- Luna's reading of an email the rules could not settle (hashed key, stage, employer), so it is asked once.
CREATE TABLE IF NOT EXISTS mail_luna (
  message_key TEXT PRIMARY KEY, stage TEXT, company TEXT, read_at TEXT
);
-- What each employer's Workday Candidate Home last said about an application, so a check
-- reports only changes. Status text and flags only; nothing the candidate entered.
CREATE TABLE IF NOT EXISTS workday_portal (
  application_id INTEGER PRIMARY KEY, tenant TEXT, status TEXT DEFAULT '', draft INTEGER DEFAULT 0,
  tasks INTEGER DEFAULT 0, stage TEXT DEFAULT '', checked_at TEXT, changed_at TEXT
);
-- When each employer's Candidate Home was last read, and how it went.
CREATE TABLE IF NOT EXISTS workday_portal_checks (tenant TEXT PRIMARY KEY, checked_at TEXT, outcome TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS resume_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE, label TEXT, category TEXT DEFAULT 'general',
  is_default INTEGER DEFAULT 0, added_at TEXT, enabled INTEGER DEFAULT 1, removed INTEGER DEFAULT 0
);
-- Bumped by every change to jobs, so the desktop can skip re-reading an unchanged library.
CREATE TABLE IF NOT EXISTS jobs_version (id INTEGER PRIMARY KEY CHECK (id = 1), n INTEGER);
INSERT OR IGNORE INTO jobs_version(id, n) VALUES (1, 0);
CREATE TRIGGER IF NOT EXISTS jobs_version_insert AFTER INSERT ON jobs BEGIN UPDATE jobs_version SET n = n + 1 WHERE id = 1; END;
CREATE TRIGGER IF NOT EXISTS jobs_version_update AFTER UPDATE ON jobs BEGIN UPDATE jobs_version SET n = n + 1 WHERE id = 1; END;
CREATE TRIGGER IF NOT EXISTS jobs_version_delete AFTER DELETE ON jobs BEGIN UPDATE jobs_version SET n = n + 1 WHERE id = 1; END;
CREATE TABLE IF NOT EXISTS boards (
  ats TEXT, slug TEXT, discovered_at TEXT, last_scanned_at TEXT, last_ok_at TEXT,
  failures INTEGER DEFAULT 0, jobs_found INTEGER DEFAULT 0, PRIMARY KEY (ats, slug)
);
-- When a scan last found each posting still listed (UTC seconds). Kept out of jobs so
-- marking thousands of postings seen does not bump jobs_version or rewrite job rows.
CREATE TABLE IF NOT EXISTS job_seen (id TEXT PRIMARY KEY, seen_at REAL);
-- Change counters for the desktop refresh: unchanged tables are not read again.
CREATE TABLE IF NOT EXISTS table_versions (name TEXT PRIMARY KEY, n INTEGER);
INSERT OR IGNORE INTO table_versions(name, n) VALUES ('applications', 0), ('drafts', 0), ('mail_events', 0);
CREATE TRIGGER IF NOT EXISTS applications_version_insert AFTER INSERT ON applications BEGIN UPDATE table_versions SET n = n + 1 WHERE name = 'applications'; END;
CREATE TRIGGER IF NOT EXISTS applications_version_update AFTER UPDATE ON applications BEGIN UPDATE table_versions SET n = n + 1 WHERE name = 'applications'; END;
CREATE TRIGGER IF NOT EXISTS applications_version_delete AFTER DELETE ON applications BEGIN UPDATE table_versions SET n = n + 1 WHERE name = 'applications'; END;
CREATE TRIGGER IF NOT EXISTS drafts_version_insert AFTER INSERT ON drafts BEGIN UPDATE table_versions SET n = n + 1 WHERE name = 'drafts'; END;
CREATE TRIGGER IF NOT EXISTS drafts_version_update AFTER UPDATE ON drafts BEGIN UPDATE table_versions SET n = n + 1 WHERE name = 'drafts'; END;
CREATE TRIGGER IF NOT EXISTS drafts_version_delete AFTER DELETE ON drafts BEGIN UPDATE table_versions SET n = n + 1 WHERE name = 'drafts'; END;
CREATE TRIGGER IF NOT EXISTS mail_events_version_insert AFTER INSERT ON mail_events BEGIN UPDATE table_versions SET n = n + 1 WHERE name = 'mail_events'; END;
CREATE TRIGGER IF NOT EXISTS mail_events_version_update AFTER UPDATE ON mail_events BEGIN UPDATE table_versions SET n = n + 1 WHERE name = 'mail_events'; END;
"""

# Preserve every historical ID/application while presenting one vacancy per ATS posting.
# An applied or review-needed record always wins over a duplicate newly discovered row.
# The window ranks ids only: sorting whole rows (descriptions, raw JSON) made every
# refresh query take hundreds of milliseconds on a large library.
DEDUPED_JOBS = """
WITH job_ranks AS (
  SELECT id AS rank_id, ROW_NUMBER() OVER (
    PARTITION BY COALESCE(canonical_key, url, id)
    ORDER BY CASE status WHEN 'applied' THEN 0 WHEN 'applying' THEN 1 WHEN 'queued' THEN 2
      WHEN 'needs_human' THEN 3 WHEN 'prepared' THEN 4 WHEN 'failed' THEN 5 WHEN 'skipped' THEN 6 ELSE 7 END,
      score DESC, discovered_at DESC, id
  ) AS posting_rank FROM jobs
),
ranked_jobs AS (
  SELECT jobs.*, job_ranks.posting_rank FROM job_ranks JOIN jobs ON jobs.id = job_ranks.rank_id
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


_reader_con: sqlite3.Connection | None = None
_reader_path = None
_reader_lock = threading.RLock()


@contextmanager
def reader() -> Iterator[sqlite3.Connection]:
    """A connection for read-only queries.

    On the main (Qt) thread this is a second connection: in WAL mode readers never
    wait for writers, so a scan's upsert or a rescore on a worker thread cannot
    stall the window behind the shared connection's lock. Other threads share
    ``conn()``. Never use it for writes, or for reads that must see the caller's
    own uncommitted changes.
    """
    global _reader_con, _reader_path
    if threading.current_thread() is not threading.main_thread():
        with conn() as c:
            yield c
        return
    with _reader_lock:
        if _reader_con is not None and _reader_path != config.DB_PATH:
            _reader_con.close()
            _reader_con = None
        if _reader_con is None:
            if _con is None or _con_path != config.DB_PATH:
                with conn():
                    pass   # creates the file and schema (only before anything else opened it)
            _reader_con = _connect()
            _reader_path = config.DB_PATH
        yield _reader_con


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
            "resume_files": {"enabled": "INTEGER DEFAULT 1", "removed": "INTEGER DEFAULT 0"},
            # message_id: the email's Message-ID header, so the candidate can open that email.
            "mail_events": {"message_id": "TEXT DEFAULT ''",
                            # employer: who the email is from, for listing one no application matches.
                            "employer": "TEXT DEFAULT ''"},
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
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at)")
        # Listing columns without descriptions: deduplication, filters, and sorts
        # read this instead of walking every long description.
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_listing ON jobs(status, score, discovered_at, id, canonical_key, "
                  "url, posted_ts, ats, company, title, location, starred, salary_min, salary_max)")
        if c.execute("SELECT 1 FROM boards WHERE ats='workday' LIMIT 1").fetchone() is None:
            # One-time: Workday boards behind postings found before Workday boards were read.
            _insert_boards(c, (board for r in c.execute("SELECT url, apply_url FROM jobs WHERE ats='workday'").fetchall()
                               for board in (board_of(r["apply_url"]), board_of(r["url"])) if board))
        if c.execute("SELECT 1 FROM boards LIMIT 1").fetchone() is None:
            # One-time: learn company boards from postings found before discovery existed.
            _insert_boards(c, (board for r in c.execute("SELECT url, apply_url FROM jobs").fetchall()
                               for board in (board_of(r["apply_url"]), board_of(r["url"])) if board))
        # Postings stored before liveness tracking: board postings count as seen at upgrade
        # time (the next read of their board settles it); aggregator postings, which cannot
        # be checked one by one, start from the latest date known for them.
        c.execute("INSERT OR IGNORE INTO job_seen(id, seen_at) SELECT id, CASE WHEN ats IN "
                  "('greenhouse','lever','ashby','workday','workable','rippling') THEN ? ELSE "
                  "MAX(COALESCE(CAST(strftime('%s', discovered_at) AS REAL), 0), "
                  "COALESCE(CAST(strftime('%s', updated_at) AS REAL), 0), COALESCE(posted_ts, 0)) END FROM jobs",
                  (time.time(),))
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
    global _con, _con_path, _reader_con, _reader_path
    with _lock:
        if _con is not None:
            _con.close()
        _con = _con_path = None
    with _reader_lock:
        if _reader_con is not None:
            _reader_con.close()
        _reader_con = _reader_path = None


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


# ---------------- settings ----------------
def seed_settings() -> None:
    with conn() as c:
        for k, v in config.DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(v)))


def get_settings() -> dict[str, Any]:
    with reader() as c:
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
    with reader() as c:
        r = c.execute("SELECT data FROM profile WHERE id = 1").fetchone()
    return Profile.model_validate_json(r["data"]) if r else None


# ---------------- jobs ----------------
def upsert_jobs(jobs: list[Job]) -> int:
    """Insert new jobs; refresh mutable fields of known ones. Returns count of new rows."""
    from .locations import is_location_acceptable
    new = 0
    with conn() as c:
        for j in jobs:
            if not is_location_acceptable(j.location):
                continue
            key = job_fingerprint(j)
            exists = c.execute(
                "SELECT id, company, title, location, remote, posted_at, posted_ts, salary_min, salary_max, "
                "description, ats, apply_url FROM jobs WHERE id = ? OR url = ? OR canonical_key = ? LIMIT 1",
                (j.id, j.url, key)).fetchone()
            if exists and c.execute("SELECT 1 FROM applications WHERE job_id=? AND (status='applied' "
                                    "OR submission_attempted=1) LIMIT 1", (exists["id"],)).fetchone():
                # Once an application went out, the posting keeps the employer and title it was sent
                # under. Two sources name some postings differently ("Netic" and "Cybernetic Labs"),
                # and taking whichever was read last renamed sent applications on every scan.
                j.company, j.title = exists["company"] or j.company, exists["title"] or j.title
            if exists and not _upsert_changes(exists, j):
                # Unchanged: rewriting the row (and its long description) on every scan
                # held the database for seconds and grew the write-ahead log.
                j.id = exists["id"]
                continue
            if exists:
                c.execute(
                    "UPDATE jobs SET company=CASE WHEN length(?)>0 THEN ? ELSE company END, "
                    "title=CASE WHEN length(?)>0 THEN ? ELSE title END, "
                    "location=CASE WHEN length(?)>0 THEN ? ELSE location END, remote=CASE WHEN length(?)>0 THEN ? ELSE remote END, "
                    "posted_at=COALESCE(?, posted_at), posted_ts=COALESCE(?, posted_ts), "
                    "salary_min=COALESCE(?, salary_min), salary_max=COALESCE(?, salary_max), "
                    "description=CASE WHEN length(?)>0 THEN ? ELSE description END, "
                    "ats=CASE WHEN ? IN ('greenhouse','lever','ashby','workable','rippling','workday') THEN ? ELSE ats END, "
                    "apply_url=CASE WHEN ? IN ('greenhouse','lever','ashby','workable','rippling','workday') THEN ? ELSE apply_url END, "
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
            # Just found, so listed now (scans refresh this for every posting they find).
            c.execute("INSERT OR REPLACE INTO job_seen(id, seen_at) VALUES (?, ?)", (j.id, time.time()))
    return new


def _upsert_changes(row: sqlite3.Row, j: Job) -> bool:
    """Whether the discovery UPDATE in ``upsert_jobs`` would change any stored field."""
    supported = j.ats in ('greenhouse', 'lever', 'ashby', 'workable', 'rippling', 'workday')
    merged = {
        "company": j.company if j.company else row["company"],
        "title": j.title if j.title else row["title"],
        "location": j.location if j.location else row["location"],
        "remote": int(j.remote) if j.location else row["remote"],
        "posted_at": j.posted_at if j.posted_at is not None else row["posted_at"],
        "posted_ts": posted_epoch(j.posted_at) if posted_epoch(j.posted_at) is not None else row["posted_ts"],
        "salary_min": j.salary_min if j.salary_min is not None else row["salary_min"],
        "salary_max": j.salary_max if j.salary_max is not None else row["salary_max"],
        "description": j.description if j.description else row["description"],
        "ats": j.ats if supported else row["ats"],
        "apply_url": j.apply_url if supported else row["apply_url"],
    }
    return any(value != row[name] for name, value in merged.items())


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
            # Keys saved before job_fingerprint knew a board (Workable, Rippling, a careers page
            # linked to Greenhouse) are plain URLs. The stored posting itself still identifies the
            # vacancy, so compare its current fingerprint; a real mismatch still stops here.
            stored = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job_fingerprint(_job_from_row(stored)) != key:
                raise ValueError("Refreshed posting does not match the stored vacancy")
        c.execute(
            "UPDATE jobs SET source=?, ats=?, company=?, title=?, location=?, remote=?, "
            "salary_min=?, salary_max=?, url=?, apply_url=?, description=?, posted_at=?, posted_ts=?, raw=?, "
            "updated_at=?, canonical_key=? WHERE id=?",
            (fresh.source, fresh.ats, fresh.company, fresh.title, fresh.location, int(fresh.remote),
             fresh.salary_min, fresh.salary_max, fresh.url, fresh.apply_url, fresh.description,
             fresh.posted_at, posted_epoch(fresh.posted_at), json.dumps(fresh.raw), now_iso(), key, job_id),
        )
    return get_job(job_id)


def jobs_changed_since(stamp: str) -> list[Job]:
    """Postings inserted or changed (``updated_at``) at or after ``stamp``, for incremental scoring."""
    with reader() as c:
        rows = c.execute("SELECT * FROM jobs WHERE updated_at >= ?", (stamp,)).fetchall()
    return [_job_from_row(r) for r in rows]


def hold_for_jarvis(job_id: str, reason: str, seconds: float) -> bool:
    """Set a posting aside for the candidate for ``seconds``. Returns True when it was not
    already set aside for the same reason (so the activity log says it once, not every cycle)."""
    import time as _time
    now = _time.time()
    with conn() as c:
        draft = c.execute("SELECT updated_at FROM drafts WHERE job_id=?", (job_id,)).fetchone()
        stamp = (draft["updated_at"] or "") if draft else ""
        before = c.execute("SELECT reason FROM jarvis_holds WHERE job_id=?", (job_id,)).fetchone()
        c.execute("INSERT INTO jarvis_holds(job_id, reason, held_at, until, draft_updated) VALUES (?,?,?,?,?) "
                  "ON CONFLICT(job_id) DO UPDATE SET reason=excluded.reason, held_at=excluded.held_at, "
                  "until=excluded.until, draft_updated=excluded.draft_updated",
                  (job_id, reason[:300], now, now + seconds, stamp))
    return before is None or before["reason"] != reason[:300]


def jarvis_holds(job_ids) -> dict[str, tuple[float, str]]:
    """(set aside until, draft updated_at then) for the given jobs JARVIS set aside."""
    ids = list(job_ids)
    found: dict[str, tuple[float, str]] = {}
    with reader() as c:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            found.update((r[0], (r[1] or 0.0, r[2] or "")) for r in c.execute(
                f"SELECT job_id, until, draft_updated FROM jarvis_holds WHERE job_id IN ({','.join('?' * len(chunk))})",
                chunk))
    return found


def workday_jobs_at(host: str) -> list[str]:
    """Workday postings at one employer's address (both of Workday's URL forms)."""
    host = (host or "").strip().casefold()
    tenant = host.split(".")[0] if host else ""
    if not tenant:
        return []
    with reader() as c:
        return [r[0] for r in c.execute(
            "SELECT id FROM jobs WHERE ats='workday' AND (lower(apply_url) LIKE ? OR lower(url) LIKE ? OR "
            "lower(apply_url) LIKE ?)", (f"%//{host}/%", f"%//{host}/%", f"%/recruiting/{tenant}/%"))]


def release_workday_holds(host: str) -> int:
    """Let JARVIS try an employer's Workday postings again (the candidate fixed its sign-in)."""
    host = (host or "").strip().casefold()
    if not host:
        return 0
    with conn() as c:
        cur = c.execute("DELETE FROM jarvis_holds WHERE job_id IN (SELECT id FROM jobs WHERE ats='workday' AND "
                        "(lower(apply_url) LIKE ? OR lower(url) LIKE ?))", (f"%//{host}/%", f"%//{host}/%"))
        return cur.rowcount


def drafts_left_for_owner(job_ids) -> set[str]:
    """Jobs whose draft JARVIS must leave alone: one already sent (it has an application), or one
    that errored, is still preparing, or stopped mid-send. JARVIS skips these, so a cycle drops them
    before it ranks postings instead of spending its probe window on them every time."""
    ids = list(job_ids)
    found: set[str] = set()
    with reader() as c:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            found.update(r[0] for r in c.execute(
                f"SELECT job_id FROM drafts WHERE job_id IN ({','.join('?' * len(chunk))}) AND "
                "(application_id IS NOT NULL OR status NOT IN ('ready', 'needs_answers', 'approved'))", chunk))
    return found


def draft_stamps(job_ids) -> dict[str, str]:
    """Each draft's updated_at by job, so a set-aside posting returns once the candidate edits it."""
    ids = list(job_ids)
    found: dict[str, str] = {}
    with reader() as c:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            found.update((r[0], r[1] or "") for r in c.execute(
                f"SELECT job_id, updated_at FROM drafts WHERE job_id IN ({','.join('?' * len(chunk))})", chunk))
    return found


def automation_history() -> tuple[set[tuple[str, str]], set[str]]:
    """(roles already applied to, employers in an active process), for ``jarvis.not_repeated``.

    Roles are (normalized company, ``jarvis.role_key``) of every attempt that reached the
    employer or was sent by hand; employers are those with an application at assessment,
    interview, or offer."""
    from .jarvis import role_key
    from .priority import normalize_company
    roles: set[tuple[str, str]] = set()
    engaged: set[str] = set()
    with reader() as c:
        for row in c.execute("SELECT j.company, j.title, j.url, a.stage, a.status, a.submission_attempted, "
                             "a.method FROM applications a JOIN jobs j ON j.id=a.job_id").fetchall():
            company = normalize_company(row["company"] or "")
            if (row["submission_attempted"] or row["status"] == "applied" or row["method"] == "manual"):
                roles.add((company, role_key(row["title"] or "")))
            if (row["stage"] or "") in ("assessment", "interview", "offer"):
                engaged.add(company)
                board = board_of(row["url"] or "")
                if board:   # the employer's own job board, whatever name a source gives it
                    engaged.add(f"board:{board[0]}:{board[1]}")
    return roles, engaged


def recent_company_attempts(days: float) -> dict[str, int]:
    """Attempts that reached an employer (submitted or confirmed) in the last ``days`` days,
    by normalized company name (see priority.normalize_company)."""
    from .priority import normalize_company
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    counts: dict[str, int] = {}
    with reader() as c:
        for row in c.execute("SELECT j.company FROM applications a JOIN jobs j ON j.id=a.job_id "
                             "WHERE (a.submission_attempted=1 OR a.status='applied') AND a.started_at >= ?", (since,)):
            name = normalize_company(row[0] or "")
            if name:
                counts[name] = counts.get(name, 0) + 1
    return counts


def update_scores(scores: dict[str, tuple[float, dict]]) -> None:
    with conn() as c:
        for jid, (score, detail) in scores.items():
            detail_json = json.dumps(detail)
            c.execute("UPDATE jobs SET score=?, score_json=? WHERE id=? AND (score IS NOT ? OR score_json IS NOT ?)",
                      (score, detail_json, jid, score, detail_json))


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


def _register_star_filter(c: sqlite3.Connection) -> None:
    """Use the same effective stars as the UI, before SQL counts and pagination."""
    from .priority import is_starred

    priority = tuple(get_settings().get("priority_companies") or [])

    @lru_cache(maxsize=4096)
    def starred(company, value):
        return bool(is_starred({"company": company, "starred": value}, priority))

    c.create_function("job_is_starred", 2, starred, deterministic=True)


def list_jobs(status: str | None = None, q: str | None = None, limit: int = 200,
              min_score: float | None = None, ats: str | None = None,
              offset: int = 0, posted_within_hours: float | None = None,
              order: str = "match", descending: bool = True, exclude_starred: bool = False) -> list[Job]:
    """Deduplicated vacancies. ``posted_within_hours`` keeps postings published in that window."""
    # Sort and page ids only, then read the page's full rows: sorting whole rows is slow.
    sql, args = DEDUPED_JOBS + " SELECT id FROM ranked_jobs WHERE posting_rank=1", []
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
    if exclude_starred:
        sql += " AND NOT job_is_starred(company, starred)"
    if order not in JOB_ORDERS:
        raise ValueError(f"Unknown job order: {order}")
    sql += " ORDER BY " + JOB_ORDERS[order].format(d="DESC" if descending else "ASC") + ", id LIMIT ? OFFSET ?"
    args.extend((limit, max(0, offset)))
    with reader() as c:
        if exclude_starred:
            _register_star_filter(c)
        ids = [r[0] for r in c.execute(sql, args).fetchall()]
        if not ids:
            return []
        rows = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            rows.update((r["id"], r) for r in c.execute(
                f"SELECT * FROM jobs WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall())
    return [_job_from_row(rows[job_id]) for job_id in ids if job_id in rows]


def count_jobs(status: str | None = None, q: str | None = None, posted_within_hours: float | None = None,
               *, exclude_starred: bool = False) -> int:
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
    if exclude_starred:
        sql += " AND NOT job_is_starred(company, starred)"
    with reader() as c:
        if exclude_starred:
            _register_star_filter(c)
        return int(c.execute(sql, args).fetchone()[0])


FRESH_WINDOWS = (24, 72, 168)


def fresh_counts(*, exclude_starred: bool = False) -> dict[str, int]:
    """New vacancies by posting age: ``fresh_24`` etc. for all, ``preparable_24`` etc. on supported forms."""
    now = time.time()
    columns = []
    args: list[float] = []
    for hours in FRESH_WINDOWS:
        columns.append(f"SUM(posted_ts >= ?) AS fresh_{hours}")
        args.append(now - hours * 3600)
    columns.append("SUM(ats IN ('greenhouse','lever','ashby','workable','rippling','workday')) AS preparable_0")
    for hours in FRESH_WINDOWS:
        columns.append(f"SUM(ats IN ('greenhouse','lever','ashby','workable','rippling','workday') AND posted_ts >= ?) AS preparable_{hours}")
        args.append(now - hours * 3600)
    with reader() as c:
        if exclude_starred:
            _register_star_filter(c)
        star_filter = " AND NOT job_is_starred(company, starred)" if exclude_starred else ""
        row = c.execute(DEDUPED_JOBS + " SELECT " + ", ".join(columns) +
                        " FROM ranked_jobs WHERE posting_rank=1 AND status='new'" + star_filter, args).fetchone()
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
    with reader() as c:
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


def jobs_version() -> int:
    """Changes whenever any job row is inserted, updated, or deleted (by any process)."""
    with reader() as c:
        row = c.execute("SELECT n FROM jobs_version WHERE id=1").fetchone()
    return int(row[0]) if row else 0


def table_version(name: str) -> int:
    """Change counter for ``applications``, ``drafts``, or ``mail_events`` (any process)."""
    with reader() as c:
        row = c.execute("SELECT n FROM table_versions WHERE name=?", (name,)).fetchone()
    return int(row[0]) if row else 0


# ---------------- liveness and pruning ----------------
# A posting nobody has touched (status New, no star of the candidate's own, no draft,
# no application) can be deleted safely: if it is still listed, the next scan adds it back.
_UNTOUCHED = ("status='new' AND starred IS NULL "
              "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id=jobs.id) "
              "AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.job_id=jobs.id)")
# Board feeds that list every open posting (Workday is read a page window at a time, so
# a posting missing from it may simply be older than the window).
FULL_LISTING_ATS = ("greenhouse", "lever", "ashby")


def mark_seen(job_ids, at: float | None = None) -> None:
    """Record that a scan found these postings still listed."""
    stamp = time.time() if at is None else at
    ids = list(dict.fromkeys(i for i in job_ids if i))
    if not ids:
        return
    with conn() as c:
        c.executemany("INSERT INTO job_seen(id, seen_at) VALUES (?, ?) "
                      "ON CONFLICT(id) DO UPDATE SET seen_at=excluded.seen_at", [(i, stamp) for i in ids])


def last_seen(job_ids) -> dict[str, float]:
    ids = list(job_ids)
    found: dict[str, float] = {}
    with reader() as c:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            found.update((r[0], r[1]) for r in c.execute(
                f"SELECT id, seen_at FROM job_seen WHERE id IN ({','.join('?' * len(chunk))})", chunk))
    return found


def prune_jobs(settings: dict, *, boards_read: dict[str, set[str]] | None = None,
               scan_started: float | None = None) -> dict[str, int]:
    """Delete untouched postings that are no longer hiring.

    ``closed``: its Greenhouse, Lever, or Ashby board was read successfully in this
    scan (``boards_read`` maps ats to slugs) and no longer lists it, while its title
    still matches the candidate's targets (otherwise the scan would not have looked
    for it). ``stale``: no scan has found it for ``prune.unseen_days`` days, which
    covers aggregators whose postings cannot be checked one by one.
    """
    from .sources.base import title_relevant
    policy = settings.get("prune") or {}
    if not policy.get("enabled", True):
        return {"closed": 0, "stale": 0}
    closed: list[str] = []
    wanted = {ats: {slug.casefold() for slug in slugs} for ats, slugs in (boards_read or {}).items() if slugs}
    with reader() as c:
        if wanted and scan_started is not None:
            rows = c.execute(
                "SELECT jobs.id, jobs.ats, jobs.url, jobs.apply_url, jobs.title, job_seen.seen_at FROM jobs "
                "LEFT JOIN job_seen ON job_seen.id = jobs.id WHERE " + _UNTOUCHED + " AND jobs.ats IN "
                f"({','.join('?' * len(FULL_LISTING_ATS))}) AND COALESCE(job_seen.seen_at, 0) < ?",
                (*FULL_LISTING_ATS, scan_started)).fetchall()
            for row in rows:
                boards = {board_of(row["apply_url"]), board_of(row["url"])} - {None}
                if any(ats == row["ats"] and slug.casefold() in wanted.get(ats, ()) for ats, slug in boards) \
                        and title_relevant(row["title"] or "", settings):
                    closed.append(row["id"])
    cutoff = time.time() - float(policy.get("unseen_days", 21)) * 86400
    with conn() as c:
        for start in range(0, len(closed), 500):
            chunk = closed[start:start + 500]
            c.execute(f"DELETE FROM jobs WHERE id IN ({','.join('?' * len(chunk))}) AND " + _UNTOUCHED, chunk)
        stale = c.execute("DELETE FROM jobs WHERE " + _UNTOUCHED + " AND id IN "
                          "(SELECT id FROM job_seen WHERE seen_at < ?)", (cutoff,)).rowcount
        c.execute("DELETE FROM job_seen WHERE id NOT IN (SELECT id FROM jobs)")
    return {"closed": len(closed), "stale": max(0, stale)}


def compact(min_free_ratio: float = 0.25) -> bool:
    """Fold the write-ahead log back into the database, and reclaim space after a large prune.

    A long WAL makes every read slower. VACUUM rewrites the file, so it runs only
    when at least ``min_free_ratio`` of the pages are free.
    """
    with conn() as c:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        pages = c.execute("PRAGMA page_count").fetchone()[0]
        free = c.execute("PRAGMA freelist_count").fetchone()[0]
        if not pages or free / pages < min_free_ratio:
            return False
        c.commit()
        c.execute("VACUUM")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return True


def job_stats() -> dict[str, int]:
    with reader() as c:
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
    from .platform_support import process_alive as alive

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


def hide_closed_posting_attempts() -> int:
    """Repair: hide automatic attempts that stopped before anything was sent because the posting
    was closed (404/410) or no longer met the automatic criteria, and skip their jobs.

    Newer runs skip these at the source (``scheduler.PostingGone``); earlier runs left them as
    "needs your review" with nothing the candidate could do."""
    hidden = 0
    with conn() as c:
        rows = c.execute(
            "SELECT a.id, a.job_id, a.error, a.log FROM applications a WHERE a.submission_attempted=0 AND a.hidden=0 "
            "AND ((a.status='failed' AND a.error LIKE 'The employer has closed this posting%') OR a.status='needs_human') "
            "AND (a.error LIKE 'The employer has closed this posting%' OR a.error LIKE '%no longer meets your automatic%' "
            "OR (a.error LIKE 'Could not verify the current public posting%' "
            "    AND (a.log LIKE '%404 Not Found%' OR a.log LIKE '%410 Gone%' "
            "         OR a.log LIKE '%Public posting was not found%')))").fetchall()
        for row in rows:
            c.execute("UPDATE applications SET hidden=1 WHERE id=?", (row["id"],))
            c.execute("UPDATE jobs SET status='skipped', updated_at=? WHERE id=? AND status IN ('needs_human', 'failed')",
                      (now_iso(), row["job_id"]))
            hidden += 1
    return hidden


# The newest inbox email that set an application's current stage (an assessment, an interview).
_STAGE_MAIL = ("(SELECT m.{column} FROM mail_events m WHERE m.application_id=a.id AND m.stage=a.stage "
               "AND m.dismissed=0 ORDER BY m.received_at DESC LIMIT 1)")
_APPLICATION_SELECT = (
    "SELECT a.*, j.company, j.title, j.url, j.location, j.ats, j.canonical_key, j.posted_at, j.score, j.starred, "
    + ", ".join(f"{_STAGE_MAIL.format(column=column)} AS stage_mail_{column}"
                for column in ("message_id", "sender", "subject", "received_at"))
    + " FROM applications a LEFT JOIN jobs j ON j.id = a.job_id"
)


def list_applications(limit: int = 100) -> list[dict]:
    """Newest attempts first, including ones hidden from the Applications list (``hidden`` = 1)."""
    with reader() as c:
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
    with reader() as c:
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
    with reader() as c:
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
    with reader() as c:
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
    with reader() as c:
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
    with reader() as c:
        row = c.execute(_DRAFT_SELECT + " WHERE d.id=?", (draft_id,)).fetchone()
    return _draft_dict(row) if row else None


def draft_for_job(job_id: str) -> dict | None:
    with reader() as c:
        row = c.execute(_DRAFT_SELECT + " WHERE d.job_id=?", (job_id,)).fetchone()
    return _draft_dict(row) if row else None


def list_drafts(statuses: tuple[str, ...] | None = None, limit: int = 1000) -> list[dict]:
    sql, args = _DRAFT_SELECT, []
    if statuses:
        sql += f" WHERE d.status IN ({','.join('?' * len(statuses))})"
        args.extend(statuses)
    sql += " ORDER BY COALESCE(j.score, 0) DESC, d.id LIMIT ?"
    args.append(limit)
    with reader() as c:
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
    with reader() as c:
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
        # Past Submit without a confirmation: it may have reached the employer.
        kept = c.execute("SELECT COUNT(DISTINCT job_id) FROM applications WHERE status='needs_human' "
                         "AND submission_attempted=1 AND COALESCE(hidden,0)=0").fetchone()[0]
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
    return {"drafts": removed_drafts, "attempts": removed_attempts, "kept": kept}


def reopen_for_browser(app_id: int) -> str:
    """Queue an unconfirmed attempt again so the candidate can finish it in a visible browser.

    Only on the candidate's request. The record keeps ``submission_attempted``;
    Starployment never presses Submit in that window, the candidate does.
    Returns the job id.
    """
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT a.job_id, a.status, j.canonical_key FROM applications a JOIN jobs j ON j.id=a.job_id "
                        "WHERE a.id=?", (app_id,)).fetchone()
        if row is None:
            raise ValueError("Application record not found")
        if row["status"] != "needs_human":
            raise ValueError("Only an application that needs your review can be finished in the browser")
        other = c.execute(
            "SELECT 1 FROM applications a JOIN jobs j ON j.id=a.job_id WHERE j.canonical_key=? AND a.id<>? "
            "AND a.status IN ('applied','queued','running') LIMIT 1", (row["canonical_key"], app_id)).fetchone()
        if other:
            raise ValueError("This posting already has a confirmed or running application")
        c.execute("UPDATE applications SET status='queued', worker_pid=NULL, hidden=0 WHERE id=?", (app_id,))
        return row["job_id"]


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
    with reader() as c:
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


def open_jobs_at(company: str) -> list[str]:
    """New or prepared postings under this employer name."""
    with reader() as c:
        return [r["id"] for r in c.execute("SELECT id FROM jobs WHERE lower(company)=lower(?) AND status IN "
                                           "('new','prepared')", (company,)).fetchall()]


def workday_applications() -> list[dict]:
    """The latest visible attempt at each Workday vacancy that reached the submit boundary."""
    with reader() as c:
        rows = c.execute(
            "SELECT a.id, a.job_id, a.status, a.stage, a.stage_source, a.started_at, a.finished_at, "
            "j.company, j.title, j.url, j.apply_url FROM applications a JOIN jobs j ON j.id=a.job_id "
            "WHERE j.ats='workday' AND COALESCE(a.hidden,0)=0 AND (a.status='applied' OR a.submission_attempted=1 "
            "OR a.status IN ('queued','running')) "
            "AND a.id=(SELECT MAX(a2.id) FROM applications a2 WHERE a2.job_id=a.job_id) ORDER BY a.id").fetchall()
    return [dict(row) for row in rows]


def portal_checked(tenant: str) -> str:
    with reader() as c:
        row = c.execute("SELECT checked_at FROM workday_portal_checks WHERE tenant=?", (tenant,)).fetchone()
    return row["checked_at"] if row else ""


def record_portal_check(tenant: str, outcome: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO workday_portal_checks(tenant, checked_at, outcome) VALUES (?,?,?) "
                  "ON CONFLICT(tenant) DO UPDATE SET checked_at=excluded.checked_at, outcome=excluded.outcome",
                  (tenant, now_iso(), outcome[:300]))


def portal_state(app_id: int) -> dict | None:
    with reader() as c:
        row = c.execute("SELECT * FROM workday_portal WHERE application_id=?", (app_id,)).fetchone()
    return dict(row) if row else None


def save_portal_state(app_id: int, tenant: str, status: str, draft: bool, tasks: bool, stage: str) -> bool:
    """Store what Candidate Home shows. Returns True when it differs from the last reading."""
    stamp = now_iso()
    with conn() as c:
        row = c.execute("SELECT status, draft, tasks FROM workday_portal WHERE application_id=?", (app_id,)).fetchone()
        changed = row is None or (row["status"], bool(row["draft"]), bool(row["tasks"])) != (status, draft, tasks)
        c.execute("INSERT INTO workday_portal(application_id, tenant, status, draft, tasks, stage, checked_at, "
                  "changed_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(application_id) DO UPDATE SET "
                  "status=excluded.status, draft=excluded.draft, tasks=excluded.tasks, stage=excluded.stage, "
                  "checked_at=excluded.checked_at, changed_at=CASE WHEN ? THEN excluded.changed_at "
                  "ELSE workday_portal.changed_at END",
                  (app_id, tenant, status[:120], int(draft), int(tasks), stage, stamp, stamp, int(changed)))
    return changed


def confirm_from_email(app_id: int, subject: str, received_at: str) -> bool:
    """An unconfirmed attempt the employer acknowledged by email after it was sent.

    "Thank you for applying" (or any later step) from that employer, received after the
    attempt started, is the employer's own confirmation; the attempt becomes Applied. Same
    guard as ``confirm_from_portal``: only an attempt past the submit boundary that is not running."""
    with conn() as c:
        row = c.execute("SELECT started_at FROM applications WHERE id=?", (app_id,)).fetchone()
    if row is None or not row["started_at"] or (received_at or "") < row["started_at"]:
        return False
    return confirm_from_portal(app_id, f"email: {subject[:60]}", source="email")


def confirm_acknowledged_attempts() -> list[dict]:
    """Unconfirmed attempts whose employer email, already recorded, acknowledges them."""
    with reader() as c:
        rows = c.execute(
            "SELECT a.id, a.job_id, j.company, j.title, m.subject, m.received_at FROM applications a "
            "JOIN jobs j ON j.id=a.job_id JOIN mail_events m ON m.application_id=a.id AND m.dismissed=0 "
            "WHERE a.status='needs_human' AND a.submission_attempted=1 AND m.received_at >= a.started_at "
            "AND m.stage IN ('applied','assessment','interview','offer','rejected') GROUP BY a.id").fetchall()
    return [dict(row) for row in rows if confirm_from_email(int(row["id"]), row["subject"] or "", row["received_at"])]


def confirm_from_portal(app_id: int, status: str, source: str = "workday") -> bool:
    """An unconfirmed Workday attempt that the employer's own Candidate Home lists as submitted.

    That listing is the employer's record of a received application, the same evidence as a
    confirmation page, so the attempt becomes Applied. Only an attempt that passed the submit
    boundary and is not running is changed; returns whether it was."""
    stamp = now_iso()
    with conn() as c:
        row = c.execute("SELECT job_id, status, submission_attempted, log FROM applications WHERE id=?",
                        (app_id,)).fetchone()
        if row is None or row["status"] != "needs_human" or not row["submission_attempted"]:
            return False
        try:
            log = json.loads(row["log"] or "[]")
        except ValueError:
            log = []
        log.append(f"The employer confirmed it by {status[:80]}." if source == "email" else
                   f"Workday's Candidate Home lists this application as submitted ({status[:80]}).")
        c.execute("UPDATE applications SET status='applied', error='', finished_at=COALESCE(finished_at, ?), "
                  "log=?, stage=CASE WHEN COALESCE(stage,'')='' THEN 'applied' ELSE stage END, "
                  "stage_source=CASE WHEN COALESCE(stage_source,'')='' THEN ? ELSE stage_source END, "
                  "stage_updated_at=? WHERE id=?", (stamp, json.dumps(log), source, stamp, app_id))
        c.execute("UPDATE jobs SET status='applied', updated_at=? WHERE id=?", (stamp, row["job_id"]))
        c.execute("UPDATE drafts SET status='sent', application_id=?, error='', updated_at=? WHERE job_id=? "
                  "AND status IN ('unconfirmed','error','sending')", (app_id, stamp, row["job_id"]))
    return True


def tracked_application(app_id: int) -> dict | None:
    """One application as ``list_tracker`` describes it, even when a newer attempt at the same
    vacancy is the one the Tracker shows."""
    with reader() as c:
        row = c.execute(
            "SELECT a.id, a.job_id, a.status, a.started_at, a.finished_at, a.stage, a.stage_source, "
            "j.company, j.title, j.url, j.ats FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.id=?",
            (app_id,)).fetchone()
    if row is None:
        return None
    found = dict(row)
    found["stage"] = found.get("stage") or ("applied" if found["status"] == "applied" else "")
    return found


def set_application_stage(app_id: int, stage: str, source: str = "manual") -> bool:
    """Record a hiring stage. Email updates only move forward and never override a manual choice."""
    if stage not in STAGES:
        raise ValueError(f"Unknown stage: {stage}")
    if source not in ("manual", "email", "workday"):
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
        # An employer's Workday Candidate Home is read the same way: forward only, never over
        # the candidate's own choice.
        if source in ("email", "workday") and (current in ("offer", "rejected", "withdrawn")
                                               or STAGE_RANK[stage] <= STAGE_RANK.get(current, 0)):
            return False
        c.execute("UPDATE applications SET stage=?, stage_source=?, stage_updated_at=? WHERE id=?",
                  (stage, source, now_iso(), app_id))
    return True


def restage_from_mail(app_id: int) -> str | None:
    """Re-derive a stage that email set, after one of its messages was re-read differently.

    The stage becomes the furthest one its remaining mail shows (at least
    "applied") and is returned when it changed. A stage the candidate chose is never touched."""
    with conn() as c:
        row = c.execute("SELECT stage, stage_source FROM applications WHERE id=?", (app_id,)).fetchone()
        if row is None or row["stage_source"] != "email":
            return None
        stages = [r["stage"] for r in c.execute(
            "SELECT stage FROM mail_events WHERE application_id=? AND dismissed=0", (app_id,)).fetchall()
            if r["stage"] in STAGE_RANK]
        stage = max(stages + ["applied"], key=lambda value: STAGE_RANK[value])
        if stage == (row["stage"] or ""):
            return None
        c.execute("UPDATE applications SET stage=?, stage_updated_at=? WHERE id=?", (stage, now_iso(), app_id))
    return stage


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
                      application_id: int | None, confidence: float, message_id: str = "", employer: str = "") -> bool:
    """Store only the sender, subject, date, Message-ID, and classification; never message bodies.

    A message read again with a better classification or match replaces its row
    (keeping the candidate's dismissal)."""
    with conn() as c:
        cur = c.execute(
            "INSERT INTO mail_events(message_key, received_at, sender, subject, stage, application_id, "
            "confidence, created_at, message_id, employer) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(message_key) "
            "DO UPDATE SET stage=excluded.stage, application_id=excluded.application_id, "
            "confidence=excluded.confidence, employer=excluded.employer, "
            "message_id=COALESCE(NULLIF(excluded.message_id, ''), mail_events.message_id) "
            "WHERE mail_events.stage IS NOT excluded.stage OR mail_events.application_id IS NOT excluded.application_id "
            "OR COALESCE(mail_events.employer, '') != excluded.employer",
            (message_key, received_at, sender[:320], subject[:300], stage, application_id, confidence, now_iso(),
             message_id[:500], employer[:120]))
        return bool(cur.rowcount)


def set_mail_message_id(message_key: str, message_id: str) -> None:
    """Fill the Message-ID of an email recorded before it was kept."""
    with conn() as c:
        c.execute("UPDATE mail_events SET message_id=? WHERE message_key=? AND COALESCE(message_id,'')=''",
                  (message_id[:500], message_key))


def mail_keys_without_id() -> set[str]:
    with conn() as c:
        return {r["message_key"] for r in
                c.execute("SELECT message_key FROM mail_events WHERE COALESCE(message_id,'')=''").fetchall()}


def seen_mail_keys(rules: int) -> set[str]:
    """Messages already read under these classifier rules and found not to be about an application."""
    with conn() as c:
        return {r["message_key"] for r in
                c.execute("SELECT message_key FROM mail_seen WHERE rules=?", (rules,)).fetchall()}


def mark_mail_seen(keys: list[str], rules: int) -> None:
    """Remember hashed message keys only; nothing about the message itself."""
    if not keys:
        return
    stamp = now_iso()
    with conn() as c:
        c.executemany("INSERT INTO mail_seen(message_key, rules, seen_at) VALUES (?,?,?) ON CONFLICT(message_key) "
                      "DO UPDATE SET rules=excluded.rules, seen_at=excluded.seen_at",
                      [(key, rules, stamp) for key in keys])


def prune_mail_seen(rules: int, before: str) -> None:
    """Forget keys from older rules or older than the lookback window."""
    with conn() as c:
        c.execute("DELETE FROM mail_seen WHERE rules IS NOT ? OR seen_at < ?", (rules, before))
        c.execute("DELETE FROM mail_luna WHERE read_at < ?", (before,))


def dismiss_mail_event(message_key: str) -> int | None:
    """Set aside a recorded email that turned out not to be about an application.
    Returns the application it was linked to, so its stage can be recomputed."""
    with conn() as c:
        row = c.execute("SELECT application_id FROM mail_events WHERE message_key=? AND dismissed=0",
                        (message_key,)).fetchone()
        if row is None:
            return None
        c.execute("UPDATE mail_events SET dismissed=1 WHERE message_key=?", (message_key,))
        return row["application_id"]


def mail_luna_readings() -> dict[str, tuple[str | None, str]]:
    """Luna's earlier readings by message key: (stage or None, employer)."""
    with conn() as c:
        return {r["message_key"]: (r["stage"] or None, r["company"] or "")
                for r in c.execute("SELECT message_key, stage, company FROM mail_luna").fetchall()}


def save_mail_luna_readings(readings: list[tuple[str, str, str]]) -> None:
    """Keep (message key, stage, employer) so Luna never reads the same email twice."""
    if not readings:
        return
    stamp = now_iso()
    with conn() as c:
        c.executemany("INSERT INTO mail_luna(message_key, stage, company, read_at) VALUES (?,?,?,?) "
                      "ON CONFLICT(message_key) DO UPDATE SET stage=excluded.stage, company=excluded.company",
                      [(key, stage, company[:120], stamp) for key, stage, company in readings])


def unmatched_stage_mail(days: int = 30) -> list[dict]:
    """Assessments, interviews, and offers in recent mail that no tracked application matched."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with reader() as c:
        rows = c.execute(
            "SELECT id, received_at, sender, subject, stage, message_id, employer FROM mail_events "
            "WHERE application_id IS NULL AND dismissed=0 AND stage IN ('assessment', 'interview', 'offer') "
            "AND received_at >= ? ORDER BY received_at DESC", (since,)).fetchall()
    return [dict(r) for r in rows]


def known_mail_keys() -> dict[str, tuple[str, int | None]]:
    """Each processed message's key with the stage and application it was recorded with."""
    with conn() as c:
        return {r["message_key"]: (r["stage"], r["application_id"])
                for r in c.execute("SELECT message_key, stage, application_id FROM mail_events").fetchall()}


def list_mail_events(limit: int = 200) -> list[dict]:
    with reader() as c:
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
    with reader() as c:
        rows = c.execute("SELECT ats, COUNT(*) n FROM boards WHERE failures < 3 GROUP BY ats").fetchall()
    return {r["ats"]: r["n"] for r in rows}


# ---------------- resume files (role-specific uploads) ----------------
def add_resume_file(path: str, label: str, category: str = "general") -> int:
    with conn() as c:
        first = c.execute("SELECT COUNT(*) FROM resume_files").fetchone()[0] == 0
        cur = c.execute("INSERT OR REPLACE INTO resume_files(path, label, category, is_default, added_at) "
                        "VALUES (?,?,?,?,?)", (path, label, category, int(first), now_iso()))
        return int(cur.lastrowid)


def _same_path(p1: str, p2: str) -> bool:
    if not p1 or not p2:
        return False
    if p1 == p2:
        return True
    try:
        return Path(p1).resolve() == Path(p2).resolve()
    except Exception:
        return False


def list_resume_files(*, include_removed: bool = False) -> list[dict]:
    with conn() as c:
        profile_row = c.execute("SELECT data FROM profile WHERE id = 1").fetchone()
        main_path = ""
        if profile_row:
            try:
                prof = Profile.model_validate_json(profile_row["data"])
                main_path = prof.resume_path or ""
            except Exception:
                main_path = ""
        if main_path:
            try:
                if Path(main_path).is_file():
                    existing = c.execute("SELECT 1 FROM resume_files WHERE path=?", (main_path,)).fetchone()
                    if not existing:
                        stem = Path(main_path).stem.replace("_", " ").replace("-", " ").strip()
                        label = stem.title() if stem and stem.lower() not in ("main", "resume") else "Main Resume"
                        first = c.execute("SELECT COUNT(*) FROM resume_files").fetchone()[0] == 0
                        c.execute("INSERT INTO resume_files(path, label, category, is_default, added_at, enabled) "
                                  "VALUES (?,?,?,?,?,1)", (main_path, label, "general", int(first), now_iso()))
            except Exception:
                pass
        rows = c.execute("SELECT * FROM resume_files " + ("" if include_removed else "WHERE removed=0 ") +
                         "ORDER BY is_default DESC, category, label").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["is_main"] = _same_path(str(d.get("path") or ""), main_path)
            result.append(d)
        return result


def update_resume_file(file_id: int, *, category: str | None = None, label: str | None = None,
                       is_default: bool | None = None, enabled: bool | int | None = None) -> None:
    if label is not None and not label.strip():
        raise ValueError("Enter a name for the resume")
    with conn() as c:
        if c.execute("SELECT 1 FROM resume_files WHERE id=?", (file_id,)).fetchone() is None:
            raise ValueError("Resume file not found")
        if category is not None:
            c.execute("UPDATE resume_files SET category=? WHERE id=?", (category, file_id))
        if label is not None:
            c.execute("UPDATE resume_files SET label=? WHERE id=?", (label.strip(), file_id))
        if is_default:
            c.execute("UPDATE resume_files SET is_default=CASE WHEN id=? THEN 1 ELSE 0 END", (file_id,))
        if enabled is not None:
            c.execute("UPDATE resume_files SET enabled=? WHERE id=?", (int(bool(enabled)), file_id))


def set_main_resume(file_id: int) -> str:
    """Find the resume file row, update Profile's resume_path to that file's path, and return the path."""
    with conn() as c:
        row = c.execute("SELECT path FROM resume_files WHERE id=? AND removed=0", (file_id,)).fetchone()
        if row is None:
            raise ValueError("Resume file not found")
        path = str(row["path"])
        if not Path(path).is_file():
            raise ValueError("Resume file is missing. Add the PDF again before making it the main resume.")
        r = c.execute("SELECT data FROM profile WHERE id = 1").fetchone()
        profile = Profile.model_validate_json(r["data"]) if r else Profile()
        profile.resume_path = path
        c.execute("INSERT OR REPLACE INTO profile(id, data, updated_at) VALUES (1, ?, ?)",
                  (profile.model_dump_json(), now_iso()))
        return path


def remove_resume_file(file_id: int) -> str | None:
    with conn() as c:
        row = c.execute("SELECT path, is_default FROM resume_files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            return None
        c.execute("UPDATE resume_files SET removed=1, enabled=0, is_default=0 WHERE id=?", (file_id,))
        if row["is_default"]:
            c.execute("UPDATE resume_files SET is_default=1 WHERE id=(SELECT MIN(id) FROM resume_files WHERE removed=0)")
        return row["path"]
