# AGENTS.md — working on JobHub

This file is for coding agents (Claude Code, Codex, Cursor, and others) and for
humans new to the codebase. It explains what JobHub is, the rules that keep a
candidate safe, how the code is organized, and how to change the desktop UI
without breaking its design language. Read the whole "Ground rules" section
before touching anything under `jobhub/apply/`, `jobhub/scheduler.py`, or
`jobhub/db.py`.

- **Product:** a local, private job-search workspace. It imports a resume,
  stores candidate-owned answers, discovers and ranks listings from public ATS
  feeds, builds an evidence-based resume PDF per role, and fills supported
  employer forms (Greenhouse, Lever, Ashby) with Playwright, on an hourly loop
  while the app is open.
- **Primary interface:** the native PySide6 desktop app (`jobhub/desktop/`).
  The FastAPI web UI (`jobhub/server.py` + `web/`) is a legacy compatibility
  surface; keep it working, but new product work goes into the desktop app.
- **Stack:** Python 3.12 only (`>=3.12,<3.13`), PySide6 6.11 (Qt 6.11),
  SQLite (WAL), Playwright/Chromium, JobSpy, ReportLab, PyInstaller. Managed with
  `uv`; the checkout has a `.venv`.
- **Git:** the code lives in the private GitHub repository
  `matthewstar05/JobHub` (branch `main`). Commit or push only when the owner
  asks. `data/`, `.env`, `tmp/`, `dist/`, `build/`, `.venv/`, the bundled
  Chromium, and `VERIFICATION.md` (a personal log naming real employers) are
  git-ignored and must never be committed. Test fixtures use fictional
  details (`example.test` emails, 555 phone numbers), never the owner's real
  ones.
- **The owner runs the installed app** from `/Applications/JobHub.app`. Every
  iteration that changes the app ends by rebuilding and reinstalling it; see
  "Definition of done" below.

## Definition of done (every iteration)

An iteration is not finished until the owner's installed app runs the new
code. After you change anything that ships in the bundle (`jobhub/`, `web/`,
`packaging/assets/`, `packaging/hooks/`, `packaging/jobhub.spec`,
`packaging/runtime_hook.py`, `pyproject.toml`, `uv.lock`):

1. **Tests pass:** `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q`.
2. **UI changes are checked by eye:** run `scripts/render_screenshots.py` and
   look at the affected pages (§4).
3. **Build, smoke-test, install, relaunch:** in one command,

   ```sh
   .venv/bin/python packaging/install_app.py --build
   ```

   This runs `build_desktop.py` and `smoke_bundle.py`, stages a signed copy,
   waits for any in-flight application in the live database to finish, asks
   the running JobHub to quit cleanly, swaps the new bundle into
   `/Applications/JobHub.app` (restoring the old one if verification fails),
   and relaunches JobHub if it was running. Candidate data in
   `~/Library/Application Support/JobHub` is never modified.
4. **Report it:** tell the owner the app was reinstalled and relaunched, or
   why it was not.

Rules for this step:

- If only docs, tests, or dev scripts changed, skip the rebuild. The installer
  refuses a bundle older than its bundled sources, so run it with `--build`
  whenever in doubt.
- Never kill the process or otherwise interrupt JobHub while an application
  is queued or running. The installer also waits for an in-progress scan. If it
  gives up waiting, report that and leave the installed app alone.
- The build takes a few minutes and the bundle is about 900 MB; run it once, at
  the end of the iteration, not after each edit.
- This applies only on the owner's Mac. On other machines or in CI, stop after
  the tests and `build_desktop.py`/`smoke_bundle.py`.
- Automatic applications are on in the owner's live settings. Relaunching
  starts a scan within seconds and may submit applications as configured. That
  is expected; do not change the owner's settings to avoid it.

---

## 1. Ground rules (safety invariants)

These are product guarantees, and several have regression tests. Do not weaken
them to make a feature easier.

1. **Never invent candidate facts.** Answers come only from the parsed resume
   and from values the candidate saved (`settings.answers`,
   `profile.custom_facts`). An unknown required answer stops the attempt with
   status `needs_human`. Demographic questions are declined. Never add
   defaults such as "work authorized: Yes".
