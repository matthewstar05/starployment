"""Render every desktop view with synthetic data, in light and dark, to PNG files.

    uv run python scripts/render_screenshots.py [OUTPUT_DIR] [--pages dashboard,jobs] [--scale 2]

The script creates a throwaway data directory before importing JobHub, so it
never reads or writes a real candidate profile. It uses Qt's offscreen
platform, so it runs headless (CI, SSH, agents). Output defaults to
``tmp/screenshots``. Pass ``--docs`` to refresh ``docs/screenshots``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

COMPANIES = [
    ("Northwind Labs", "Software Engineer, Platform", "San Francisco, CA", 150_000, 190_000, 93),
    ("Bluebird", "Software Engineer", "Remote (US)", 135_000, 170_000, 88),
    ("Harbor Analytics", "Quantitative Developer", "New York, NY", 175_000, 225_000, 86),
    ("Lumen Health", "Backend Engineer, Data", "Seattle, WA", 140_000, 180_000, 81),
    ("Cobalt Systems", "New Grad Software Engineer", "Austin, TX", 120_000, 140_000, 79),
    ("Pinecrest", "Data Engineer", "Remote", 130_000, 160_000, 74),
    ("Arcadia Robotics", "Software Engineer I", "Boston, MA", 125_000, 150_000, 71),
    ("Meridian Pay", "Full Stack Engineer", "Chicago, IL", None, None, 66),
    ("Tidewater", "Machine Learning Engineer", "San Jose, CA", 160_000, 210_000, 62),
    ("Granite Cloud", "Site Reliability Engineer", "Denver, CO", 140_000, 175_000, 58),
    ("Orchard", "iOS Engineer", "Los Angeles, CA", 145_000, 185_000, 54),
    ("Summit Bio", "Software Engineer, Tools", "San Diego, CA", 118_000, 150_000, 47),
    ("Keystone Fintech", "Associate Engineer", "Charlotte, NC", 95_000, 115_000, 41),
    ("Example", "Data Engineer", "Remote", 128_000, 150_000, 77),
    ("Vantage", "Security Engineer", "Washington, DC", 150_000, 190_000, 35),
]
PAGES = ("dashboard", "jobs", "applications", "profile", "settings", "setup")
# README image names written by --docs.
DOC_NAMES = {
    "dashboard-light.png": "jobhub-macos.png",
    "dashboard-dark.png": "jobhub-macos-dark.png",
    "jobs-light.png": "jobhub-jobs-light.png",
    "jobs-dark.png": "jobhub-jobs-dark.png",
    "applications-light.png": "jobhub-applications-light.png",
    "applications-dark.png": "jobhub-applications-dark.png",
    "settings-light.png": "jobhub-settings-light.png",
    "settings-dark.png": "jobhub-settings-dark.png",
    "setup-step1-dark.png": "jobhub-setup-dark.png",
    "setup-step3-light.png": "jobhub-setup-light.png",
}


def seed(db, config) -> None:
    from jobhub.models import Job, Profile

    now = datetime.now(timezone.utc)
    db.save_profile(Profile(
        first_name="Alex", last_name="Rivera", email="alex.rivera@example.test", phone="555-0134",
        city="San Luis Obispo", state="CA", country="United States",
        linkedin="https://www.linkedin.com/in/example", github="https://github.com/example",
        headline="Software engineer focused on data systems",
        summary="New-grad engineer who builds reliable data pipelines and quantitative tooling.",
        skills=["Python", "SQL", "Go", "Kubernetes", "Pandas", "React"],
    ))
    db.set_settings({
        "setup_complete": True, "auto_apply": True, "dry_run": False, "appearance": "System",
        "target_titles": ["Software Engineer", "Data Engineer", "Quantitative Developer"],
        "preferred_locations": ["San Francisco, CA", "New York, NY", "Remote"],
    })
    jobs = []
    for index, (company, title, location, low, high, _score) in enumerate(COMPANIES):
        slug = company.lower().replace(" ", "")
        jobs.append(Job(
            source="greenhouse", ats="greenhouse", company=company, title=title, location=location,
            remote="Remote" in location, salary_min=low, salary_max=high,
            url=f"https://job-boards.greenhouse.io/{slug}/jobs/{4000 + index}",
            discovered_at=(now - timedelta(hours=index * 3)).isoformat(timespec="seconds"),
        ))
    for index in range(40):
        jobs.append(Job(source="simplify", ats="lever", company=f"Studio {index + 1}", title="Software Engineer",
                        location="Remote", url=f"https://jobs.lever.co/studio{index}/{index:04d}"))
    db.upsert_jobs(jobs)
    scores = {job.id: (float(COMPANIES[i][5]) if i < len(COMPANIES) else 30.0 - i * 0.3,
                       {"title_fit": 30, "skills_fit": 18, "location": 10, "freshness": 8})
              for i, job in enumerate(jobs)}
    db.update_scores(scores)
    by_company = {job.company: job for job in jobs}
    statuses = {"Bluebird": "applied", "Example": "needs_human", "Pinecrest": "needs_human",
                "Keystone Fintech": "skipped", "Vantage": "failed"}
    applications = [
        ("Bluebird", "applied", 1, [], "", "Thank-you page confirmed.\nFollow up with the recruiter Friday."),
        ("Example", "needs_human", 0, ["Current location (required)", "Earliest start date (required)"],
         "Required or invalid fields need attention: location, start date", ""),
        ("Pinecrest", "needs_human", 1, ["Email code"], "Verification code requested after Submit", ""),
        ("Vantage", "failed", 0, [], "The form requires an account login", ""),
    ]
    with db.conn() as con:
        for company, status in statuses.items():
            con.execute("UPDATE jobs SET status=? WHERE id=?", (status, by_company[company].id))
        for offset, (company, status, attempted, unanswered, error, note) in enumerate(applications):
            started = (now - timedelta(hours=offset * 5 + 1)).isoformat(timespec="seconds")
            finished = (now - timedelta(hours=offset * 5)).isoformat(timespec="seconds")
            log = [f"{started} Opened {by_company[company].url}", f"{started} Filled 14 fields from saved facts",
                   f"{finished} Result: {status}"]
            con.execute(
                "INSERT INTO applications(job_id,status,method,started_at,finished_at,log,unanswered,error,"
                "candidate_note,submission_attempted,resume_path) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (by_company[company].id, status, "engine", started, finished, json.dumps(log),
                 json.dumps(unanswered), error, note, attempted, str(config.TAILORED / "resume.pdf")))
    events = [
        (5, "info", "Scan started: greenhouse, lever, ashby, simplify"),
        (4, "warn", "Scan partial: 2782 jobs from 5/6 sources, 12 new"),
        (3, "info", f"applied: {by_company['Bluebird'].id}: Confirmation page detected"),
        (2, "warn", f"needs_human: {by_company['Example'].id}: Required fields need attention"),
        (1, "info", "Scan ok: 2790 jobs from 6/6 sources, 8 new"),
    ]
    with db.conn() as con:
        for minutes_ago, level, message in events:
            ts = (now - timedelta(minutes=minutes_ago * 37)).isoformat(timespec="seconds")
            con.execute("INSERT INTO events(ts,level,msg) VALUES (?,?,?)", (ts, level, message))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", nargs="?", default=str(ROOT / "tmp" / "screenshots"))
    parser.add_argument("--pages", default=",".join(PAGES), help="comma-separated subset of " + ", ".join(PAGES))
    parser.add_argument("--appearances", default="Light,Dark")
    parser.add_argument("--scale", type=float, default=2.0, help="device pixel ratio for crisp output")
    parser.add_argument("--size", default="1280x820")
    parser.add_argument("--docs", action="store_true", help="write the README screenshots in docs/screenshots")
    args = parser.parse_args()

    temporary = tempfile.TemporaryDirectory(prefix="jobhub-screens-")
    os.environ["JOBHUB_DATA_DIR"] = str(Path(temporary.name) / "data")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["QT_SCALE_FACTOR"] = str(args.scale)
    os.environ["JOBHUB_REDUCED_MOTION"] = "1"

    from PySide6.QtWidgets import QApplication

    from jobhub import config, db
    from jobhub.desktop.controller import DesktopController
    from jobhub.desktop.styles import app_font, apply_appearance
    from jobhub.desktop.ui import JobHubWindow
    from jobhub.desktop.wizard import SetupWizard
    from jobhub.desktop import widgets

    if args.docs:
        widgets.BACKDROP_GRAIN = False  # keeps README images small; invisible at this alpha anyway

    db.init()
    config.TAILORED.mkdir(parents=True, exist_ok=True)
    seed(db, config)
    app = QApplication.instance() or QApplication([])
    app.setFont(app_font())
    output = ROOT / "docs" / "screenshots" if args.docs else Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    width, height = (int(part) for part in args.size.lower().split("x"))
    pages = [page.strip() for page in args.pages.split(",") if page.strip()]
    written = []
    for appearance in [value.strip() for value in args.appearances.split(",") if value.strip()]:
        apply_appearance(app, appearance)
        window = JobHubWindow()
        controller = DesktopController(window, scheduler_enabled=False, show_setup_on_start=False)
        apply_appearance(app, appearance)
        controller._next_cycle_at = datetime.now().timestamp() + 42 * 60
        controller.scheduler_enabled = True
        controller.refresh(include_account=True)
        window.resize(width, height)
        window.show()
        for index, page in enumerate(("dashboard", "jobs", "applications", "profile", "settings")):
            if page not in pages:
                continue
            window.nav.setCurrentRow(index)
            if page == "jobs":
                window.jobs.table.selectRow(1)
            for _ in range(5):
                app.processEvents()
            path = output / f"{page}-{appearance.lower()}.png"
            window.grab().save(str(path))
            written.append(path)
        if "setup" in pages:
            wizard = SetupWizard(db.get_profile().model_dump(), db.get_settings(), window)
            wizard.resize(860, 700)
            wizard.show()
            for step in (0, 2):
                wizard.tabs.setCurrentIndex(step)
                for _ in range(5):
                    app.processEvents()
                path = output / f"setup-step{step + 1}-{appearance.lower()}.png"
                wizard.grab().save(str(path))
                written.append(path)
            wizard.close()
        controller.stop()
        window.close()
    db.close()
    if args.docs:
        renamed = []
        for path in written:
            target = DOC_NAMES.get(path.name)
            if target is None:
                path.unlink()
                continue
            renamed.append(path.replace(path.with_name(target)))
        written = renamed
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
