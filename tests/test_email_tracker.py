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


@pytest.mark.parametrize("subject, body", [
    ("Follow-up on your application to Northwind",
     "Thank you for your interest. Unfortunately, we have decided not to move forward with your candidacy at this time."),
    ("Important information about your application",
     "We appreciate your effort in applying. While another candidate has been offered the role, we thank you."),
    ("Update on Northwind Software Engineer II Role",
     "At this time we have decided to move forward in the process with other candidates."),
    ("Thank you for your interest in Northwind",
     "We have decided to move forward with candidates whose background and experience are a better match."),
    ("Thanks for applying to Northwind",
     "Unfortunately, due to your responses on one or more questions, you do not qualify for employment at this time."),
    ("Your application at Northwind",
     "After carefully reviewing your application, we’ve decided not to move forward at this stage."),
])
def test_common_rejection_wording(subject, body):
    assert email_tracker.classify(subject, body)[0] == "rejected"


@pytest.mark.parametrize("subject, body", [
    ("Thanks for applying!", "We have successfully received your application. Interview Phase: if your qualifications "
                             "align, we will reach out to schedule an interview."),
    ("Thank you for applying to Northwind!", "We’ve received your application. If it's not the right fit at this "
                                             "time, we appreciate your interest. If we decide not to move forward, we'll tell you."),
])
def test_acknowledgements_describing_the_process_stay_applied(subject, body):
    assert email_tracker.classify(subject, body)[0] == "applied"


def test_matching_tolerates_punctuation_and_board_names():
    apps = [{"id": 1, "company": "Alarm.Com", "title": "Software Engineer II", "finished_at": "2026-09-01"},
            {"id": 2, "company": "Batoncorporation", "title": "Engineer", "finished_at": "2026-09-01"},
            {"id": 3, "company": "Toshiba Global Commerce Solutions - External", "title": "Developer",
             "finished_at": "2026-09-01"},
            {"id": 4, "company": "General Matter", "title": "Engineer", "finished_at": "2026-09-01"}]
    match = email_tracker.match_application
    assert match("recruiting_team@alarm.com", "Update on Alarm.com Software Engineer II Role", "", apps)["id"] == 1
    assert match("Baton Corporation Ltd Hiring Team no-reply@ashbyhq.com", "Thanks for applying", "", apps)["id"] == 2
    assert match("no-reply@us.greenhouse-mail.io", "Thank you for applying to Toshiba Global Commerce Solutions",
                 "", apps)["id"] == 3
    assert match("news@example.test", "General update", "", apps) is None


def test_a_message_read_differently_later_is_applied_again(store):
    northwind = submitted("Northwind Labs", "Software Engineer", "https://job-boards.greenhouse.io/nw/jobs/1")
    mail = message("Northwind Labs <talent@northwindlabs.com>", "Follow-up on your application to Northwind Labs",
                   "Unfortunately, we have decided not to move forward.", "<r@x>")
    key = email_tracker._message_key(mail, b"1")
    # An earlier, weaker rule recorded this rejection as an acknowledgement.
    db.record_mail_event(message_key=key, received_at="2026-09-30T10:00:00+00:00", sender="Northwind Labs",
                         subject=mail["Subject"], stage="applied", application_id=northwind, confidence=0.5)
    report = email_tracker.sync({"email_tracking": {"lookback_days": 30}}, messages=[(b"1", mail, mail.get_content())])
    assert [u["stage"] for u in report.updates] == ["rejected"]
    assert {row["id"]: row["stage"] for row in db.list_tracker()}[northwind] == "rejected"
    events = db.list_mail_events()
    assert len(events) == 1 and events[0]["stage"] == "rejected"


class _FlakyIMAP:
    """A fake IMAP session whose first connection drops after a few header batches."""
    opened = 0

    def __init__(self, total):
        self.total = total
        type(self).opened += 1
        self.flaky = type(self).opened == 1

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b" ".join(str(n).encode() for n in range(1, self.total + 1))]
        uids = args[0].split(b",")
        if self.flaky and b"5" in uids:
            raise TimeoutError("The read operation timed out")
        rows = [(b"%s (UID %s BODY[HEADER.FIELDS (FROM SUBJECT)] {10}" % (u, u),
                 b"From: friend@example.test\r\nSubject: Lunch " + u + b"\r\n\r\n") for u in uids]
        return "OK", rows

    def logout(self):
        pass


