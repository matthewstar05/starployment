"""Workday Candidate Home tracking. The reader is a fake: nothing contacts an employer."""
import pytest

from jobhub import db, vault, workday_portal
from jobhub.models import Job


class FakeVault:
    def __init__(self):
        self.items = {}

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def get_password(self, service, username):
        return self.items.get((service, username))

    def delete_password(self, service, username):
        self.items.pop((service, username), None)


@pytest.fixture
def portal(store, monkeypatch):
    fake = FakeVault()
    monkeypatch.setattr(vault, "_vault", lambda: fake)
    monkeypatch.setattr(workday_portal.governor, "busy", lambda *a, **k: False)
    vault.save_account("bank", "ada@example.test", "S3cret!pass", company="Bank",
                       host="bank.wd5.myworkdayjobs.com", status="active")
    return store


def workday_attempt(title, requisition, status="needs_human", url=None):
    url = url or f"https://bank.wd5.myworkdayjobs.com/en-US/Careers/job/New-York/{title.replace(' ', '-')}_{requisition}"
    job = Job(source="workday", ats="workday", company="Bank", title=title, url=url)
    db.upsert_jobs([job])
    app_id = db.reserve_application(job.id)
    db.start_application(app_id, job.id)
    db.mark_submission_attempted(job.id)
    db.finish_application(app_id, status, [], [], [], submission_attempted=True)
    return app_id


def row(title, requisition, status="Application Received", draft=False, tasks=False):
    return {"postingTitle": title, "jobRequisitionId": requisition, "draft": draft, "status": status,
            "jobPostingAnchorId": f"{title.replace(' ', '-')}_{requisition}", "hasAwaitingTasks": tasks}


def tracked():
    return {r["id"]: (r["status"], r["stage"]) for r in db.list_tracker()}


def test_site_of_reads_both_workday_addresses():
    assert workday_portal.site_of("https://fmr.wd1.myworkdayjobs.com/en-US/FidelityCareers/job/Westlake-TX/"
                                  "Associate-Financial-Analyst_2137506") == (
        "fmr", "fmr.wd1.myworkdayjobs.com", "FidelityCareers", "Associate-Financial-Analyst_2137506")
    assert workday_portal.site_of("https://wd1.myworkdaysite.com/recruiting/fmr/FidelityCareers/job/Westlake-TX/"
                                  "Associate-Financial-Analyst_2137506/apply")[:3] == (
        "fmr", "fmr.wd1.myworkdayjobs.com", "FidelityCareers")
    assert workday_portal.site_of("https://jobs.ashbyhq.com/example/1") is None


@pytest.mark.parametrize("kind, status, tasks, stage", [
    ("active", "Application Received", False, "applied"),
    ("active", "Under Consideration", False, "applied"),
    ("active", "Under Consideration", True, "assessment"),
    ("active", "Interview Scheduled", False, "interview"),
    ("active", "Offer Extended", False, "offer"),
    ("inactive", "No Longer Under Consideration", False, "rejected"),
    ("inactive", "Position Filled", False, "rejected"),
    ("inactive", "Withdrawn", False, "withdrawn"),
])
def test_candidate_home_statuses_become_stages(kind, status, tasks, stage):
    assert workday_portal.stage_for(kind, {"status": status, "hasAwaitingTasks": tasks}) == stage
    assert workday_portal.stage_for(kind, {"status": "Not Submitted", "draft": True}) is None


def test_a_listed_submission_confirms_and_stages_and_reports_only_changes(portal):
    received = workday_attempt("Associate Financial Analyst", "2137506")      # Submit pressed, no page seen
    draft = workday_attempt("Quantitative Analyst", "2137324")                 # stopped on a question page
    rows = [("active", row("Associate Financial Analyst", "2137506")),
            ("active", row("Quantitative Analyst", "2137324", "Not Submitted", draft=True))]
    reads = []

    def reader(host, tenant, site, email, password):
        reads.append((host, tenant, site, email, password))
        return rows
    report = workday_portal.check({}, reader=reader)
    assert reads == [("bank.wd5.myworkdayjobs.com", "bank", "Careers", "ada@example.test", "S3cret!pass")]
    assert [c["application_id"] for c in report.confirmed] == [received]
    assert tracked()[received] == ("applied", "applied")
    assert tracked()[draft][0] == "needs_human"         # a draft never reached the employer
    # Within the check interval the employer is not read again; a manual check reads it,
    # and an unchanged Candidate Home reports nothing.
    assert workday_portal.check({}, reader=reader).checked == []
    with db.conn() as c:
        c.execute("UPDATE workday_portal_checks SET checked_at='2000-01-01T00:00:00+00:00'")
    again = workday_portal.check({}, manual=True, reader=reader)
    assert again.checked == ["bank"] and again.updates == [] and again.confirmed == []
    # A later reading that moves on is applied once.
    rows[0] = ("inactive", row("Associate Financial Analyst", "2137506", "No Longer Under Consideration"))
    with db.conn() as c:
        c.execute("UPDATE workday_portal_checks SET checked_at='2000-01-01T00:00:00+00:00'")
    later = workday_portal.check({}, reader=reader)
    assert [(u["application_id"], u["stage"]) for u in later.updates] == [(received, "rejected")]
    assert tracked()[received] == ("applied", "rejected")
    events = [e["msg"] for e in db.list_events(limit=50) if e["msg"].startswith("Workday")]
    assert sum("→ rejected" in msg for msg in events) == 1


def test_workday_never_overrides_the_candidates_stage_or_moves_back(portal):
    app_id = workday_attempt("Analyst", "R100", status="applied")
    db.set_application_stage(app_id, "interview", "manual")
    workday_portal.check({}, reader=lambda *a: [("active", row("Analyst", "R100", "Under Consideration"))])
    assert tracked()[app_id] == ("applied", "interview")


def test_settled_busy_or_signed_out_employers_are_not_read(portal):
    app_id = workday_attempt("Analyst", "R100", status="applied")
    calls = []

    def reader(*args):
        calls.append(args)
        return []
    db.set_application_stage(app_id, "rejected", "manual")
    assert workday_portal.check({}, reader=reader).checked == [] and calls == []
    with db.conn() as c:
        c.execute("UPDATE applications SET stage='applied' WHERE id=?", (app_id,))
    vault.set_status("bank", "needs_verification")
    assert workday_portal.check({}, reader=reader).checked == [] and calls == []
    vault.set_status("bank", "active")
    assert workday_portal.check({"workday": {"check_portal": False}}, reader=reader).checked == []
    assert workday_portal.check({}, reader=reader).checked == ["bank"]


def test_one_employers_failure_is_recorded_and_the_rest_go_on(portal):
    workday_attempt("Analyst", "R100")

    def reader(*args):
        raise workday_portal.PortalError("Workday asked for a security check")
    report = workday_portal.check({}, reader=reader)
    assert report.checked == [] and report.problems == ["bank: Workday asked for a security check"]
    assert db.portal_checked("bank")       # not retried on every cycle


def test_rows_match_by_requisition_and_titles_only_when_unambiguous(portal):
    exact = workday_attempt("Data Analyst", "R1")
    titled = workday_attempt("Risk Analyst", "R2", url="https://bank.wd5.myworkdayjobs.com/Careers/job/NY/Risk_X9")
    apps = db.workday_applications()
    pairs = workday_portal.match([("active", row("Data Analyst", "R1")), ("active", row("Risk Analyst", "R7"))], apps)
    assert {app["id"]: rec["jobRequisitionId"] for app, _, rec in pairs} == {exact: "R1", titled: "R7"}
