"""Send-path robustness found by auditing real forms: hosted posting links, more URL shapes and
field types, superseded attempts, and practice runs that predict a real send. All local fakes."""
from __future__ import annotations

import json

import pytest

from jobhub import db, prepare, scheduler
from jobhub.apply.adapters import adapter_for
from jobhub.models import Job, Profile, board_of
from jobhub.sources.import_job import JobImportError, parse_job_url


def careers_page_job():
    return Job(source="greenhouse", ats="greenhouse", company="Example", title="Software Engineer",
               url="https://www.example.test/careers/positions/8247303?gh_jid=8247303",
               apply_url="https://job-boards.greenhouse.io/example/jobs/8247303")


def test_posting_refresh_uses_the_hosted_board_link(store, monkeypatch):
    job = careers_page_job()
    db.upsert_jobs([job])
    assert scheduler.hosted_posting_url(job) == job.apply_url
    fetched = []
    monkeypatch.setattr("jobhub.sources.import_job.fetch_job", lambda url: fetched.append(url) or job)
    refreshed = scheduler.enrich_job(db.get_job(job.id))
    assert fetched == [job.apply_url] and refreshed.id == job.id


def test_more_public_board_links_are_supported():
    assert parse_job_url("https://job-boards.eu.greenhouse.io/agency/jobs/4738026101").slug == "agency"
    assert board_of("https://job-boards.eu.greenhouse.io/agency/jobs/4738026101") == ("greenhouse", "agency")
    assert adapter_for(Job(source="g", company="A", title="T", url="https://job-boards.eu.greenhouse.io/agency/jobs/1"))
    spaced = parse_job_url("https://jobs.ashbyhq.com/Superhuman%20Platform%20Inc/"
                           "134c282c-2837-44a8-9f7c-74ca39486490/application?embed=true")
    assert spaced.slug == "Superhuman Platform Inc"
    for url in ("https://jobs.ashbyhq.com/%20lead/134c282c-2837-44a8-9f7c-74ca39486490",
                "https://job-boards.greenhouse.io.evil.test/a/jobs/1",
                "https://jobs.lever.co/acme%20co/134c282c-2837-44a8-9f7c-74ca39486490"):
        with pytest.raises(JobImportError):
            parse_job_url(url)


def test_url_and_university_fields_are_read_ahead():
    assert prepare._ASHBY_KINDS["Url"] == "text"
    assert prepare._LEVER_KINDS["university"] == "combobox"


def test_a_confirmed_send_hides_earlier_attempts_of_the_same_posting(store):
    job = careers_page_job()
    db.upsert_jobs([job])
    first = db.create_application(job.id, "engine")
    db.finish_application(first, "needs_human", [], [], ["Graduation Date"], "Required fields")
    second = db.create_application(job.id, "engine")
    db.mark_submission_attempted(job.id)
    db.finish_application(second, "applied", [], [], [], submission_attempted=True)
    rows = {row["id"]: row for row in db.list_applications()}
    assert rows[first]["hidden"] == 1 and rows[second]["hidden"] == 0
    assert db.hide_superseded_attempts() == 0                # nothing left to repair


def test_startup_repair_and_the_list_show_one_row_per_posting(store):
    from jobhub.desktop.views.applications import latest_attempts

    job = careers_page_job()
    db.upsert_jobs([job])
    stopped = db.create_application(job.id, "engine")
    db.finish_application(stopped, "needs_human", [], [], [], "Stopped")
    with db.conn() as c:   # a confirmed record from before this version: nothing hid the old attempt
        c.execute("INSERT INTO applications(job_id, status, method, started_at, finished_at, submission_attempted) "
                  "VALUES (?, 'applied', 'engine', '2026-10-04T00:00:00+00:00', '2026-10-04T00:00:00+00:00', 1)",
                  (job.id,))
    assert [r["status"] for r in latest_attempts(db.list_applications())] == ["applied"]
    assert db.hide_superseded_attempts() == 1
    assert db.get_application(stopped)["hidden"] == 1


# ------------------------------------------------------------------ reading forms ahead
def test_lever_cards_with_a_greater_than_sign_are_read():
    card = '{"text":"Campus Questionnaire","fields":[{"type":"dropdown","text":"GPA > 3.0?","required":true,' \
           '"options":[{"text":"Yes"},{"text":"No"}]}]}'
    page = ('<form><input type="hidden" value="' + card.replace('"', "&quot;") +
            '" name="cards[abc][baseTemplate]"><select name="opportunityLocationId"><option value="">Select...'
            '</option><option value="1">San Francisco, CA</option><option value="2">Seattle, WA</option></select>')
    assert [json.loads(raw)["fields"][0]["text"] for raw in prepare.lever_cards(page)] == ["GPA > 3.0?"]
    assert prepare.lever_location_choices(page) == ["San Francisco, CA", "Seattle, WA"]


def test_greenhouse_demographic_consent_is_read_ahead(monkeypatch):
    payload = {"company_name": "Example Co", "questions": [], "demographic_questions": {"questions": []},
               "data_compliance": [{"type": "gdpr", "demographic_data_consent_applies": True}]}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: payload})()

    monkeypatch.setattr(prepare, "client", lambda **kw: Client())
    questions = prepare.greenhouse_questions(careers_page_job())
    assert questions[-1]["label"] == ("By checking this box, I consent to Example Co collecting, storing, and "
                                      "processing my responses to the demographic data surveys above.")
    assert questions[-1]["kind"] == "checkbox" and questions[-1]["required"]