def test_inbox_read_resumes_after_a_dropped_connection(monkeypatch):
    monkeypatch.setattr(email_tracker, "_HEADER_BATCH", 2)
    monkeypatch.setattr(email_tracker.time, "sleep", lambda _: None)
    _FlakyIMAP.opened = 0
    box = email_tracker.Mailbox.__new__(email_tracker.Mailbox)
    monkeypatch.setattr(box, "open", lambda: _FlakyIMAP(8), raising=False)
    subjects = [str(mail["Subject"]) for _, mail, _ in box.recent(30)]
    assert subjects == [f"Lunch {n}" for n in range(8, 0, -1)]   # every message once, newest first
    assert _FlakyIMAP.opened == 2


@pytest.mark.parametrize("subject, body, stage", [
    # 2K's acknowledgement (2026-10-06) names the next stage without inviting the candidate to it.
    ("Thanks for applying to the Northwind Technical Art Graduate Program",
     "Thanks for applying to the Technical Art Graduate Program. What happens next? You will hear from us by email "
     "either way. If your application moves forward, the next stage is a technical assessment you take online.",
     "applied"),
    ("Thanks for applying to Northwind", "We received your application. The process includes an online assessment "
                                         "and interviews with the team.", "applied"),
    ("Thanks for applying to Northwind", "We received your application. Please complete the HackerRank assessment "
                                         "within 7 days.", "assessment"),
    ("Thanks for applying to Northwind", "Thanks for applying! We'd like to schedule a call. Share your "
                                         "availability for next week.", "interview"),
])
def test_acknowledgements_advance_only_on_a_real_invitation(subject, body, stage):
    assert email_tracker.classify(subject, body)[0] == stage


@pytest.mark.parametrize("subject, body, stage", [
    # Akuna-style acknowledgements (2026-10) name HackerRank while describing the process.
    ("Thank you for applying to Northwind Capital!",
     "Thank you for applying to Northwind Capital! We have received your application for Software Engineer. "
     "Our team will review it. Many of our roles include a HackerRank coding challenge as part of the process.",
     "applied"),
    ("Thank you for your interest in Northwind Capital!",
     "Thank you for your interest in Northwind Capital. As part of our process, you will receive a HackerRank "
     "coding challenge by email within 48 hours.", "applied"),
    ("Thank you for applying to Northwind Capital!",
     "We received your application. Candidates who advance complete a CodeSignal assessment.", "applied"),
    # The invitation itself, from the testing platform or the employer.
    ("Your HackerRank 2026 - 2027 Quant Coding Challenge (Python) Invitation",
     "Northwind Capital has invited you to take the Quant Coding Challenge. Click the link below to begin.",
     "assessment"),
    ("Next Steps with Northwind Capital: HackerRank Coding Challenge",
     "Thank you for your interest in Northwind Capital! We'd like to invite you to complete our HackerRank coding "
     "challenge. You will receive a separate email from HackerRank with your link.", "assessment"),
    # Future tense never hides a rejection or an offer.
    ("Your application", "Thank you for applying. Unfortunately, you will not be moving forward in our process.",
     "rejected"),
])
def test_platform_names_in_acknowledgements_are_not_invitations(subject, body, stage):
    assert email_tracker.classify(subject, body)[0] == stage


def test_a_stage_email_can_be_opened_from_its_application(store):
    from jobhub.desktop.format import email_link
    app_id = submitted("Northwind Labs", "Software Engineer", "https://job-boards.greenhouse.io/nw/jobs/1")
    mail = message("Northwind Labs <talent@northwindlabs.com>", "Interview with Northwind Labs",
                   "We would like to schedule an interview.", "<interview-1@northwindlabs.test>")
    email_tracker.sync({"email_tracking": {"lookback_days": 30}}, messages=[(b"1", mail, mail.get_content())])
    record = next(a for a in db.list_applications() if a["id"] == app_id)
    assert record["stage"] == "interview" and record["stage_mail_message_id"] == "interview-1@northwindlabs.test"
    gmail = email_link(record, {"imap_host": "imap.gmail.com", "imap_username": "ada@example.test"})
    assert gmail == ("https://mail.google.com/mail/u/ada@example.test/#search/"
                     "rfc822msgid%3Ainterview-1%40northwindlabs.test")
    assert email_link(record, {"imap_host": "imap.example.test"}) == \
        "message://%3Cinterview-1%40northwindlabs.test%3E"


