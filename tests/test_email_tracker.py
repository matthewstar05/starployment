"""Inbox classification, matching, and forward-only stage updates. No mailbox is opened."""
from email.message import EmailMessage

import pytest

from jobhub import db, email_tracker
from jobhub.models import Job


@pytest.mark.parametrize("subject, body, stage", [
    ("Your application to Northwind Labs", "Thank you for applying! We have received your application.", "applied"),
    ("Northwind Labs coding challenge", "Please complete the HackerRank assessment within 7 days.", "assessment"),
    ("Next steps", "As the next step in our interview process, complete this CodeSignal assessment.", "assessment"),
    ("Interview invitation", "We'd like to schedule a phone screen. Please share your availability for next week.", "interview"),
    ("Update on your application", "Thank you for your interest. Unfortunately, we have decided to move forward with other candidates.", "rejected"),
    ("Offer", "We are pleased to extend an offer of employment. Your offer letter is attached.", "offer"),
    ("Weekly newsletter", "Ten tips for better sleep.", None),
])
def test_classify(subject, body, stage):
    assert email_tracker.classify(subject, body)[0] == stage


def applications():
    return [{"id": 1, "company": "Northwind Labs, Inc.", "title": "Software Engineer", "finished_at": "2026-09-01"},
            {"id": 2, "company": "Bluebird", "title": "Data Engineer", "finished_at": "2026-09-02"},
            {"id": 3, "company": "Bluebird", "title": "Platform Engineer", "finished_at": "2026-09-03"}]


def test_matching_requires_a_clear_single_employer():
    apps = applications()
    assert email_tracker.match_application("Northwind Recruiting <jobs@northwindlabs.com>", "Interview",
                                           "Hi Ada", apps)["id"] == 1
    assert email_tracker.match_application("no-reply@greenhouse.io", "Your Bluebird application",
                                           "Data Engineer role", apps)["id"] == 2
    assert email_tracker.match_application("no-reply@greenhouse.io", "Your Bluebird application", "", apps)["id"] == 3
    assert email_tracker.match_application("friend@example.test", "Lunch?", "No employers here", apps) is None


def message(sender, subject, body, msg_id):
    mail = EmailMessage()
    mail["From"], mail["Subject"], mail["Message-ID"] = sender, subject, msg_id
    mail["Date"] = "Wed, 30 Sep 2026 10:00:00 +0000"
    mail.set_content(body)
    return mail


def submitted(company, title, url):
    job = Job(source="greenhouse", ats="greenhouse", company=company, title=title, url=url)
    db.upsert_jobs([job])
    app_id = db.reserve_application(job.id)
    db.start_application(app_id, job.id)
    db.mark_submission_attempted(job.id)
    db.finish_application(app_id, "applied", [], [], [], submission_attempted=True)
    return app_id


def test_sync_advances_stages_forward_only_and_stores_no_bodies(store):
    northwind = submitted("Northwind Labs", "Software Engineer", "https://job-boards.greenhouse.io/nw/jobs/1")
    bluebird = submitted("Bluebird", "Data Engineer", "https://job-boards.greenhouse.io/bb/jobs/2")
    db.set_application_stage(bluebird, "rejected", "manual")
    mails = [
        (b"1", message("Northwind Labs <talent@northwindlabs.com>", "Interview with Northwind Labs",
                       "We would like to schedule an interview. SECRET-BODY-TEXT", "<a@x>"), None),
        (b"2", message("Northwind Labs <talent@northwindlabs.com>", "Northwind Labs assessment",
                       "Please complete the HackerRank coding challenge.", "<b@x>"), None),
        (b"3", message("Bluebird <jobs@bluebird.test>", "Bluebird interview", "Schedule a phone screen", "<c@x>"), None),
        (b"4", message("Someone <a@b.test>", "Interview tips", "schedule an interview with yourself", "<d@x>"), None),
    ]
    feed = [(uid, mail, mail.get_content()) for uid, mail, _ in mails]
    report = email_tracker.sync({"email_tracking": {"lookback_days": 30, "use_luna": False}}, messages=feed)
    stages = {row["id"]: row["stage"] for row in db.list_tracker()}
    assert stages[northwind] == "interview"          # the later assessment email cannot move it back
    assert stages[bluebird] == "rejected"            # a manual close is never reopened by email
    assert [u["company"] for u in report.updates] == ["Northwind Labs"]
    assert len(report.unmatched) == 1
    events = db.list_mail_events()
    assert len(events) == 4
    assert all("SECRET-BODY-TEXT" not in str(event) for event in events)
    again = email_tracker.sync({"email_tracking": {"lookback_days": 30}}, messages=feed)
    assert again.updates == [] and len(db.list_mail_events()) == 4    # each message is processed once


def test_manual_tracking_and_stage_rules(store):
    app_id = db.add_manual_application(company="Orchard", title="iOS Engineer", location="Remote", salary_min=150000)
    row = db.list_tracker()[0]
    assert row["id"] == app_id and row["stage"] == "applied" and row["salary_min"] == 150000
    assert db.set_application_stage(app_id, "interview", "email")
    assert not db.set_application_stage(app_id, "assessment", "email")
    assert db.set_application_stage(app_id, "assessment", "manual")        # the candidate can always correct it
    with pytest.raises(ValueError):
        db.set_application_stage(app_id, "hired", "manual")


def test_providers_have_imap_hosts():
    assert email_tracker.PROVIDERS["gmail"][1] == "imap.gmail.com"
    assert email_tracker.provider_for("imap.gmail.com") == "gmail"
    assert email_tracker.provider_for("mail.example.test") == "other"
