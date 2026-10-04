"""Regressions from an Ashby form (checkbox groups, a date picker) that could not be sent.

A failed send used to add each box of a "select all that apply" group as its own
required question, wipe AI Autofill answers, and leave a date picker invalid.
All employers, agents, and browsers here are local fakes.
"""
import json

import pytest

from jobhub import db, prepare, scheduler
from jobhub.answers import form_date, missing_answer, us_date
from jobhub.models import Education, Job, Profile

GROUP = "What type of work are you most interested in? Select 2."
WORK = ["Applied AI (NY, SF)", "Backend (SF)", "Data (SF)", "Open to Anything "]


@pytest.fixture
def candidate(store, make_pdf, monkeypatch):
    resume = make_pdf(store / "resumes" / "main.pdf", ["Ada Example"])
    db.save_profile(Profile(first_name="Ada", last_name="Example", email="ada@example.test", resume_path=str(resume),
                            education=[Education(school="Example University", degree="B.S. Computer Science",
                                                 end="May 2027")]))
    db.set_settings({"setup_complete": True, "agent": {"backend": "claude_cli", "model": ""}})
    monkeypatch.setattr(scheduler, "enrich_job", lambda job: job)
    job = Job(source="ashby", ats="ashby", company="Example AI", title="Software Engineer, New Grad",
              url="https://jobs.ashbyhq.com/example/0e5c4b0a-1234-4cde-8f00-123456789abc", score=90)
    db.upsert_jobs([job])
    return job


def form():
    questions = [
        prepare._question(1, "Graduation Date", "text", [], True, "grad"),
        prepare._question(2, GROUP, "checkbox", WORK, True, "work"),
        prepare._question(3, "Why Example AI?", "textarea", [], True),
        prepare._question(4, "I agree to the privacy notice", "checkbox", [], True),
    ]
    questions[0]["format"] = "date"
    return questions


# ------------------------------------------------------------------ parsing and the missing check
def test_dates_become_what_a_date_picker_accepts():
    assert us_date("May 2027") == "05/01/2027" and form_date("May 2027")[1] is False
    assert us_date("Expected graduation: May 2027") == "05/01/2027"
    assert us_date("2027-05-14") == us_date("5/14/2027") == us_date("May 14, 2027") == "05/14/2027"
    assert form_date("05/14/2027")[1] is True
    assert us_date("Spring 2027") is None and us_date("Present") is None and us_date("13/40/2027") is None


def test_required_acknowledgement_must_be_ticked():
    box = {"kind": "checkbox", "options": [], "required": True, "answer": False}
    assert missing_answer(box) and missing_answer(dict(box, answer=None)) and not missing_answer(dict(box, answer=True))
    assert not missing_answer(dict(box, required=False))
    assert missing_answer({"kind": "checkbox", "options": ["A"], "required": True, "answer": []})
    assert not missing_answer({"kind": "file", "required": True, "answer": None})


# ------------------------------------------------------------------ option copies
def test_option_copies_of_a_group_are_dropped_but_acknowledgements_stay():
    questions = form() + [prepare._question(9, "Open to Anything", "checkbox", [], True),
                          prepare._question(10, f"{GROUP} - Backend (SF)", "checkbox", [], True)]
    kept = [q["label"] for q in prepare.drop_option_duplicates(questions)]
    assert kept == [q["label"] for q in form()]
    assert prepare._question(1, "x", "checkbox", ["Open to Anything "])["options"] == ["Open to Anything"]
    assert prepare.expand_answer({"label": GROUP, "kind": "checkbox", "options": WORK},
                                 ["Open to Anything "])[f"{GROUP} - Open to Anything"] is True


def test_drafts_from_earlier_versions_are_repaired(candidate):
    questions = form()
    questions[0]["answer"], questions[0]["source"] = "05/01/2027", "profile"
    questions[1].update(answer=WORK[:2], source="autofill")
    questions[2].update(answer="Because of the product.", source="autofill")
    questions[3].update(answer=True, source="you")
    copies = [dict(prepare._question(5 + i, option, "checkbox", [], True), answer=None) for i, option in enumerate(WORK)]
    draft_id = db.upsert_draft(candidate.id, status="needs_answers", questions=questions + copies)
    assert prepare.repair_drafts() == 1
    after = db.get_draft(draft_id)
    assert len(after["questions"]) == 4 and after["status"] == "ready"
    assert prepare.repair_drafts() == 0