def test_emails_recorded_before_message_ids_were_kept_get_them_on_the_next_check(store):
    app_id = submitted("Northwind Labs", "Software Engineer", "https://job-boards.greenhouse.io/nw/jobs/1")
    mail = message("Northwind Labs <talent@northwindlabs.com>", "Interview with Northwind Labs",
                   "We would like to schedule an interview.", "<old@northwindlabs.test>")
    email_tracker.sync({"email_tracking": {"lookback_days": 30}}, messages=[(b"1", mail, mail.get_content())])
    with db.conn() as c:
        c.execute("UPDATE mail_events SET message_id=''")
    email_tracker.sync({"email_tracking": {"lookback_days": 30}}, messages=[(b"1", mail, mail.get_content())])
    record = next(a for a in db.list_applications() if a["id"] == app_id)
    assert record["stage_mail_message_id"] == "old@northwindlabs.test"


def test_a_corrected_reading_undoes_the_stage_it_set(store):
    northwind = submitted("Northwind Labs", "Technical Artist", "https://job-boards.greenhouse.io/nw/jobs/1")
    mail = message("Northwind Labs <no-reply@northwindlabs.test>", "Thanks for applying to Northwind Labs",
                   "Thanks for applying! If your application moves forward, the next stage is a technical "
                   "assessment you take online.", "<ack@x>")
    key = email_tracker._message_key(mail, b"1")
    # An earlier rule read this acknowledgement as an assessment and advanced the application.
    db.record_mail_event(message_key=key, received_at="2026-10-06T19:46:23+00:00", sender="Northwind Labs",
                         subject=mail["Subject"], stage="assessment", application_id=northwind, confidence=0.6)
    db.set_application_stage(northwind, "assessment", "email")
    email_tracker.sync({"email_tracking": {"lookback_days": 30}}, messages=[(b"1", mail, mail.get_content())])
    assert {row["id"]: row["stage"] for row in db.list_tracker()}[northwind] == "applied"
    assert db.list_mail_events()[0]["stage"] == "applied"
    # A stage the candidate chose is left alone.
    db.set_application_stage(northwind, "interview", "manual")
    assert db.restage_from_mail(northwind) is None


def _feed(*mails):
    return [(str(index).encode(), mail, mail.get_content()) for index, mail in enumerate(mails, 1)]


def test_archives_only_matched_rejections_whose_application_is_rejected(store):
    northwind = submitted("Northwind Labs", "Software Engineer", "https://job-boards.greenhouse.io/nw/jobs/1")
    bluebird = submitted("Bluebird", "Data Engineer", "https://job-boards.greenhouse.io/bb/jobs/2")
    db.set_application_stage(bluebird, "offer", "manual")    # the candidate's own record wins over an email
    feed = _feed(
        message("Northwind Labs <talent@northwindlabs.test>", "Update on your Northwind Labs application",
                "Unfortunately, we have decided not to move forward.", "<nw-reject@x>"),
        message("Bluebird <jobs@bluebird.test>", "Your Bluebird application",
                "Unfortunately, we have decided not to move forward.", "<bb-reject@x>"),
        message("Someone <hr@unknown.test>", "Your application", "Unfortunately, we will not be moving forward.",
                "<unknown-reject@x>"),
        message("Northwind Labs <talent@northwindlabs.test>", "Thanks for applying to Northwind Labs",
                "We received your application.", "<nw-ack@x>"))
    archived = []

    def archiver(items):
        archived.extend(items)
        return len(items)

    off = email_tracker.sync({"email_tracking": {"lookback_days": 30}}, messages=feed, archiver=archiver)
    assert off.archived == 0 and archived == []                  # off unless the candidate turns it on
    on = {"email_tracking": {"lookback_days": 30, "archive_rejections": True}}
    report = email_tracker.sync(on, messages=feed, archiver=archiver)
    assert archived == [(b"1", "nw-reject@x")] and report.archived == 1
    assert {row["id"]: row["stage"] for row in db.list_tracker()}[northwind] == "rejected"
    assert "archived 1 rejection email" in report.summary


def test_an_archive_failure_never_fails_the_inbox_check(store):
    submitted("Northwind Labs", "Software Engineer", "https://job-boards.greenhouse.io/nw/jobs/1")
    feed = _feed(message("Northwind Labs <talent@northwindlabs.test>", "Northwind Labs update",
                         "Unfortunately, we have decided not to move forward.", "<nw@x>"))

    def broken(_items):
        raise RuntimeError("This mailbox has no Archive folder")

    report = email_tracker.sync({"email_tracking": {"lookback_days": 30, "archive_rejections": True}},
                                messages=feed, archiver=broken)
    assert report.updates and report.archived == 0 and "no Archive folder" in report.archive_error


