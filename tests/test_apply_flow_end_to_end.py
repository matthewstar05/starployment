"""The full Apply flow, end to end, with every employer replaced by local fixtures.

Prepare the top matches → review them in Applications (filterable) → approve
one with one click → real Chromium submits it to a local employer form, which
accepts only when Luna's approved answer and the generated resume arrive →
confirmation is recorded → an interview email advances the Tracker → reset
clears what was never sent. No employer, mailbox, or AI agent is contacted.
"""
from __future__ import annotations

import json
import os
import time
from email.message import EmailMessage

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("JOBHUB_REDUCED_MOTION", "1")

from jobhub import config, db, email_tracker, prepare, scheduler  # noqa: E402
import jobhub.apply as dispatcher  # noqa: E402
from jobhub.apply.adapters import Adapter  # noqa: E402
from jobhub.models import Education, Experience, Job, Profile, Project  # noqa: E402

LUNA_ANSWER = "I build reliable Python data pipelines, and Fixture Labs' platform team works on exactly that."

FORM = """<!doctype html><html><body><form data-expected="%s" onsubmit="event.preventDefault();
  const f=new FormData(this);
  const ok = f.get('first')==='Ada' && f.get('last')==='Example' && f.get('email')==='ada@example.test'
    && f.get('why')===this.dataset.expected && f.get('resume').size>1000;
  document.body.innerHTML = ok ? '<h1>Thank you for applying!</h1>' : '<h1>Invalid fixture data</h1>'">
  <label for="first">First Name</label><input id="first" name="first" required>
  <label for="last">Last Name</label><input id="last" name="last" required>
  <label for="email">Email</label><input id="email" name="email" type="email" required>
  <label for="why">Why do you want to work at Fixture Labs?</label><textarea id="why" name="why" required></textarea>
  <label for="resume">Resume</label><input id="resume" name="resume" type="file" required>
  <button type="submit">Submit application</button></form></body></html>"""


