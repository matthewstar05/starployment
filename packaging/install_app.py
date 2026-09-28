"""Install the freshly built bundle into /Applications and relaunch it (macOS).

    .venv/bin/python packaging/install_app.py            # install dist/JobHub.app
    .venv/bin/python packaging/install_app.py --build    # build + smoke test first

The owner runs JobHub from /Applications, so every iteration that changes the
app ends with this script (see AGENTS.md, "Definition of done"). It:

1. refuses a bundle that is older than the sources that go into it;
2. stages a signed copy next to the installed app with ``ditto``;
3. waits for any in-flight application or scan in the live database, then
   asks the running app to quit through Apple Events (a clean shutdown);
4. swaps the staged copy into place, keeping the previous app until the new
   one verifies, so a failed install restores the old app; and
5. relaunches JobHub if it was running before.

Candidate data lives in ~/Library/Application Support/JobHub and is never
touched; the database is opened read-only to check for in-flight work.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ID = "local.jobhub.desktop"
DEFAULT_TARGET = Path("/Applications/JobHub.app")
DEFAULT_DATA = Path.home() / "Library" / "Application Support" / "JobHub"
# Everything PyInstaller copies into the bundle. If any of it is newer than
# the built executable, the bundle does not contain the latest changes.
BUNDLED_SOURCES = ("jobhub", "web", "packaging/assets", "packaging/hooks",
                   "packaging/jobhub.spec", "packaging/runtime_hook.py", "pyproject.toml", "uv.lock")


def executable(bundle: Path) -> Path:
    return bundle / "Contents" / "MacOS" / "JobHub"


def stale_sources(bundle: Path, root: Path = ROOT) -> list[Path]:
    """Source files modified after ``bundle`` was built (empty means up to date)."""
    built = executable(bundle).stat().st_mtime
    newer = []
    for name in BUNDLED_SOURCES:
        path = root / name
        files = [path] if path.is_file() else path.rglob("*") if path.is_dir() else []
        for item in files:
            if item.is_file() and "__pycache__" not in item.parts and item.name != ".DS_Store":
                if item.stat().st_mtime > built + 1:
                    newer.append(item.relative_to(root))
    return sorted(newer)


def _count(database: Path, sql: str) -> int:
    if not database.is_file():
        return 0
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5) as connection:
            row = connection.execute(sql).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] if row else 0)


def in_flight_applications(database: Path) -> int:
    """Applications the live app is queuing or running right now (read-only)."""
    return _count(database, "SELECT COUNT(*) FROM applications WHERE status IN ('queued', 'running')")


def active_scans(database: Path) -> int:
    """Source scans still running in the live app (read-only)."""
    return _count(database, "SELECT COUNT(*) FROM runs WHERE status = 'running'")


def busy(database: Path) -> str:
    """Describe unfinished work the installer should not cut off, or return ''."""
    applications, scans = in_flight_applications(database), active_scans(database)
    parts = [f"{applications} application(s)" if applications else "", "a job scan" if scans else ""]
    return " and ".join(part for part in parts if part)


def running_pids(bundle: Path) -> list[int]:
    result = subprocess.run(["pgrep", "-f", str(executable(bundle))], capture_output=True, text=True)
    return [int(pid) for pid in result.stdout.split()]


def _wait_for_exit(bundle: Path, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not running_pids(bundle):
            return True
        time.sleep(0.5)
    return not running_pids(bundle)


def quit_app(bundle: Path, timeout: float) -> bool:
    """Quit JobHub cleanly; return True once no process remains.

    First ask like ⌘Q would (an Apple Event). If macOS has not granted
    Automation permission for that, send SIGTERM, which JobHub also handles
    as a clean quit. Call this only when no application or scan is running.
    """
    try:
        result = subprocess.run(["osascript", "-e", f'tell application id "{BUNDLE_ID}" to quit'],
                                capture_output=True, text=True, timeout=30)
        asked = result.returncode == 0
    except subprocess.TimeoutExpired:
        asked = False
    if asked and _wait_for_exit(bundle, timeout):
        return True
    for pid in running_pids(bundle):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return _wait_for_exit(bundle, timeout)


def verify(bundle: Path) -> None:
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(bundle)], check=True)


def run(*command: str) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install dist/JobHub.app into /Applications and relaunch it.")
    parser.add_argument("--build", action="store_true", help="run build_desktop.py and smoke_bundle.py first")
    parser.add_argument("--source", type=Path, default=ROOT / "dist" / "JobHub.app")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA, help="live data directory to check for in-flight work")
    parser.add_argument("--wait", type=float, default=600, help="seconds to wait for an in-flight application (default 600)")
    parser.add_argument("--no-launch", action="store_true", help="do not relaunch, even if JobHub was running")
    parser.add_argument("--launch", action="store_true", help="launch after installing, even if JobHub was not running")
    parser.add_argument("--allow-stale", action="store_true", help="install even if sources changed after the build")
    args = parser.parse_args(argv)

    if sys.platform != "darwin":
        raise SystemExit("install_app.py installs the macOS bundle; on other systems use the dist/ build directly.")
    python = sys.executable
    if args.build:
        run(python, "packaging/build_desktop.py")
        run(python, "packaging/smoke_bundle.py")
    source, target = args.source.resolve(), args.target
    if not executable(source).is_file():
        raise SystemExit(f"No built bundle at {source}. Run with --build.")
    stale = stale_sources(source)
    if stale and not args.allow_stale:
        listed = "\n  ".join(str(path) for path in stale[:12])
        raise SystemExit(f"{source.name} is older than its sources; rebuild with --build. Newer files:\n  {listed}")
    verify(source)

    staging = target.with_name(".JobHub-staging.app")
    previous = target.with_name(".JobHub-previous.app")
    for leftover in (staging, previous):
        if leftover.exists():
            shutil.rmtree(leftover)
    print(f"Staging {source} → {staging}", flush=True)
    subprocess.run(["ditto", str(source), str(staging)], check=True)
    verify(staging)

    was_running = bool(running_pids(target))
    if was_running:
        database = args.data_dir / "jobhub.db"
        deadline = time.time() + args.wait
        while (work := busy(database)) and time.time() < deadline:
            print(f"Waiting for {work} to finish…", flush=True)
            time.sleep(10)
        if work := busy(database):
            shutil.rmtree(staging)
            raise SystemExit(f"JobHub is still running {work}; not interrupting it. Try again later.")
        print("Quitting the running JobHub…", flush=True)
        if not quit_app(target, timeout=45):
            shutil.rmtree(staging)
            raise SystemExit("JobHub did not quit within 45 seconds; the installed app was left unchanged.")

    if target.exists():
        target.rename(previous)
    try:
        staging.rename(target)
        verify(target)
    except Exception:
        if target.exists():
            shutil.rmtree(target)
        if previous.exists():
            previous.rename(target)
        raise
    if previous.exists():
        shutil.rmtree(previous)
    print(f"Installed {target}", flush=True)

    if (was_running or args.launch) and not args.no_launch:
        subprocess.run(["open", str(target)], check=True)
        print("Relaunched JobHub", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