class _WritableIMAP:
    """A fake mailbox for archiving: records every command; nothing touches a network."""

    def __init__(self, gmail=True, headers=None, folders=()):
        self.capabilities = ("IMAP4REV1", "X-GM-EXT-1") if gmail else ("IMAP4REV1", "MOVE")
        self.headers, self.folders, self.commands = headers or {}, folders, []

    def login(self, *_args):
        return "OK", []

    def select(self, name, readonly=False):
        self.commands.append(("SELECT", name, readonly))
        return "OK", [b"3"]

    def list(self):
        return "OK", list(self.folders)

    def uid(self, command, uid, *args):
        self.commands.append((command, uid, *args))
        if command == "FETCH":
            return "OK", [(b"1 (UID %s BODY[HEADER.FIELDS (MESSAGE-ID)])" % uid,
                           b"Message-ID: <%s>\r\n\r\n" % self.headers.get(uid, b"other@x"))]
        return "OK", []

    def logout(self):
        pass


def _archiving_box(monkeypatch, fake):
    box = email_tracker.Mailbox.__new__(email_tracker.Mailbox)
    box.host, box.port, box.username, box.password = "imap.example.test", 993, "ada@example.test", "app-password"
    monkeypatch.setattr(email_tracker.imaplib, "IMAP4_SSL", lambda *a, **k: fake)
    return box


def test_gmail_archive_labels_and_leaves_the_inbox_after_checking_message_ids(monkeypatch):
    fake = _WritableIMAP(headers={b"7": b"reject@x"})
    box = _archiving_box(monkeypatch, fake)
    assert box.archive([(b"7", "reject@x"), (b"8", "moved-since@x")]) == 1
    writes = [c for c in fake.commands if c[0] not in ("SELECT", "FETCH")]
    assert writes == [("STORE", b"7", "+X-GM-LABELS", '("Starployment/Rejected")'),
                      ("STORE", b"7", "-X-GM-LABELS", "(\\Inbox)")]        # no deletes, no expunge
    assert not any("Deleted" in str(c) or c[0] == "EXPUNGE" for c in fake.commands)


def test_other_servers_move_to_their_own_archive_folder_or_stop(monkeypatch):
    fake = _WritableIMAP(gmail=False, headers={b"7": b"reject@x"},
                         folders=[b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Archive) "/" "Archive"'])
    assert _archiving_box(monkeypatch, fake).archive([(b"7", "reject@x")]) == 1
    assert ("MOVE", b"7", '"Archive"') in fake.commands
    bare = _WritableIMAP(gmail=False, folders=[b'(\\HasNoChildren) "/" "INBOX"'])
    with pytest.raises(RuntimeError, match="no Archive folder"):
        _archiving_box(monkeypatch, bare).archive([(b"7", "reject@x")])


class _Inbox:
    """A fake read-only IMAP session over a few fictional emails that counts full downloads."""
    bodies: list[bytes] = []

    def __init__(self, mails):
        self.mails = mails

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b" ".join(self.mails)]
        uids, what = args[0].split(b","), args[1]
        if what == "(BODY.PEEK[])":
            type(self).bodies.append(uids[0])
            return "OK", [(b"%s (UID %s BODY[] {1}" % (uids[0], uids[0]), self.mails[uids[0]].as_bytes())]
        rows = [(b"%s (UID %s BODY[HEADER.FIELDS] {1}" % (u, u),
                 b"".join(b"%s: %s\r\n" % (k.encode(), str(v).encode()) for k, v in self.mails[u].items()) + b"\r\n")
                for u in uids]
        return "OK", rows

    def logout(self):
        pass