2. **"Applied" means confirmed.** A submission counts only with a new employer
   confirmation signal *and* a durable local record. `db.finish_application`
   refuses `applied` without `submission_attempted`. A form fill or Submit click
   alone is not success.
3. **The submit boundary is durable.** `db.mark_submission_attempted` runs
   before any browser action that can send data. Anything past that boundary
   is never auto-retried (`requeue_preflight_needs_human`,
   `JobHubWindow._retryable`). Interrupted attempts become `needs_human` on
   restart (`db.recover_interrupted`).
4. **One vacancy, one application.** `db.reserve_application` blocks
   duplicates across sources by canonical key. Automatic runs are capped per run
   and at one attempt per employer per cycle.
5. **Automation is opt-in.** `auto_apply` defaults to off; practice mode
   (`dry_run`) fills forms without clicking Submit.
6. **User data never ships.** `data/`, `.env`, resumes, screenshots, and the
   Playwright browser profile must never enter a bundle or
   `dist/JobHub-source.zip` (`packaging/export_source.py` is an allowlist on
   purpose). `VERIFICATION.md` is a personal log and is excluded too.
7. **Don't experiment on the real profile.** For demos, screenshots, or manual
   runs, point `JOBHUB_DATA_DIR` at a fresh temporary directory (see §4). The
   real data lives in `data/` for source runs and in
   `~/Library/Application Support/JobHub` for the packaged app.
8. **Tests never contact employers.** They use temporary databases and local
   browser fixtures. Keep it that way; mock the network.
9. **Secrets stay in the OS vault.** The IMAP app password goes through
   `keyring` (`jobhub/apply/email_verification.py`), never into SQLite or logs.
   The Settings view sends it as a transient `_imap_password` patch key that the
   controller strips before saving.

---

## 2. Commands

Run everything from the repository root. `uv run …` works too; the `.venv`
interpreter is equivalent.

| Task | Command |
| --- | --- |
| Install | `uv sync --frozen` (add `--group dev` for PyInstaller) and `uv run playwright install chromium` |
| Run the desktop app | `./run_desktop.sh` (or `.venv/bin/python -m jobhub.desktop --no-scheduler`) |
| Open setup directly | `.venv/bin/python -m jobhub.desktop --setup` |
| All tests | `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q` (≈220 tests, ~95 s) |
| Desktop tests only | `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/test_desktop_*.py tests/test_application_notes.py` |
| Lint | `uvx ruff check --select F,E9,B --ignore B008,B905,B904 jobhub packaging scripts tests` (unused `app = QApplication(...)` in tests is intentional) |
| Screenshots of every view | `.venv/bin/python scripts/render_screenshots.py` → `tmp/screenshots/` (`--docs` refreshes `docs/screenshots/`) |
| Build the app bundle | `.venv/bin/python packaging/build_desktop.py` → `dist/JobHub.app` |
| Smoke-test the bundle | `.venv/bin/python packaging/smoke_bundle.py` |
| Build, install to /Applications, relaunch | `.venv/bin/python packaging/install_app.py --build` (end of every iteration; flags: `--launch`, `--no-launch`, `--wait SECONDS`) |
| Portable source zip | `.venv/bin/python packaging/export_source.py` → `dist/JobHub-source.zip` |
| Legacy web UI | `./run.sh` or `.venv/bin/python -m jobhub serve --no-scheduler` (port 8765) |
| Health check | `.venv/bin/python -m jobhub doctor` |

The offscreen Qt platform prints `Populating font family aliases took … ms`.
That warning is harmless.

### Environment variables

| Variable | Effect |
| --- | --- |
| `JOBHUB_DATA_DIR` | Data root (DB, resumes, PDFs, screenshots, browser profile). Rejects home, filesystem root, symlinks, and unrecognized populated directories. |
| `JOBHUB_LLM_BACKEND` | `none` / `auto` / `anthropic` / `claude_cli` / `codex_cli`. The core workflow needs no LLM. |
| `JOBHUB_CODEX_MODEL`, `JOBHUB_CLAUDE_CLI_MODEL`, `JOBHUB_ANTHROPIC_MODEL` | Model overrides for the optional LLM backends. |
| `JOBHUB_HEADLESS` | `0` shows Chromium during attempts. |
| `JOBHUB_PORT` | Legacy web server port (8765). |
| `JOBHUB_REDUCED_MOTION` | `1` disables UI animation, `0` forces it on; unset follows macOS Reduce Motion. |
| `JOBHUB_NATIVE_TITLEBAR` | `0` keeps a standard macOS title bar instead of the full-height glass layout. |
| `QT_QPA_PLATFORM=offscreen` | Headless Qt for tests, scripts, and CI. |

