"""One-time transfer from a checkout beside a packaged desktop build.

This is deliberately limited to a bundle built inside its own checkout. An
installed copy elsewhere starts with an empty, candidate-owned setup instead.
Browser sessions and environment files are never transferred.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .models import Profile

ARTIFACT_DIRS = ("resumes", "tailored", "screenshots")
MARKER = ".checkout-import.json"


def bundled_checkout_data(executable: Path | None = None) -> Path | None:
    """Find `data/` only when the executable is inside this checkout's dist/."""
    exe = (executable or Path(sys.executable)).resolve()
    for root in exe.parents:
        if root.name != "dist":
            continue
        checkout = root.parent
        if (checkout / "pyproject.toml").is_file() and (checkout / "jobhub" / "setup.py").is_file():
            source = checkout / "data"
            if (source / "jobhub.db").is_file():
                return source.resolve()
    return None


def _has_user_state(db_path: Path) -> bool:
    if not db_path.is_file():
        return False
    with closing(sqlite3.connect(db_path)) as con:
        for table in ("jobs", "applications"):
            try:
                if con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                    return True
            except sqlite3.Error:
                return True
        try:
            row = con.execute("SELECT data FROM profile WHERE id=1").fetchone()
        except sqlite3.Error:
            return True
        if row:
            try:
                profile = Profile.model_validate_json(row[0])
            except Exception:
                return True
            if any((profile.first_name, profile.last_name, profile.email, profile.resume_path)):
                return True
    return False


def _valid_source(source: Path) -> bool:
    """Validate candidate setup and its PDF before replacing local state."""
    from .config import DEFAULT_SETTINGS, merge_settings, validate_settings

    try:
        with closing(sqlite3.connect(source / "jobhub.db")) as con:
            row = con.execute("SELECT data FROM profile WHERE id=1").fetchone()
            if not row:
                return False
            profile = Profile.model_validate_json(row[0])
            settings = merge_settings(DEFAULT_SETTINGS, {})
            for key, value in con.execute("SELECT key, value FROM settings"):
                if key in DEFAULT_SETTINGS:
                    settings = merge_settings(settings, {key: json.loads(value)})
            validate_settings(settings)
        pdf = Path(profile.resume_path).resolve()
        if not pdf.is_relative_to((source / "resumes").resolve()) or not pdf.is_file():
            return False
        with pdf.open("rb") as document:
            return document.read(5) == b"%PDF-"
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return False


def _copy_artifacts(source: Path, destination: Path) -> None:
    for directory in ARTIFACT_DIRS:
        origin = source / directory
        if not origin.is_dir():
            continue
        for path in origin.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(origin)
            target = destination / directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            config.secure_file(target)


def _relocate_paths(database: Path, source: Path, destination: Path) -> None:
    old = str(source.resolve())
    new = str(destination.resolve())
    with closing(sqlite3.connect(database)) as con:
        row = con.execute("SELECT data FROM profile WHERE id=1").fetchone()
        if row:
            data = json.loads(row[0])
            if isinstance(data.get("resume_path"), str) and data["resume_path"].startswith(old + os.sep):
                data["resume_path"] = new + data["resume_path"][len(old):]
            con.execute("UPDATE profile SET data=? WHERE id=1", (json.dumps(data),))
        for key in ("resume_path", "screenshots"):
            columns = {r[1] for r in con.execute("PRAGMA table_info(applications)")}
            if key not in columns:
                continue
            for app_id, value in con.execute(f"SELECT id, {key} FROM applications WHERE {key} IS NOT NULL"):
                if not value or old not in value:
                    continue
                rewritten = value.replace(old + os.sep, new + os.sep)
                con.execute(f"UPDATE applications SET {key}=? WHERE id=?", (rewritten, app_id))
        con.commit()


def migrate_source_checkout(source_data: Path, target_data: Path | None = None) -> bool:
    """Copy a complete local snapshot once, leaving existing candidate state intact.

    Uses SQLite's online backup API for both source and destination, so WAL
    writes are included. Caller should invoke this before opening the target DB.
    """
    source = Path(source_data).resolve()
    target = (target_data or config.DATA).resolve()
    if source == target or not _valid_source(source):
        return False
    target.mkdir(parents=True, exist_ok=True)
    marker = target / MARKER
    database = target / "jobhub.db"
    if marker.exists() or _has_user_state(database):
        return False

    # Preserve a partially initialized destination before replacing it.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if database.is_file():
        backup = target / f"jobhub-before-import-{timestamp}.db"
        with closing(sqlite3.connect(database)) as original, closing(sqlite3.connect(backup)) as saved:
            original.backup(saved)

    temporary = target / f".jobhub-import-{os.getpid()}.db"
    try:
        with closing(sqlite3.connect(source / "jobhub.db")) as original, closing(sqlite3.connect(temporary)) as copied:
            original.backup(copied)
        _relocate_paths(temporary, source, target)
        _copy_artifacts(source, target)
        # Backup into the destination instead of replacing its inode. A prior
        # first-run window may have left SQLite WAL sidecars at that path.
        with closing(sqlite3.connect(temporary)) as copied, closing(sqlite3.connect(database)) as installed:
            copied.backup(installed)
        marker.write_text(json.dumps({"source": str(source), "imported_at": timestamp}) + "\n")
        config.secure_data_tree(target)
    finally:
        temporary.unlink(missing_ok=True)
    return True
