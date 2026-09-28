# JobHub desktop app

JobHub has a native PySide6 entrypoint at `python -m jobhub.desktop` (implemented
by `jobhub.desktop.__main__`). The
repository launchers run that entrypoint from a checkout. They use the local
`.venv` first, then a PyInstaller
bundle if no local environment exists.

The app follows the operating system's light or dark appearance by default.
Use **Settings → Appearance** to choose a fixed theme. This preference is stored
with the other local settings and applies immediately with a short crossfade.
The setup wizard, sheets, and every page share the same ambient backdrop and
glass surfaces. The icons are bundled Lucide SVG assets with attribution in
`packaging/assets/lucide/README.md`.

On macOS the window content extends under a transparent, unified title bar, so
the traffic-light buttons sit inside the floating glass sidebar. Set
`JOBHUB_NATIVE_TITLEBAR=0` for a standard title bar. Animations follow the
system Reduce Motion preference; `JOBHUB_REDUCED_MOTION=1` turns them off and
`JOBHUB_REDUCED_MOTION=0` forces them on. The design system and its rules are
described in [AGENTS.md](../AGENTS.md#5-the-desktop-app).

## Run from a checkout

On macOS, double-click `Launch JobHub.command`, or run this command on macOS or Linux:

```sh
./run_desktop.sh
```

On Windows, double-click `run_desktop.bat`. The launcher prefers the local
`.venv` interpreter and sets `JOBHUB_DATA_DIR` to `data/` by default. After a
successful checkout import into a packaged app, it uses the same per-user data
directory as the bundle. Set `JOBHUB_DATA_DIR` explicitly to a different
writable directory when the checkout is read-only, for
example `JOBHUB_DATA_DIR="$HOME/Library/Application Support/JobHub/data"`.
The source launchers only accept Python 3.12, matching the project requirement.
For a fresh checkout, run `uv sync --frozen` before launching. If the local
environment has a different Python version, the launcher tries a Python 3.12
runtime installed on the system and otherwise prints this setup instruction.

The data directory contains the SQLite database, resumes, generated PDFs,
screenshots, and the dedicated Playwright browser profile. It is intentionally
not included in a desktop bundle. Do not copy `.env`, `data/`, or a browser
profile into a release archive.

## Build a native bundle

PyInstaller is platform-specific. Build on each target operating system and
architecture with Python 3.12, the version required by `pyproject.toml`. From
the repository root, install the desktop build dependencies in the build
environment, then run:

```sh
python packaging/build_desktop.py
```

The build helper installs Chromium into `packaging/playwright-browsers` and
embeds it in the bundle. It also generates platform icon assets from
`packaging/assets/jobhub-icon.svg`. This makes the packaged app independent of the
developer's Playwright cache. Chromium is large, so keep that directory out of
source control and remove it after producing a release if desired. The
generated `dist/` and `build/` directories are also release artifacts, not
source files.

On macOS, the helper removes Playwright's local `.links` metadata from the
copied browser tree and ad hoc signs the completed bundle after copying the
nested Chromium app. This preserves the browser framework layout while making
the app bundle internally verifiable. Ad hoc signing is for local use; a
distributed macOS release still needs a developer identity and notarization.

The expected output is `dist/JobHub.app` on macOS and `dist/JobHub/JobHub` or
`dist/JobHub/JobHub.exe` on Linux or Windows. The launchers detect these paths
when no local `.venv` is present. Opening the app bundle directly uses
JobHub's OS-specific per-user data directory by default.

For a bundle opened directly from this checkout's `dist/` folder, a one-time
startup import copies the checkout's SQLite history, resumes, tailored PDFs,
and screenshots into that directory. The import creates a backup if an empty
or partial destination database already exists. It skips any destination
that has an identity or job/application history, and it never transfers the
dedicated browser profile or `.env`. Once the bundle is moved outside this
checkout, fresh users receive the normal setup wizard.

PyInstaller does not cross-compile. A Windows executable must be built on
Windows, and a Linux executable must be built on Linux. This checkout only
provides the build recipe; each platform still needs a native smoke test before
distribution.

## Playwright and user data

The bundle includes Playwright's Python support files, driver, and Chromium.
The runtime hook points `PLAYWRIGHT_BROWSERS_PATH` at the bundled browser. The
application must keep its database, resume files, generated output, and browser
profile in `JOBHUB_DATA_DIR`, outside the read-only application bundle. This
also prevents one user's credentials and browsing state from being shipped to
another user.

For a release test, start the bundle with an empty temporary data directory,
complete the profile setup, and run `JobHub --browser-check` before testing a
real posting. A successful PyInstaller build alone does not prove that the
native UI or Chromium works on the target machine.