---

## 3. Architecture

```
sources/*  ──► db.upsert_jobs ──► scoring.score_all ──► scheduler.scan_once
   (public ATS feeds, Simplify,           │                     │
    RemoteOK, JobSpy, URL import)         ▼                     ▼
                                   SQLite (db.py)     scheduler.auto_apply_batch
                                          ▲                     │
                                          │         apply/engine.py (Playwright)
                                          │           + answers.py + tailor.py
                                          └──── db.finish_application ◄─┘

desktop/controller.py: one worker thread runs scans/applies/imports; the Qt
thread only reads SQLite (refresh every 15 s) and renders via window.set_data.
```

| Path | Responsibility |
| --- | --- |
| `jobhub/config.py` | Paths, env vars, default settings, settings validation (`validate_settings`, `merge_settings`). |
| `jobhub/models.py` | Pydantic `Job`, `Profile`, `ApplyResult`; canonical URLs and job IDs. |
| `jobhub/db.py` | SQLite schema, dedup view (`DEDUPED_JOBS`), jobs, applications, runs, events, settings, profile. |
| `jobhub/sources/` | One module per source; `import_job.py` validates and imports a single posting URL. |
| `jobhub/scoring.py` | Deterministic, explainable 0–100 match score and auto-apply eligibility. Settings that affect it are listed in `controller.SCORING_KEYS`. |
| `jobhub/scheduler.py` | `scan_once`, `rescore`, `apply_one`, `auto_apply_batch`, shared `STATE`. |
| `jobhub/apply/engine.py`, `adapters.py` | Browser form filling per ATS, confirmation detection, screenshots. |
| `jobhub/answers.py` | Maps rendered form questions to saved candidate answers without guessing. |
| `jobhub/profile.py`, `tailor.py` | Resume parsing (pdftotext/pypdf, optional LLM) and role-specific PDF output. |
| `jobhub/setup.py`, `readiness.py` | Saving setup; what is still missing before unattended work. |
| `jobhub/migration.py` | One-time import of checkout data into the packaged app's data directory. |
| `jobhub/server.py`, `web/` | Legacy FastAPI JSON API and static web UI (no build step). |
| `jobhub/desktop/` | The native app. See §5. |
| `packaging/` | PyInstaller spec, runtime hook, icon builder, build and smoke scripts, source exporter. |
| `scripts/render_screenshots.py` | Headless screenshots of every desktop view with synthetic data. |
| `tests/` | Pytest suite; files are named by area (`test_apply_engine.py`, `test_desktop_theme.py`, …). |

---

## 4. Verifying UI work visually

Tests prove behavior; screenshots prove design. After any visual change:

1. `.venv/bin/python scripts/render_screenshots.py` renders Overview, Jobs,
   Applications, Profile, Settings, and two setup steps in Light and Dark at 2×
   into `tmp/screenshots/`. It seeds a throwaway data directory, so it is safe.
   Use `--size 1000x680` to check the minimum window size, and `--pages jobs,settings`
   to limit output.
2. Open the PNGs and look at them: alignment, truncation, contrast, spacing.
3. For native macOS behavior (title bar, traffic lights, real fonts), run the
   app on the `cocoa` platform against a temporary data directory, for example:

   ```python
   import os, sys, tempfile; from pathlib import Path
   sys.path[:0] = [".", "scripts"]
   os.environ["JOBHUB_DATA_DIR"] = str(Path(tempfile.mkdtemp()) / "data")
   from jobhub import config, db; from render_screenshots import seed
   db.init(); config.TAILORED.mkdir(parents=True, exist_ok=True); seed(db, config); db.close()
   from jobhub.desktop.__main__ import main; main(["--no-scheduler"])
   ```

   `screencapture -x out.png` captures the screen if the terminal has Screen
   Recording permission.