def wait_until(app, condition, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_prepare_review_approve_send_track_and_reset(store, make_pdf, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from jobhub.desktop.controller import DesktopController
    from jobhub.desktop.ui import JobHubWindow

    monkeypatch.setattr(config, "HEADLESS", True)
    resume = make_pdf(store / "resumes" / "ada.pdf", ["Ada Example", "ada@example.test"])
    db.save_profile(Profile(
        first_name="Ada", last_name="Example", email="ada@example.test", phone="5555550100",
        city="Springfield", state="IL", country="United States", resume_path=str(resume),
        skills=["Python", "SQL", "Airflow"],
        education=[Education(school="Example University", degree="B.S. Computer Science", end="May 2026")],
        experience=[Experience(company="Example Lab", title="Data Engineering Intern",
                               bullets=["Built Python pipelines that load 2M rows nightly"])],
        projects=[Project(name="Transit Forecasts", bullets=["Forecast delays with Python"])],
    ))
    db.set_settings({"setup_complete": True, "dry_run": False, "resume_strategy": "tailored",
                     "target_titles": ["Data Engineer"], "agent": {"backend": "claude_cli", "model": ""}})
    jobs = [Job(source="greenhouse", ats="greenhouse", company=name, title="Data Engineer",
                url=f"https://job-boards.greenhouse.io/{slug}/jobs/{100 + i}", description="Python pipelines")
            for i, (name, slug) in enumerate((("Fixture Labs", "fixture"), ("Second Co", "second"), ("Third Co", "third")))]
    db.upsert_jobs(jobs)
    db.update_scores({job.id: (90.0 - i, {}) for i, job in enumerate(jobs)})

    # The employer's published questions, and Luna's draft for the custom one.
    def discover(job):
        rows = [("First Name", "text"), ("Last Name", "text"), ("Email", "text"), ("Resume", "file"),
                (f"Why do you want to work at {job.company}?", "textarea")]
        return [prepare._question(i + 1, label, kind, [], True, "resume" if kind == "file" else "")
                for i, (label, kind) in enumerate(rows)], "greenhouse_form"

    monkeypatch.setattr(prepare, "discover_questions", discover)
    monkeypatch.setattr("jobhub.luna.complete",
                        lambda prompt, settings, timeout=0: json.dumps({"answers": [{"id": "q5", "answer": LUNA_ANSWER}]}))
    form = store / "employer.html"
    import html
    form.write_text(FORM % html.escape(LUNA_ANSWER, quote=True))
    monkeypatch.setattr(dispatcher, "adapter_for", lambda job: Adapter("local-fixture", form.as_uri(), settle_ms=0))
    monkeypatch.setattr(scheduler, "enrich_job", lambda job: job)

    app = QApplication.instance() or QApplication([])
    window = JobHubWindow()
    controller = DesktopController(window, scheduler_enabled=False, show_setup_on_start=False)
    try:
        # 1. Overview → Apply: prepare the top matches (Luna drafts the custom answers).
        window.dashboard.apply_plan_requested.emit({"count": 100, "min_score": 0, "one_per_company": True,
                                                    "use_luna": True})
        assert wait_until(app, lambda: not controller._preparing)
        drafts = db.list_drafts()
        assert {d["company"] for d in drafts} == {"Fixture Labs", "Second Co", "Third Co"}
        assert all(d["status"] == "ready" for d in drafts)

        # 2. Applications lists them, and the filter narrows to what needs review.
        controller.refresh(include_account=True)
        window.apps.show_filter("review")
        assert window.apps.list.count() == 3
        window.apps.show_filter("sent")
        assert window.apps.list.count() == 0
        window.apps.show_filter("review")
        window.apps.select_draft_for_job(jobs[0].id)
        detail = window.apps.draft_detail
        assert detail.draft["company"] == "Fixture Labs"
        luna_row = next(row for row in detail.rows if row.question["source"] == "autofill")
        assert luna_row.value() == LUNA_ANSWER

        # 3. One click: approve every answer and send.
        detail.approve.click()
        assert wait_until(app, lambda: db.draft_for_job(jobs[0].id)["status"] in ("sent", "unconfirmed", "error",
                                                                                 "needs_answers"))
        sent = db.draft_for_job(jobs[0].id)
        assert sent["status"] == "sent", sent
        application = db.get_application(sent["application_id"])
        assert application["status"] == "applied" and application["submission_attempted"] == 1
        assert any(name.endswith("success.png") for name in application["screenshots"])

        # 4. The Tracker shows it; an interview email from the employer advances it.
        controller.refresh(include_account=True)
        assert [row["company"] for row in window.tracker.rows] == ["Fixture Labs"]
        mails = []
        for index, (subject, body) in enumerate((
                ("Thank you for applying to Fixture Labs", "We have received your application."),
                ("Fixture Labs interview", "We'd like to schedule a phone screen. Share your availability."))):
            mail = EmailMessage()
            mail["From"], mail["To"] = "Fixture Labs Recruiting <talent@fixturelabs.test>", "ada@example.test"
            mail["Subject"], mail["Message-ID"] = subject, f"<e2e-{index}@fixturelabs.test>"
            mail["Date"] = "Thu, 01 Oct 2026 10:00:00 +0000"
            mail.set_content(body)
            mails.append((str(index).encode(), mail, body))
        report = email_tracker.sync({"email_tracking": {"lookback_days": 30, "use_luna": False}}, messages=mails)
        assert [u["stage"] for u in report.updates] == ["interview"]
        controller.refresh(include_account=True)
        assert window.tracker.rows[0]["stage"] == "interview"
        assert window.dashboard.focus_card.isVisibleTo(window.dashboard)

        # 5. Reset unfinished: the two unsent drafts go, the sent application stays.
        window.apps.reset_requested.emit(True)
        assert {d["status"] for d in db.list_drafts()} == {"sent"}
        assert {db.get_job(job.id).status for job in jobs[1:]} == {"new"}
        assert db.get_application(sent["application_id"])["status"] == "applied"
    finally:
        controller.stop()
        window.close()