def test_mail_read_and_set_aside_is_not_downloaded_again(store, monkeypatch):
    northwind = submitted("Northwind Labs", "Software Engineer", "https://job-boards.greenhouse.io/nw/jobs/1")
    news = message("Example Weekly <news@example.test>", "Product update from Example Weekly",
                   "Our newest features this month.", "<n@x>")
    rejection = message("Northwind Labs <talent@northwindlabs.com>", "Update on your Northwind Labs application",
                        "Unfortunately, we have decided not to move forward.", "<r@x>")
    mails = {b"1": news, b"2": rejection}
    box = email_tracker.Mailbox.__new__(email_tracker.Mailbox)
    monkeypatch.setattr(box, "open", lambda: _Inbox(mails), raising=False)
    monkeypatch.setattr(email_tracker, "Mailbox", lambda _settings: box)
    settings = {"email_tracking": {"lookback_days": 30}}

    _Inbox.bodies = []
    report = email_tracker.sync(settings)
    assert sorted(_Inbox.bodies) == [b"1", b"2"] and report.scanned == 2
    assert {row["id"]: row["stage"] for row in db.list_tracker()}[northwind] == "rejected"

    _Inbox.bodies = []
    email_tracker.sync(settings)
    assert _Inbox.bodies == []   # the newsletter and the rejection already linked to its application

    _Inbox.bodies = []
    email_tracker.sync({"email_tracking": {"lookback_days": 30, "use_luna": False,
                                           "archive_rejections": True}}, archiver=lambda items: 0)
    assert _Inbox.bodies == [b"2"]   # a rejection still waiting to be archived is read again
    monkeypatch.setattr(email_tracker, "RULES_VERSION", email_tracker.RULES_VERSION + 1)
    _Inbox.bodies = []
    email_tracker.sync(settings)
    assert sorted(_Inbox.bodies) == [b"1", b"2"]   # new rules read everything again


class _StallingIMAP(_FlakyIMAP):
    """Every connection drops after one more header batch; or, with ``stuck``, before any."""
    stuck = False

    def __init__(self, total):
        super().__init__(total)
        self.batches = 0

    def uid(self, command, *args):
        if command != "SEARCH":
            if type(self).stuck or self.batches:
                raise TimeoutError("The read operation timed out")
            self.batches += 1
        self.flaky = False
        return super().uid(command, *args)


def test_a_slow_read_that_keeps_making_progress_is_not_given_up(monkeypatch):
    monkeypatch.setattr(email_tracker, "_HEADER_BATCH", 2)
    monkeypatch.setattr(email_tracker.time, "sleep", lambda _: None)
    _StallingIMAP.opened, _StallingIMAP.stuck = 0, False
    box = email_tracker.Mailbox.__new__(email_tracker.Mailbox)
    monkeypatch.setattr(box, "open", lambda: _StallingIMAP(10), raising=False)
    assert len(list(box.recent(30))) == 10   # five drops, more than the per-stall budget of three
    assert _StallingIMAP.opened > email_tracker._RECONNECTS + 1


def test_an_unreachable_mail_server_is_reported_as_offline(monkeypatch):
    monkeypatch.setattr(email_tracker.time, "sleep", lambda _: None)
    _StallingIMAP.opened, _StallingIMAP.stuck = 0, True
    box = email_tracker.Mailbox.__new__(email_tracker.Mailbox)
    monkeypatch.setattr(box, "open", lambda: _StallingIMAP(4), raising=False)
    with pytest.raises(email_tracker.MailUnavailable) as raised:
        list(box.recent(30))
    assert str(raised.value).startswith(email_tracker.OFFLINE)
    assert _StallingIMAP.opened == email_tracker._RECONNECTS + 1


# Wording from real assessment invitations the rules once missed (2026-10), with fictional employers.
@pytest.mark.parametrize("subject, body", [
    ("Northwind invites you to take an assessment",
     "As part of their evaluation process, you have been invited by Northwind to complete an assessment. "
     "Start Assessment. This link is exclusively yours."),
    ("[Litmus] Assessment invitation from Northwind",
     "You've been invited to complete an assessment from Northwind, due Nov 1, 2026 at 11:59 PM PT."),
    ("Congratulations, you're on to the next step with Northwind!",
     "Thank you for taking the time to apply for the Summer Analyst position at Northwind. We partner with an "
     "online pre-recruiting assessment provider. Please complete this pre-recruiting assessment within the next "
     "48 hours. This is a required step in your application process."),
    ("Next step in your application with Northwind",
     "As a next step, we ask you to take two short assessments: an adaptive cognitive test and a personality "
     "questionnaire. Start now."),
    ("[Action Required] Your Northwind Assessments Expire in 24 hours",
     "Quick reminder that your assessments expire in 24 hours. Robots (25 minutes): Incomplete."),
    ("Next Steps with Northwind: HackerRank Coding Challenge",
     "Thank you for your interest in Northwind! We're happy to move forward with the process for the Junior "
     "Researcher role, which is an online coding assessment via the HackerRank platform."),
])
def test_assessment_invitations_in_their_many_wordings(subject, body):
    assert email_tracker.classify(subject, body)[0] == "assessment"