---

## 5. The desktop app

### 5.1 Files

| File | What lives there |
| --- | --- |
| `desktop/__main__.py` | Entry point and flags: `--no-scheduler`, `--setup`, `--smoke-test PNG`, `--browser-check`, `--dependency-check`. |
| `desktop/controller.py` | `DesktopController`: signal wiring, worker thread, refresh loop, toasts, save handlers. The only place the UI talks to `db`/`scheduler`. |
| `desktop/ui.py` | `JobHubWindow` shell: floating sidebar, page stack, toasts, keyboard shortcuts, macOS menu bar, theme crossfade, `set_data`, `set_scan_state`. Re-exports the views. |
| `desktop/views/` | One module per sidebar page: `dashboard.py`, `jobs.py` (+ `AddPostingSheet`), `applications.py`, `profile.py`, `settings.py`. |
| `desktop/wizard.py` | `SetupWizard`: step indicator, resume drop zone, grouped forms, answer bank. |
| `desktop/widgets.py` | The design system: backdrop, glass, controls, motion, layout helpers. |
| `desktop/delegates.py` | `GlassTable` and item delegates (role, score ring, status pill, action capsule, application row). |
| `desktop/theme.py` | Design tokens (`PALETTES`, `BACKDROP_GLOWS`) and `ThemeManager`. |
| `desktop/styles.py` | Global stylesheet for stock Qt controls, `app_font`, `color_for_status`. |
| `desktop/format.py` | Pure copy helpers: status labels, relative time, countdown, greeting, salary, activity copy, log prettifying. No Qt; unit-test here. |
| `desktop/native.py` | macOS window chrome (full-height content, hidden title, unified toolbar). |
| `desktop/icons.py`, `icon.py` | Lucide SVG icons tinted at runtime; the app icon. |

### 5.2 Design language

JobHub follows Apple's current "liquid glass" direction: content floats on
translucent, rounded materials over a soft ambient color field, with restrained
motion and minimal chrome. Concretely:

- **Backdrop.** `AmbientBackdrop` / `paint_backdrop` paints a near-neutral base
  with four large, soft radial glows (`theme.BACKDROP_GLOWS`) and a faint grain
  that prevents banding (`widgets.BACKDROP_GRAIN`; `render_screenshots.py --docs`
  turns it off because noise bloats PNGs). It renders at quarter resolution and
  is cached per size and scheme. The glows avoid the upper-left, where titles sit directly on
  the backdrop, so muted text keeps at least 4.5:1 contrast.
- **Glass.** `GlassPanel` (and `paint_glass`) draws a translucent vertical
  gradient fill, a 1px outer hairline (`glass_line`), and a specular inner rim
  that fades out within ~30px (`glass_edge`). Materials: `regular` for content
  cards, `sidebar` for the thinner navigation pane, and `thick` for sheets,
  toasts, and menus. `tint="accent"` (etc.) washes a panel with a semantic color.
- **Floating chrome.** The sidebar is a 236px glass pane inset 8–10px from the
  window edges with radius 22. Its drop shadow is painted by the backdrop
  (`add_shadow_target`), not by a `QGraphicsEffect`, so animating inside the
  sidebar stays cheap.
- **Shape.** Buttons, pills, search fields, and segmented controls are
  capsules. Cards use radius 16–18, grouped settings 14, and rows 10–12. Corner
  radii nest: inner radius is roughly outer radius minus the padding.
- **Type.** System UI font (`.AppleSystemUIFont` on macOS) in px sizes: 28 bold
  page titles, 15 semibold card titles, 13 body, 12 secondary, 11 captions and
  eyebrows. Select label roles with `objectName` (see §5.4).
- **Color.** Semantic tones only: accent (blue), green (applied/success), amber
  (needs review/warning), red (issue/error), indigo (practice runs/tracked).
  Status colors come from `styles.color_for_status`; tone pairs from
  `widgets.tone_colors`.
