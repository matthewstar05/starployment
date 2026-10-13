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
PAGES = ("dashboard", "jobs", "applications", "tracker", "profile", "resume-bank", "builder", "settings", "apply", "setup")
# README image names written by --docs.
DOC_NAMES = {
    "dashboard-light.png": "starployment-overview-light.png",
    "dashboard-dark.png": "starployment-overview-dark.png",
    "jobs-light.png": "starployment-jobs-light.png",
    "jobs-dark.png": "starployment-jobs-dark.png",
    "applications-light.png": "starployment-applications-light.png",
    "applications-dark.png": "starployment-applications-dark.png",
    "tracker-light.png": "starployment-tracker-light.png",
    "tracker-dark.png": "starployment-tracker-dark.png",
    "builder-light.png": "starployment-resume-builder-light.png",
    "settings-light.png": "starployment-settings-light.png",
    "settings-dark.png": "starployment-settings-dark.png",
    "setup-step1-dark.png": "starployment-setup-dark.png",
    "setup-step3-light.png": "starployment-setup-light.png",
}


def seed(db, config) -> None:
    from jobhub.models import Education, Experience, Job, Profile, Project

    now = datetime.now(timezone.utc)
    experience = [
        Experience(company="Northbeam Analytics", title="Software Engineer Intern", location="San Francisco, CA",
                   start="Jun 2025", end="Sep 2025", tech=["Python", "Airflow", "Postgres"],
                   bullets=["Built an Airflow pipeline that cut nightly reporting time from 3 hours to 40 minutes",
                            "Added data-quality checks that caught 12 schema regressions before release",
                            "Wrote a Postgres partitioning plan for a 2 TB events table"]),
        Experience(company="Campus Research Lab", title="Research Assistant", location="San Luis Obispo, CA",
                   start="Jan 2024", end="Present", tech=["R", "Stata", "Python"],
                   bullets=["Estimated labor-market models on 1.2M survey records",
                            "Automated figure generation for two working papers"]),
        Experience(company="Harbor Trading Club", title="Quant Developer", start="Sep 2023", end="Jun 2025",
                   tech=["C++", "Python"], bullets=["Wrote a backtester for 40 equity strategies",
                                                    "Mentored eight members on market microstructure"]),
    ] + [Experience(company=f"Example Co {i}", title="Teaching Assistant", start=f"Jan {2019 + i}",
                    end=f"Jun {2019 + i}", bullets=["Led weekly labs for 30 students"]) for i in range(8)]
    projects = [
        Project(name="Order Book Simulator", role="Author", tech=["C++", "CMake"],
                bullets=["Simulated a limit order book processing 1M messages per second"]),
        Project(name="Transit Delay Forecasts", role="Lead", tech=["Python", "Pandas", "XGBoost"],
                bullets=["Forecast bus delays with 18% lower error than the agency baseline"]),
        Project(name="Voxel Engine", role="Author", tech=["C++", "OpenGL"],
                bullets=["Rendered 10M voxels at 120 fps with greedy meshing"]),
    ] + [Project(name=f"Side Project {i}", tech=["TypeScript"], bullets=["Shipped a small web tool"]) for i in range(9)]
    db.save_profile(Profile(
        first_name="Alex", last_name="Rivera", email="alex.rivera@example.test", phone="555-0134",
        city="San Luis Obispo", state="CA", country="United States",
        linkedin="https://www.linkedin.com/in/example", github="https://github.com/example",
        headline="Software engineer focused on data systems",
        summary="New-grad engineer who builds reliable data pipelines and quantitative tooling.",
        skills=["Python", "SQL", "Go", "Kubernetes", "Pandas", "React", "C++", "Airflow", "Postgres", "Rust",
                "TypeScript", "Docker", "Spark", "XGBoost", "R", "Stata"],
        education=[Education(school="Example State University", degree="B.S. Computer Science", start="Sep 2022",
                             end="Jun 2026", gpa="3.8")],
        experience=experience, projects=projects, awards=["Dean's List, six quarters", "ICPC regional finalist"],
        custom_facts={"Home city": "San Luis Obispo, CA", "Languages spoken": "English, Spanish"},
    ))
    # Synthetic bank rows let the Resume Bank page show its real switch, main
    # resume, and remove controls without reading any candidate files.
    main_resume = config.RESUME_LIBRARY / "Alex_Rivera_Main.pdf"
    profile = db.get_profile()
    profile.resume_path = str(main_resume)
    db.save_profile(profile)
    db.add_resume_file(str(main_resume), "Alex Rivera Main", "general")
    db.add_resume_file(str(config.RESUME_LIBRARY / "Alex_Rivera_Software.pdf"), "SWE", "auto")
    data_resume_id = db.add_resume_file(str(config.RESUME_LIBRARY / "Alex_Rivera_Data.pdf"), "Data Science", "auto")
    db.add_resume_file(str(config.RESUME_LIBRARY / "Alex_Rivera_Financial.pdf"), "Financial Analyst", "auto")
    db.update_resume_file(data_resume_id, enabled=False)
    db.set_settings({
        "setup_complete": True, "auto_apply": True, "dry_run": False, "appearance": "System",
        "resume_strategy": "library",
        "target_titles": ["Software Engineer", "Data Engineer", "Quantitative Developer"],
        "preferred_locations": ["San Francisco, CA", "New York, NY", "Remote"],
        # A fictional priority employer, so its prepared application shows the star.
        "priority_companies": [*config.PRIORITY_COMPANIES, "Northwind Labs"],
        "agent": {"backend": "claude_cli", "model": ""},
        "email_verification": {"enabled": False, "imap_host": "imap.gmail.com", "imap_port": 993,
                               "imap_username": "alex.rivera@example.test", "sender_address": ""},
        "answers": {**db.get_settings()["answers"], "work_authorized_us": "Yes", "requires_sponsorship": "No",
                    "confirmed_fields": ["work_authorized_us", "requires_sponsorship"],
                    "field_answers": {"Are you willing to work in person?": "Yes"}},
    })
    jobs = []
    for index, (company, title, location, low, high, _score) in enumerate(COMPANIES):
        slug = company.lower().replace(" ", "")
        jobs.append(Job(
            source="greenhouse", ats="greenhouse", company=company, title=title, location=location,
            remote="Remote" in location, salary_min=low, salary_max=high,
            url=f"https://job-boards.greenhouse.io/{slug}/jobs/{4000 + index}",
            discovered_at=(now - timedelta(hours=index * 3)).isoformat(timespec="seconds"),
            posted_at=(now - timedelta(hours=index * 7 + 1, minutes=index * 13)).isoformat(timespec="seconds"),
        ))
    for index in range(40):
        jobs.append(Job(source="simplify", ats="lever", company=f"Studio {index + 1}", title="Software Engineer",
                        location="Remote", url=f"https://jobs.lever.co/studio{index}/{index:04d}",
                        posted_at=(now - timedelta(days=index % 9)).date().isoformat() if index % 3 else None))
    db.upsert_jobs(jobs)
    scores = {job.id: (float(COMPANIES[i][5]) if i < len(COMPANIES) else 30.0 - i * 0.3,
                       {"title_fit": 30, "skills_fit": 18, "location": 10, "freshness": 8})
              for i, job in enumerate(jobs)}
    db.update_scores(scores)
    by_company = {job.company: job for job in jobs}
    statuses = {"Bluebird": "applied", "Example": "needs_human", "Pinecrest": "needs_human",
                "Keystone Fintech": "skipped", "Vantage": "failed", "Cobalt Systems": "dry_run"}
    applications = [
        ("Bluebird", "applied", 1, [], "", "Thank-you page confirmed.\nFollow up with the recruiter Friday."),
        ("Example", "needs_human", 0, ["Current location (required)", "Earliest start date (required)"],
         "Required or invalid fields need attention: location, start date", ""),
        ("Pinecrest", "needs_human", 1, ["Email code"], "Verification code requested after Submit", ""),
        ("Vantage", "failed", 0, [], "The form requires an account login", ""),
        ("Cobalt Systems", "dry_run", 0, ["LinkedIn profile (optional)"], "", "Review the tailored resume before applying."),
    ]
    with db.conn() as con:
        for company, status in statuses.items():
            con.execute("UPDATE jobs SET status=? WHERE id=?", (status, by_company[company].id))
        for offset, (company, status, attempted, unanswered, error, note) in enumerate(applications):
            started = (now - timedelta(hours=offset * 5 + 1)).isoformat(timespec="seconds")
            finished = (now - timedelta(hours=offset * 5)).isoformat(timespec="seconds")
            if status == "dry_run":
                started = (now - timedelta(minutes=3)).isoformat(timespec="seconds")
                finished = (now - timedelta(minutes=2)).isoformat(timespec="seconds")
            log = [f"{started} Opened {by_company[company].url}", f"{started} Filled 14 fields from saved facts",
                   f"{finished} Result: {status}"]
            con.execute(
                "INSERT INTO applications(job_id,status,method,started_at,finished_at,log,unanswered,error,"
                "candidate_note,submission_attempted,resume_path) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (by_company[company].id, status, "engine", started, finished, json.dumps(log),
                 json.dumps(unanswered), error, note, attempted, str(config.TAILORED / "resume.pdf")))
    # Submitted applications at different hiring stages, for the Tracker.
    tracked = [("Harbor Analytics", "interview", 2), ("Lumen Health", "assessment", 4), ("Tidewater", "rejected", 9),
               ("Granite Cloud", "applied", 1), ("Orchard", "offer", 12), ("Arcadia Robotics", "applied", 3)]
    with db.conn() as con:
        for company, stage, days in tracked:
            job = by_company[company]
            when = (now - timedelta(days=days)).isoformat(timespec="seconds")
            con.execute("UPDATE jobs SET status='applied' WHERE id=?", (job.id,))
            con.execute(
                "INSERT INTO applications(job_id,status,method,started_at,finished_at,log,submission_attempted,stage,"
                "stage_source,stage_updated_at,candidate_note) VALUES (?,?,?,?,?,?,1,?,?,?,?)",
                (job.id, "applied", "engine", when, when, "[]", stage, "email" if stage != "applied" else "",
                 (now - timedelta(days=max(0, days - 1))).isoformat(timespec="seconds"),
                 "Recruiter: Sam" if stage == "interview" else ""))
    db.set_job_star(by_company["Meridian Pay"].id, True)
    # Prepared applications waiting for review.
    drafts = [
        ("Northwind Labs", "ready", [
            ("Why do you want to work at Northwind Labs?", "textarea", [], True,
             "I build data pipelines like the Airflow work at Northbeam, and Northwind's platform team runs the "
             "systems I want to grow into.", "luna", False),
            ("Are you legally authorized to work in the United States?", "select", ["Yes", "No"], True, "Yes", "saved", True),
            ("Do you have experience with Kubernetes?", "select", ["Yes", "No"], False, "Yes", "luna", False),
            ("First Name", "text", [], True, "Alex", "profile", True),
            ("Resume/CV", "file", [], True, "Resume for this role", "file", True),
        ]),
        ("Meridian Pay", "needs_answers", [
            ("What is your desired salary?", "text", [], True, None, "none", False),
            ("Tell us about a project you are proud of", "textarea", [], True,
             "I wrote an order book simulator in C++ that processes a million messages per second.", "luna", False),
        ]),
        ("Summit Bio", "approved", [("First Name", "text", [], True, "Alex", "profile", True)]),
    ]
    for company, status, questions in drafts:
        job = by_company[company]
        rows = [{"id": f"q{i + 1}", "label": label, "kind": kind, "options": options, "required": required,
                 "name": "", "answer": answer, "source": source, "approved": approved,
                 "note": "Northbeam internship and skills" if source == "luna" else ""}
                for i, (label, kind, options, required, answer, source, approved) in enumerate(questions)]
        db.upsert_draft(job.id, status=status, resume_path=str(config.TAILORED / "resume.pdf"),
                        resume_note="One-page resume generated from your resume builder",
                        questions=rows, question_source="greenhouse_form")
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

    temporary = tempfile.TemporaryDirectory(prefix="starployment-screens-")
    os.environ["JOBHUB_DATA_DIR"] = str(Path(temporary.name) / "data")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["QT_SCALE_FACTOR"] = str(args.scale)
    os.environ["JOBHUB_REDUCED_MOTION"] = "1"

    from PySide6.QtWidgets import QApplication, QPushButton

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
        shots = (("dashboard", 0), ("jobs", 1), ("applications", 2), ("tracker", 3), ("profile", 4),
                 ("resume-bank", 4), ("builder", 4), ("settings", 5))
        for page, index in shots:
            if page not in pages:
                continue
            window.nav.setCurrentRow(index)
            if page == "jobs":
                window.jobs.table.selectRow(1)
            if page == "applications":
                window.apps.list.setCurrentRow(0)
            if page == "profile":
                window.profile.show_tab("details")
            if page == "resume-bank":
                window.profile.show_tab("files")
            if page == "builder":
                window.profile.show_tab("builder")
            for _ in range(5):
                app.processEvents()
            path = output / f"{page}-{appearance.lower()}.png"
            window.grab().save(str(path))
            written.append(path)
            if page == "resume-bank":
                more = next((widget for widget in window.profile.files.findChildren(QPushButton)
                             if widget.toolTip() == "More" and widget.menu()
                             and any(action.text() == "Set as main resume" for action in widget.menu().actions())), None)
                if more:
                    menu = more.menu()
                    menu.adjustSize()
                    menu.show()
                    for _ in range(3):
                        app.processEvents()
                    menu_path = output / f"resume-bank-menu-{appearance.lower()}.png"
                    menu.grab().save(str(menu_path))
                    written.append(menu_path)
                    menu.close()
        if "apply" in pages:
            from jobhub.desktop.views.dashboard import ApplyPlanSheet
            sheet = ApplyPlanSheet(db.get_settings(), 412, window, counts=db.fresh_counts())
            sheet.show()
            for _ in range(5):
                app.processEvents()
            path = output / f"apply-{appearance.lower()}.png"
            sheet.grab().save(str(path))
            written.append(path)
            sheet.close()
        if "setup" in pages:
            wizard = SetupWizard(db.get_profile().model_dump(), db.get_settings(), window)
            wizard.resize(860, 700)
            wizard.show()
            for step in (0, 2, 3, 4):
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
