"""Build a Starployment desktop bundle on the current operating system.

PyInstaller builds are platform-specific. Run this script on each target OS
with that OS's Python interpreter. It intentionally does not copy user data,
credentials, resumes, or browser profiles into the application bundle.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
BROWSER_DIR = ROOT / "packaging" / "playwright-browsers"


def run(*args: str, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, env=env, check=True)


def frozen_runtime_root(platform: str = sys.platform, root: Path = ROOT) -> Path:
    """Return the directory exposed as ``sys._MEIPASS`` by PyInstaller."""
    if platform == "darwin":
        return root / "dist" / "Starployment.app" / "Contents" / "Frameworks"
    return root / "dist" / "Starployment" / "_internal"


def install_browser_bundle() -> Path:
    """Copy Playwright's browser tree after PyInstaller has built the app."""
    runtime_root = frozen_runtime_root()
    if not runtime_root.is_dir():
        raise RuntimeError(f"PyInstaller runtime directory is missing: {runtime_root}")
    destination = runtime_root / "playwright-browsers"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(BROWSER_DIR, destination, symlinks=True, ignore=shutil.ignore_patterns(".links"))

    # The Playwright hook normally places this Node driver in the CArchive.
    # Keep it beside the frozen package for the same macOS nested-binary
    # reason as Chromium above.
    playwright_package = Path(__import__("playwright").__file__).resolve().parent
    driver_destination = runtime_root / "playwright" / "driver"
    if driver_destination.exists():
        shutil.rmtree(driver_destination)
    driver_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(playwright_package / "driver", driver_destination, symlinks=True)
    if sys.platform == "darwin":
        app_bundle = ROOT / "dist" / "Starployment.app"
        run("/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app_bundle))
    return destination


def main() -> int:
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Starployment packaging requires Python 3.12, matching pyproject.toml.")
    entrypoint = ROOT / "jobhub" / "desktop" / "__main__.py"
    if not entrypoint.is_file():
        raise SystemExit("jobhub/desktop/__main__.py is missing. Add the native entrypoint before packaging.")
    try:
        import PyInstaller  # noqa: F401
        import playwright  # noqa: F401
    except ImportError as error:
        raise SystemExit("Install the desktop build dependencies first: PyInstaller, PySide6, and Playwright.") from error

    BROWSER_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSER_DIR)
    icon_env = env.copy()
    icon_env["QT_QPA_PLATFORM"] = "offscreen"
    run(sys.executable, "packaging/build_icon.py", "--quiet", env=icon_env)
    run(sys.executable, "-m", "playwright", "install", "chromium", env=env)
    run(sys.executable, "-m", "PyInstaller", "packaging/jobhub.spec", "--clean", "--noconfirm", env=env)
    browser_destination = install_browser_bundle()
    print(f"Bundled Playwright browsers at {browser_destination}", flush=True)
    print(f"Built bundle under {ROOT / 'dist'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