- **Copy.** Sentence case, short, calm, specific. Say what happened and what to
  do next ("Application needs your review · Required fields: location"). Never
  show internal IDs, raw enum names, or stack traces in primary UI; keep them in
  logs and the evidence panel. A score is a ranking aid, never a probability.
- **Spacing.** Pages use 30px side and 26px top margins; 14px between sibling
  cards; 18px between page sections; 22px between settings groups.

### 5.3 Theming rules

- Every color is a token in `theme.PALETTES` (Light and Dark have identical
  keys; add new tokens to both). Opaque colors are `#RRGGBB`; translucent glass
  tokens are CSS `rgba(r,g,b,a)` strings that work in stylesheets and through
  `widgets.qcolor()`.
- **Resolve colors at paint time** with `theme.current_colors()`. Do not store
  colors on widgets or bake them into per-widget stylesheets.
  `ThemeManager.apply()` swaps the palette and stylesheet and calls `update()`
  on every widget, so paint-time lookups switch themes for free. The detail
  status pill once used an inline stylesheet and kept stale colors after a
  theme change; `StatusPill` exists to avoid exactly that.
- Stock controls (inputs, buttons, scroll bars, tables, menus, tooltips) are
  styled in `styles.stylesheet()`. Custom-painted widgets deliberately have no
  stylesheet rules.
- Icons: `icon_pixmap(name, color, size, stroke)` is cached. Widgets whose icon
  color depends on the theme refresh it on `QEvent.PaletteChange` or
  `StyleChange` (see `CapsuleButton`, `SearchField`).
- Theme switches from Settings apply instantly (`SettingsView.appearance_changed`
  → `controller.set_appearance`) under `JobHubWindow.crossfade`.

### 5.4 Stylesheet label roles

`label(text, role)` and `ElidedLabel(text, role)` set `objectName`:

`brand`, `eyebrow`, `pageTitle`, `sheetTitle`, `subtitle`, `caption`,
`sectionTitle`/`cardTitle`, `groupTitle`, `groupNote`, `rowTitle`, `rowNote`,
`emptyTitle`, `metricValue`, `metricLabel`, `activityTime`, `activityMessage`,
`rowPrimary`, `rowSecondary`, `sidebarStatus`, `sidebarStatusTitle`,
`detailTitle`, `callout` (inset note box), `questions` (amber note box),
`dropHint`.

Buttons: `button(text, role, icon=..., trailing=...)` where role is `primary`
(default, no objectName), `secondary`, `quiet`, or `danger`.

### 5.5 Building blocks (`widgets.py`)

| Widget | Use it for |
| --- | --- |
| `AmbientBackdrop`, `paint_backdrop` | Any top-level surface (the window shell, dialogs, sheets). Dialogs call `paint_backdrop` in `paintEvent`. |
| `GlassPanel`, `Card`, `ClickableGlass` | Content containers; `Card` adds a title row, trailing action, and row separators; `ClickableGlass` is a tappable tile. |
| `FormGroup` | System Settings-style groups. `add_row(title, field, description)` expands text inputs and hugs compact controls; `add_widget` adds a full-width row. |
| `ToggleSwitch` | Every boolean. It is a `QCheckBox`, so `isChecked`/`setChecked`/`toggled` work. |
| `SegmentedControl` | Small exclusive choices (theme, filters, resume strategy). Mirrors the `QComboBox` API; `activated` fires only for user clicks. |
| `Stepper` | Wraps a `QSpinBox` with a −/+ capsule. Keep reading and writing the wrapped spin box. |
| `SearchField` | Capsule search input with a magnifier and a clear button. |
| `GlassNavList` | The sidebar list with a spring-animated selection pill and count badges (`set_badge`). |
| `FadeStack` | A `QStackedWidget` with fade-and-rise (pages) or push (wizard) transitions. It removes its opacity effect when done. |
| `Toast` | Transient feedback. Use `window.notify(text, tone, timeout)`; tones are `info`, `success`, `warning`, `error`, and `busy` (spinner; timeout 0 keeps it). |
| `CountUpLabel`, `ActivityIndicator`, `StatusDot`, `StatusPill`, `IconBadge`, `Monogram` | Metrics, spinners, timeline dots, status capsules, tinted symbols, employer initials. |
| `PageHeader`, `EmptyState`, `ElidedLabel`, `ActionRow` | Page titles and actions, empty collections, single-line truncation, clickable list rows. |
| `DirtyTracker` | Enables Save only when there are unsaved edits and protects edits from background refreshes. |