def test_failed_send_keeps_autofill_answers_and_adds_no_option_copies(candidate, monkeypatch):
    monkeypatch.setattr(prepare, "discover_questions", lambda job: (form(), "ashby_form"))
    monkeypatch.setattr("jobhub.luna.complete", lambda prompt, settings, timeout=0: json.dumps({"answers": [
        {"id": "q2", "answer": WORK[:2], "assumed": True, "note": "backend experience"},
        {"id": "q3", "answer": "Because Example AI builds tools I use.", "note": "projects"},
        {"id": "q4", "answer": "true", "note": "acknowledgement"}]}))
    prepare.prepare_batch(1, 0)
    draft = db.list_drafts()[0]
    by_label = {q["label"]: q for q in draft["questions"]}
    assert by_label["Graduation Date"]["answer"] == "05/01/2027"      # exactly what the picker receives
    assert by_label[GROUP]["source"] == "autofill"
    prepare.approve_and_queue(draft["id"])

    def fake_apply(job_id, **kwargs):
        db.start_application(kwargs["application_id"], job_id)
        reported = [{"label": option.strip(), "kind": "checkbox", "required": True} for option in WORK]
        reported += [{"label": f"{GROUP} - Data (SF)", "kind": "checkbox", "required": True},
                     {"label": "Portfolio password", "kind": "text", "required": True}]
        db.finish_application(kwargs["application_id"], "needs_human", [], [], [], "Required fields",
                              questions=reported)
        return {"status": "needs_human", "submission_attempted": False, "message": "Required fields",
                "questions": reported}

    monkeypatch.setattr(scheduler, "apply_one", fake_apply)
    prepare.send_draft(draft["id"])
    after = {q["label"]: q for q in db.get_draft(draft["id"])["questions"]}
    assert set(after) == {q["label"] for q in form()} | {"Portfolio password"}
    assert after[GROUP]["answer"] == WORK[:2] and after[GROUP]["source"] == "autofill"
    assert after["Why Example AI?"]["source"] == "autofill"
    assert db.get_draft(draft["id"])["status"] == "needs_answers"   # only the genuinely new question


def test_profile_saves_keep_autofill_answers(candidate):
    questions = form()
    questions[2].update(answer="Drafted by AI Autofill.", source="autofill", approved=False)
    draft_id = db.upsert_draft(candidate.id, status="needs_answers", questions=questions)
    prepare.reanswer_open_drafts()
    after = {q["label"]: q for q in db.get_draft(draft_id)["questions"]}
    assert after["Why Example AI?"]["answer"] == "Drafted by AI Autofill."


def test_ashby_date_fields_are_marked(monkeypatch):
    payload = {"data": {"jobPosting": {"applicationForm": {"sections": [{"fieldEntries": [
        {"isRequired": True, "field": {"type": "Date", "title": "Graduation Date", "path": "d"}},
        {"isRequired": True, "field": {"type": "MultiValueSelect", "title": GROUP, "path": "w",
                                        "selectableValues": [{"label": option} for option in WORK]}}]}]}}}}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: payload})()

    monkeypatch.setattr(prepare, "client", lambda **kw: Client())
    job = Job(source="a", ats="ashby", company="A", title="T",
              url="https://jobs.ashbyhq.com/a/0e5c4b0a-1234-4cde-8f00-123456789abc")
    date, group = prepare.ashby_questions(job)
    assert date["format"] == "date" and date["kind"] == "text"
    assert group["options"][-1] == "Open to Anything"


# ------------------------------------------------------------------ AI Autofill
def test_autofill_can_leave_a_single_checkbox_unticked(candidate, monkeypatch):
    from jobhub import luna

    prompts = []

    def fake(prompt, settings, timeout=0):
        prompts.append(prompt)
        return json.dumps({"answers": [{"id": "q1", "answer": "false"}, {"id": "q2", "answer": "true"}]})

    monkeypatch.setattr(luna, "complete", fake)
    questions = [prepare._question(1, "Data (SF)", "checkbox", [], False),
                 prepare._question(2, "I agree to the privacy notice", "checkbox", [], True)]
    out = luna.autofill_answers(candidate, db.get_profile(), db.get_settings(), questions)
    assert out["q1"]["answer"] is False and out["q2"]["answer"] is True
    assert "single checkbox (true or false)" in prompts[0]
    assert "select all that apply" not in prompts[0].split("QUESTIONS")[-1]
