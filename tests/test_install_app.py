"""The /Applications installer must refuse stale bundles and never cut off an application."""
from __future__ import annotations

import importlib.util
import os
import sqlite3
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "jobhub_install_app", Path(__file__).resolve().parents[1] / "packaging" / "install_app.py"
)
assert SPEC is not None and SPEC.loader is not None
install_app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install_app)


def _touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    os.utime(path, (mtime, mtime))


def test_stale_sources_lists_only_bundled_files_newer_than_the_build(tmp_path):
    bundle = tmp_path / "dist" / "JobHub.app"
    _touch(install_app.executable(bundle), 1_000)
    _touch(tmp_path / "jobhub" / "desktop" / "ui.py", 900)
    assert install_app.stale_sources(bundle, tmp_path) == []
    _touch(tmp_path / "jobhub" / "desktop" / "widgets.py", 2_000)
    _touch(tmp_path / "jobhub" / "__pycache__" / "ui.cpython-312.pyc", 2_000)
    _touch(tmp_path / "packaging" / "install_app.py", 2_000)  # not part of the bundle
    _touch(tmp_path / "AGENTS.md", 2_000)
    assert install_app.stale_sources(bundle, tmp_path) == [Path("jobhub/desktop/widgets.py")]


def test_in_flight_applications_reads_the_live_database_read_only(tmp_path):
    database = tmp_path / "jobhub.db"
    assert install_app.in_flight_applications(database) == 0
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE applications (id INTEGER PRIMARY KEY, status TEXT)")
        connection.executemany("INSERT INTO applications(status) VALUES (?)",
                               [("applied",), ("running",), ("queued",), ("needs_human",)])
    assert install_app.in_flight_applications(database) == 2
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE applications SET status='applied'")
    assert install_app.in_flight_applications(database) == 0


def test_busy_reports_running_scans_and_applications(tmp_path):
    database = tmp_path / "jobhub.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE applications (id INTEGER PRIMARY KEY, status TEXT)")
        connection.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, status TEXT)")
        connection.execute("INSERT INTO runs(status) VALUES ('ok')")
    assert install_app.busy(database) == ""
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO runs(status) VALUES ('running')")
        connection.execute("INSERT INTO applications(status) VALUES ('running')")
    assert install_app.busy(database) == "1 application(s) and a job scan"