### 5.6 Motion

- Durations live in `widgets.Motion` (`FAST` 160ms, `BASE` 260, `PAGE` 300,
  `SLOW` 420, `COUNT` 750). Prefer ease-out curves (`OutCubic`, `OutQuart`) and
  `spring_curve()` for things that travel (the nav pill and segmented thumb).
- Every animation must call `reduced_motion()` and jump to its end state when
  it returns True **or when the widget is not visible yet**. Initial data loads
  should never animate from zero, except the deliberate first-show count-up.
- Use `QVariantAnimation` plus a paint-time value for custom widgets. Avoid
  long-lived `QGraphicsEffect`s: they re-render the subtree every frame and
  can soften text. `FadeStack` and `crossfade` remove theirs when finished.

### 5.7 Data flow and refresh contract

- The controller calls `window.set_data(...)` every 15 seconds and after every
  action. Views **must** make identical data a no-op: they compare a signature
  and skip rebuilding (`JobsView.set_jobs`, `ApplicationsView.set_applications`,
  dashboard sections). When data does change, preserve the user's selection,
  scroll position, and unsaved input.
- Profile and Settings use `DirtyTracker`; `set_data` skips a dirty page so a
  refresh never overwrites typing. Application notes autosave 900ms after typing
  stops and survive refreshes.
- Table cells are painted by delegates from item roles (`delegates.*_ROLE`).
  Do not add per-row widgets (`setCellWidget`). The old per-row buttons made
  each refresh slow and reset the table.
- Views emit signals; only `controller.py` touches `db` or `scheduler`.
  Long work goes through `DesktopController._tasks` onto the worker thread,
  and results return via `WorkerSignals`.

### 5.8 macOS window chrome

`native.configure_window_chrome` sets `Qt.WindowType.ExpandedClientAreaHint`
(Qt 6.9+; it reuses bit `0x00400000`, so PySide exposes it only as an enum alias)
plus `NoTitleBarBackgroundHint`, turns off `WA_ContentsMarginsRespectsSafeArea`,
and through `ctypes`/libobjc hides the title text and attaches an empty
**unified** `NSToolbar` (`NSWindowToolbarStyleUnified` = 3). The toolbar makes the
title band 52pt tall (`native.TITLE_BAR_HEIGHT`), which centers the traffic
lights inside the floating sidebar. Consequences:

- The top 52pt of the window belongs to AppKit's title bar: clicks there drag
  the window and double-clicks zoom. Keep interactive controls below it. The
  sidebar reserves `TITLE_BAR_HEIGHT` of top padding.
- All of this is skipped off macOS, on the offscreen platform, and when
  `JOBHUB_NATIVE_TITLEBAR=0`. Every ctypes call is wrapped so failure falls
  back to a standard title bar.
- The native menu bar (File, Edit, View, Window) is only created on the
  `cocoa` platform; the same `QAction`s carry the shortcuts everywhere.

Shortcuts: ⌘1–⌘5 pages, ⌘R refresh, ⌘N add posting, ⌘F find jobs, ⌘S save
the current page, ⌘, Settings, ⌘⇧E edit setup, Return opens the selected job.

### 5.9 Recipes

**Add a setting.** Add the default to `config.DEFAULT_SETTINGS` and its type
to `validate_settings`. Add a control in `views/settings.py` inside a
`FormGroup`, register it with `self.tracker.watch(...)`, and read and write it
in `set_settings` and `_save`. If it changes ranking, add the key to
`controller.SCORING_KEYS`. Extend `tests/test_desktop_settings_view.py`.

**Add a page.** Create `views/<name>.py` with a `QWidget` whose objectName is
`page`, using 30/26 margins and a `PageHeader`. Add it to `PAGES` and the stack
in `ui.py`, wire its signals in `JobHubWindow.__init__`, and handle them in the
controller. Add a Lucide icon if needed (below). Add a screenshot branch to
`scripts/render_screenshots.py`.