@pytest.mark.parametrize("subject, body", [
    ("Thank you for applying to Northwind!",
     "Thank you for applying! As we review your application, here's a quick overview of what to expect next:"
     "\r\n\r\n* Complete our Assessments: If your profile meets our basic qualifications, you'll receive a link."),
    ("Thanks for your interest in joining the team at Northwind",
     "Thanks for applying. In the meantime, we recommend that you take our products for a test spin."),
    ("Hiring now: Software Engineer at Northwind", "Your job alert. View job: https://example.test/?ref=bryqwoven"),
])
def test_descriptions_and_product_tests_are_not_assessments(subject, body):
    assert email_tracker.classify(subject, body)[0] != "assessment"


def test_matching_ignores_mailbox_words_and_spacing_and_picks_the_named_role():
    apps = [{"id": 1, "company": "Reply", "title": "Data Engineer", "finished_at": "2026-10-01"},
            {"id": 2, "company": "Eightsleep", "title": "Security Engineer", "finished_at": "2026-10-02"},
            {"id": 3, "company": "Akuna Capital", "title": "Junior Quantitative Risk Analyst", "finished_at": "2026-10-05"},
            {"id": 4, "company": "Akuna Capital University", "title": "Junior Quantitative Researcher",
             "finished_at": "2026-10-01"}]
    match = email_tracker.match_application
    assert match("Hiring Team DO-NOT-REPLY@criteriacorp.com", "Next step in your application with Eight Sleep",
                 "Any questions, just reply.", apps)["id"] == 2
    assert match("no-reply@akunacapital.com", "Next Steps with Akuna Capital: HackerRank Coding Challenge",
                 "We're happy to move forward with the process for the Junior Quantitative Researcher role", apps)["id"] == 4


def test_unmatched_emails_name_their_employer():
    hint = email_tracker.employer_hint
    assert hint("Coderbyte <do-not-reply@coderbyte.com>", "Northwind AI invites you to take an assessment") == "Northwind AI"
    assert hint("noreply@litmus.build", "[Litmus] Assessment invitation from Bluebird") == "Bluebird"
    assert hint("Madeleine Example <m@northwind.test>", "Northwind | Reminder to complete your assessment") in (
        "Madeleine Example", "Northwind")


def test_luna_reads_only_unsettled_emails_in_one_batch_and_never_twice(store, monkeypatch):
    from jobhub import luna
    northwind = submitted("Northwind Labs", "Software Engineer", "https://job-boards.greenhouse.io/nw/jobs/1")
    monkeypatch.setattr(email_tracker, "_luna_ready", lambda settings: True)
    calls: list[list[dict]] = []

    def fake(emails, settings):
        calls.append(emails)
        return [{"stage": "assessment", "company": "Northwind Labs"}, {"stage": None, "company": ""}][:len(emails)]

    monkeypatch.setattr(luna, "classify_emails", fake)
    mails = [
        message("Talent <hello@talentportal.test>", "Your next step: candidate portal",
                "Your application stands out among other candidates. Your next task is in the portal.", "<a@x>"),
        message("Portal <hello@talentportal.test>", "Application portal update",
                "Your application status changed. We decided not to share details by email.", "<b@x>"),
        message("Northwind Labs <talent@northwindlabs.com>", "Northwind Labs interview",
                "We'd like to schedule a phone screen.", "<c@x>"),    # the rules settle this one
    ]
    feed = [(str(i).encode(), mail, mail.get_content()) for i, mail in enumerate(mails)]
    settings = {"email_tracking": {"lookback_days": 30, "use_luna": True}, "agent": {"backend": "claude_cli"}}
    email_tracker.sync(settings, messages=feed)
    assert len(calls) == 1 and len(calls[0]) == 2          # one call for both unsettled emails
    assert "candidate portal" in calls[0][0]["subject"]
    stages = {row["id"]: row["stage"] for row in db.list_tracker()}
    assert stages[northwind] == "interview"                # Luna never demotes; interview stands
    events = {e["subject"]: e for e in db.list_mail_events()}
    assert events["Your next step: candidate portal"]["stage"] == "assessment"
    email_tracker.sync(settings, messages=feed)
    assert len(calls) == 1                                  # each email is read by Luna once