def test_saved_eligibility_answers_tick_yes_no_boxes(store):
    db.set_settings({"answers": {**db.get_settings()["answers"], "work_authorized_us": "Yes",
                                 "confirmed_fields": ["work_authorized_us"]}})
    question = prepare._question(1, "Are you legally authorized to work in the United States?", "checkbox",
                                 ["Yes", "No"], True)
    prepare.answer_questions([question], Profile(first_name="Ada"), db.get_settings(), careers_page_job())
    assert question["answer"] == ["Yes"] and question["source"] == "saved"


# ------------------------------------------------------------------ answers for one approved send
def test_approved_yes_ticks_an_acknowledgement_but_a_saved_yes_never_does():
    from jobhub.answers import Q, rule_answer
    box = Q("I acknowledge the arbitration agreement", "checkbox")
    saved = {"answers": {"field_answers": {box.label: "Yes"}}}
    assert rule_answer(box, Profile(), saved, None) is None
    approved = {"answers": {"field_answers": {box.label: "Yes"}, "approved_answers": {box.label: "Yes"}}}
    assert rule_answer(box, Profile(), approved, None).value is True


def test_aliases_that_agree_are_one_answer_and_conflicting_ones_none():
    from jobhub.answers import Q, _custom_answer
    box = Q("Acknowledgement - I agree", "checkbox", name="I agree")
    assert _custom_answer(box, {"Acknowledgement - I agree": True, "I agree": True}).value is True
    assert _custom_answer(box, {"Acknowledgement - I agree": True, "I agree": False}) is None


def test_send_answers_cover_lists_and_lone_boxes_but_the_bank_stays_per_option():
    group = {"label": "Clearances", "kind": "checkbox", "options": ["None", "Secret"]}
    assert prepare.send_answers(group, ["Secret"])["Clearances"] == ["Secret"]
    assert "Clearances" not in prepare.expand_answer(group, ["Secret"])
    lone = {"label": "Privacy policy", "kind": "checkbox", "options": ["Acknowledge/Confirm"]}
    assert prepare.send_answers(lone, ["Acknowledge/Confirm"])["Acknowledge/Confirm"] is True
    assert "Acknowledge/Confirm" not in prepare.expand_answer(lone, ["Acknowledge/Confirm"])


# ------------------------------------------------------------------ new rules
def test_contact_preferences_dates_and_education_rules():
    from datetime import date

    from jobhub.answers import Q, rule_answer
    from jobhub.models import Education, Experience
    profile = Profile(education=[Education(school="Example University", degree="B.S. Computer Science",
                                           start="Sep 2022", end="May 2026")],
                      experience=[Experience(company="Northbeam", title="Intern", end="Sep 2025")])
    settings = {"answers": {}}

    def ask(label, kind="text", options=(), **extra):
        answer = rule_answer(Q(label, kind, list(options), **extra), profile, settings, None)
        return answer.value if answer else None

    assert ask("Receive text message updates about this application", "radio", ["Yes", "No"]) == "No"
    assert ask("We would like to contact you via SMS or WhatsApp", "combobox") == "No"
    assert ask("Date", name="eeo[disabilitySignatureDate]") == date.today().strftime("%m/%d/%Y")
    assert ask("Education History - Education Still Student?", "checkbox") is False
    assert ask("Education Start Date", "select", ["January", "September"]) == "September"
    assert ask("Education Start Date", "select", ["2026", "2022"]) == "2022"
    assert ask("Education End Date", "select", ["2026", "2025"]) == "2026"
    assert ask("Company name", id="company-name-0") == "Northbeam"
    assert ask("Title", id="title-0") == "Intern"
    assert ask("Company name") is None                     # no current role and no employment block


def test_uploads_the_app_cannot_attach_are_flagged_before_sending(store, make_pdf):
    from jobhub.answers import missing_answer
    from jobhub.tailor import cover_letter_pdf
    questions = [prepare._question(1, "Please upload your transcript", "file", [], True),
                 prepare._question(2, "Cover Letter", "file", [], True),
                 prepare._question(3, "Resume/CV", "file", [], True)]
    prepare.answer_questions(questions, Profile(first_name="Ada"), db.get_settings(), careers_page_job())
    assert [missing_answer(q) for q in questions] == [True, False, False]
    assert questions[1]["source"] == "file" and "cover letter" in questions[1]["answer"].casefold()
    assert prepare.draft_status(questions) == "needs_answers"
    pdf = cover_letter_pdf(Profile(first_name="Ada", last_name="Example", skills=["Python"]), careers_page_job())
    assert pdf.endswith(".pdf") and open(pdf, "rb").read(4) == b"%PDF"


def test_acknowledgements_mentioning_demographics_are_still_answered():
    from jobhub import luna
    ack = {"kind": "checkbox", "options": ["I Acknowledge"],
           "label": "We consider resumes only for employment history, never age, race, or gender."}
    assert luna._acknowledgement(ack)
    assert not luna._acknowledgement({"kind": "checkbox", "options": ["Male", "Female"], "label": "Gender"})