**Add an icon.** Save the Lucide SVG in `packaging/assets/lucide/` (keep
`stroke="currentColor"`), register it in `icons._ICON_FILES`, and list it in
`packaging/assets/lucide/README.md`. The PyInstaller spec bundles the whole
directory.

**Add a status.** Extend `format.STATUS_LABELS` and `styles.color_for_status`.

**Change a color.** Change the token in both palettes, then run
`tests/test_desktop_theme.py` (it enforces backdrop luminance and text
contrast) and look at the screenshots.

### 5.10 Testing Qt code

- Put `os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")` at the top of Qt
  test modules and reuse `QApplication.instance() or QApplication([])`.
- Isolate the database by monkeypatching `config.DATA`, `DB_PATH`, `RESUMES`,
  `TAILORED`, and `SCREENSHOTS`, then `db.close(); db.init()`
  (see `tests/test_desktop_setup.py::local_store`).
- Pages and cards are transparent. `widget.grab()` on them returns transparent
  pixels, so sample the composited top-level window instead
  (`tests/test_desktop_theme.py::_rendered_surface`).
- Set `JOBHUB_REDUCED_MOTION=1` for pixel tests. Mid-transition pages move
  under sample points. For animation tests, force `0`, pump events for the
  duration, and assert the end state (`tests/test_desktop_widgets.py`).
- A widget's `isVisible()` is False until its window is shown, which also
  suppresses animations.
- Where tests live: `test_desktop_theme.py` (appearance, luminance, contrast),
  `test_desktop_widgets.py` (primitives, motion end states, refresh safety,
  routing), `test_desktop_controller.py` (appearance, rescoring, skip/restore,
  toasts), `test_desktop_setup.py` (wizard payloads, Jobs table), `test_desktop_settings_view.py`,
  `test_desktop_activity.py`, and `test_application_notes.py`.

---

## 6. Packaging

- `packaging/build_desktop.py` regenerates icons from
  `packaging/assets/jobhub-icon.svg`, installs Chromium into
  `packaging/playwright-browsers/`, runs PyInstaller with
  `packaging/jobhub.spec`, copies Chromium and the Playwright driver into the
  bundle, then ad hoc signs `dist/JobHub.app` on macOS. It takes a few
  minutes. Chromium is large; the directory is git-ignored.
- `collect_submodules("jobhub")` picks up new modules automatically. New
  non-Python assets need a `datas` entry in the spec (the Lucide directory and
  app icon are already included).
- `packaging/smoke_bundle.py` runs the frozen app with a temporary data
  directory: dependency check, Chromium launch, and an offscreen window render.
- `packaging/install_app.py` installs `dist/JobHub.app` into `/Applications`
  (macOS only). It refuses stale bundles, never interrupts an in-flight
  application or scan, quits the app through Apple Events (bundle ID
  `local.jobhub.desktop`), swaps atomically with rollback, and relaunches.
  Tests: `tests/test_install_app.py`.
- PyInstaller does not cross-compile. `.github/workflows/native-bundles.yml`
  (manual trigger) builds macOS arm64 and x64, Windows x64, and Linux x64.
  macOS builds are ad hoc signed, not notarized.

---

## 7. Pitfalls worth knowing

- `pypdf` mangles some Word-exported PDFs; `profile.extract_text` prefers
  `pdftotext` (poppler) when available.
- ZipRecruiter blocks JobSpy (HTTP 403) and RemoteOK can fail DNS on some
  networks. Public Greenhouse, Lever, and Ashby APIs plus Simplify's list are
  the reliable sources. Source failures are reported, never fatal.
- LLMs are optional. `codex exec -m gpt-5.5` works on the original developer
  machine where the codex default model is rejected; `claude -p` needs a logged-in CLI.
- `db.set_job_status` refuses to reset a job with a submission record. The
  Jobs context menu surfaces that refusal as a warning toast.
- Qt stylesheet `rgba()` accepts fractional alpha, but CSS border triangles
  for spin-box arrows do not render. Use images or `Stepper`.
- Keep `QMainWindow.statusBar()` unused; feedback goes through toasts.