def test_a_failed_luna_reading_keeps_the_rules_reading(store, monkeypatch):
    from jobhub import luna
    monkeypatch.setattr(email_tracker, "_luna_ready", lambda settings: True)
    monkeypatch.setattr(luna, "classify_emails", lambda emails, settings: (_ for _ in ()).throw(RuntimeError("down")))
    mail = message("Hiring <jobs@bluebird.test>", "Bluebird coding challenge",
                   "Please complete the HackerRank assessment within 7 days.", "<a@x>")
    report = email_tracker.sync({"email_tracking": {"lookback_days": 30, "use_luna": True}, "agent": {}},
                                messages=[(b"1", mail, mail.get_content())])
    assert report.recruiting == 1 and report.unmatched[0]["stage"] == "assessment"
    assert db.unmatched_stage_mail(days=36500)[0]["employer"] == "Bluebird"   # fixtures are dated 2026-09-30


def test_scheduled_checks_wait_for_a_fuller_luna_batch_but_manual_checks_do_not(store, monkeypatch):
    from datetime import datetime, timezone
    from email.utils import format_datetime
    from jobhub import luna
    monkeypatch.setattr(email_tracker, "_luna_ready", lambda settings: True)
    calls: list[int] = []
    monkeypatch.setattr(luna, "classify_emails", lambda emails, settings: calls.append(len(emails)) or [None] * len(emails))
    mail = message("Talent <hello@talentportal.test>", "Your next step: candidate portal",
                   "Your application stands out among other candidates. Your next task is in the portal.", "<a@x>")
    mail.replace_header("Date", format_datetime(datetime.now(timezone.utc)))    # just arrived
    feed = [(b"1", mail, mail.get_content())]
    settings = {"email_tracking": {"lookback_days": 30, "use_luna": True}, "agent": {"backend": "claude_cli"}}
    email_tracker.sync(settings, messages=feed)
    assert calls == []                       # one fresh email waits for a fuller batch
    email_tracker.sync(settings, messages=feed, luna_now=True)
    assert calls == [1]


def test_job_alerts_are_never_application_updates(store):
    mail = message("LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>", "Application Developer at Northwind",
                   "Northwind is hiring. Schedule an interview with top candidates fast.", "<a@x>")
    report = email_tracker.sync({"email_tracking": {"lookback_days": 30}}, messages=[(b"1", mail, mail.get_content())])
    assert report.recruiting == 0 and db.list_mail_events() == []
    db.record_mail_event(message_key=email_tracker._message_key(mail, b"1"), received_at="2026-09-30T10:00:00+00:00",
                         sender="LinkedIn Job Alerts", subject="Application Developer at Northwind", stage="interview",
                         application_id=None, confidence=0.5)              # as older rules read it
    email_tracker.sync({"email_tracking": {"lookback_days": 30}}, messages=[(b"1", mail, mail.get_content())])
    assert db.list_mail_events() == [] and db.unmatched_stage_mail(days=36500) == []


def test_an_email_stays_with_the_application_sent_before_it_arrived(store):
    """Generic emails once moved to each newer application at the same employer, undoing stages."""
    first = submitted("Northwind", "Data Analyst", "https://jobs.ashbyhq.com/northwind/1")
    mail = message("Coderbyte <do-not-reply@coderbyte.com>", "Northwind invites you to take an assessment",
                   "You have been invited by Northwind to complete an assessment.", "<a@x>")
    mail.replace_header("Date", "Wed, 28 Oct 2099 10:00:00 +0000")     # after the first application
    feed = [(b"1", mail, mail.get_content())]
    email_tracker.sync({"email_tracking": {"lookback_days": 30}}, messages=feed)
    later = submitted("Northwind", "Platform Engineer", "https://jobs.ashbyhq.com/northwind/2")
    with db.conn() as c:   # sent after the email arrived
        c.execute("UPDATE applications SET finished_at='2099-10-29T10:00:00+00:00' WHERE id=?", (later,))
    for _ in range(3):
        email_tracker.sync({"email_tracking": {"lookback_days": 30}}, messages=feed)
    stages = {row["id"]: row["stage"] for row in db.list_tracker()}
    assert stages[first] == "assessment" and stages[later] == "applied"


class _CountingInbox(_Inbox):
    """Reports a UIDVALIDITY and counts header fetches."""
    header_fetches: list[list[bytes]] = []

    def response(self, code):
        return code, [b"7"]

    def uid(self, command, *args):
        if command == "FETCH" and args[1] != "(BODY.PEEK[])":
            type(self).header_fetches.append(args[0].split(b","))
        return super().uid(command, *args)


def test_settled_mail_is_not_even_fetched_again(store, monkeypatch):
    submitted("Northwind Labs", "Software Engineer", "https://job-boards.greenhouse.io/nw/jobs/1")
    news = message("Example Weekly <news@example.test>", "Ten tips for better sleep", "Sleep well.", "<n@x>")
    invite = message("Northwind Labs <talent@northwindlabs.com>", "Interview with Northwind Labs",
                     "We would like to schedule an interview.", "<i@x>")
    unknown = message("Acme Recruiting <jobs@acme.test>", "Your application to Acme",
                      "Thank you for applying to Acme.", "<u@x>")
    mails = {b"1": news, b"2": invite, b"3": unknown}
    monkeypatch.setattr(email_tracker, "_HEADERS", {})
    box = email_tracker.Mailbox.__new__(email_tracker.Mailbox)
    box.host, box.username = "imap.example.test", "candidate@example.test"
    monkeypatch.setattr(box, "open", lambda: _CountingInbox(mails), raising=False)
    monkeypatch.setattr(email_tracker, "Mailbox", lambda _settings: box)
    settings = {"email_tracking": {"lookback_days": 30, "use_luna": False}}
    _CountingInbox.bodies, _CountingInbox.header_fetches = [], []
    first = email_tracker.sync(settings)
    assert first.scanned == 3 and sorted(_CountingInbox.bodies) == [b"2", b"3"]
    _CountingInbox.bodies, _CountingInbox.header_fetches = [], []
    again = email_tracker.sync(settings)
    assert again.scanned == 3 and again.updates == []
    # Only the email no application matches yet is looked at again.
    assert _CountingInbox.header_fetches == [[b"3"]] and _CountingInbox.bodies == [b"3"]


def test_a_linked_email_stays_put_when_the_posting_is_renamed(store):
    """Endeavor and Netic: a scan renamed the employer, so every check moved the interview
    email to another application and back, posting a correction and an update each time."""
    first = submitted("Netic", "Forward Deployed Engineer", "https://jobs.ashbyhq.com/netic/1")
    second = submitted("Netic", "Software Engineer, Agent Platform", "https://jobs.ashbyhq.com/netic/2")
    mail = message("Netic Hiring Team <no-reply@ashbyhq.com>", "Netic interview: Forward Deployed Engineer",
                   "We would like to schedule an interview for the Forward Deployed Engineer role.", "<n@x>")
    feed = [(b"1", mail, mail.get_content())]
    settings = {"email_tracking": {"lookback_days": 30, "use_luna": False}}
    email_tracker.sync(settings, messages=feed)
    assert {row["id"]: row["stage"] for row in db.list_tracker()}[first] == "interview"
    with db.conn() as c:   # a later scan renamed the first posting's employer
        c.execute("UPDATE jobs SET company='Cybernetic Labs' WHERE id=(SELECT job_id FROM applications WHERE id=?)",
                  (first,))
    for _ in range(3):
        report = email_tracker.sync(settings, messages=feed)
        assert report.updates == []
    stages = {row["id"]: row["stage"] for row in db.list_tracker()}
    assert stages[first] == "interview" and stages[second] == "applied"
    assert not [e for e in db.list_events(limit=200) if "Inbox correction" in e["msg"]]


def test_a_sent_posting_keeps_the_name_it_was_sent_under(store):
    app_id = submitted("Netic", "Software Engineer, Agent Platform", "https://jobs.ashbyhq.com/netic/2")
    renamed = Job(source="ashby", ats="ashby", company="Cybernetic Labs", title="Software Engineer New Grad",
                  url="https://jobs.ashbyhq.com/netic/2", description="Now with a description")
    db.upsert_jobs([renamed])
    row = db.tracked_application(app_id)
    assert (row["company"], row["title"]) == ("Netic", "Software Engineer, Agent Platform")
    fresh = Job(source="ashby", ats="ashby", company="Netic", title="Data Analyst", url="https://jobs.ashbyhq.com/netic/3")
    db.upsert_jobs([fresh])
    db.upsert_jobs([fresh.model_copy(update={"company": "Cybernetic Labs"})])
    with db.conn() as c:   # a posting nobody applied to still takes the latest name
        assert c.execute("SELECT company FROM jobs WHERE url=?", (fresh.url,)).fetchone()[0] == "Cybernetic Labs"
