"""Greenhouse public-metadata preflight; no employer submissions or user DB."""
from pathlib import Path

from jobhub.models import Job, Profile
from jobhub import readiness


def job(url="https://job-boards.greenhouse.io/example/jobs/123") -> Job:
    return Job(source="greenhouse", ats="greenhouse", company="Example", title="Engineer", url=url)


def question(label, name, kind="input_text", *, required=True, values=None):
    return {"label": label, "required": required,
            "fields": [{"name": name, "type": kind, "values": values or []}]}


def candidate(tmp_path: Path) -> Profile:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.7\nfixture")
    return Profile(first_name="Ada", last_name="Lovelace", email="ada@example.com",
                   phone="555-0123", resume_path=str(resume), city="San Luis Obispo",
                   state="CA", country="United States")


def test_simple_required_form_is_ready_and_optional_unknowns_do_not_block(tmp_path):
    profile = candidate(tmp_path)
    payload = {"questions": [
        question("First Name", "first_name"), question("Last Name", "last_name"),
        question("Email", "email"), question("Phone", "phone"),
        {"label": "Resume/CV", "required": True, "fields": [
            {"name": "resume", "type": "input_file"},
            {"name": "resume_text", "type": "textarea"}]},
        question("Why this company?", "question_1", "textarea", required=False),
    ]}
    assert readiness.greenhouse_readiness_from_payload(payload, profile, {}, job()).status == "ready"


def test_unconfirmed_eligibility_blocks_but_exact_saved_answer_unblocks(tmp_path):
    profile = candidate(tmp_path)
    payload = {"questions": [question(
        "Are you legally authorized to work in the United States?", "question_3",
        "multi_value_single_select", values=[{"label": "Yes"}, {"label": "No"}])]}
    missing = readiness.greenhouse_readiness_from_payload(payload, profile, {}, job())
    assert missing.status == "missing_facts"
    assert missing.missing == ("Are you legally authorized to work in the United States?",)
    confirmed = {"answers": {"work_authorized_us": "Yes", "confirmed_fields": ["work_authorized_us"]}}
    assert readiness.greenhouse_readiness_from_payload(payload, profile, confirmed, job()).status == "ready"


def test_required_linkedin_can_be_supplied_by_candidate(tmp_path):
    profile = candidate(tmp_path)
    payload = {"questions": [question("LinkedIn Profile", "question_4")]}
    assert readiness.greenhouse_readiness_from_payload(payload, profile, {}, job()).status == "missing_facts"
    profile.linkedin = "https://www.linkedin.com/in/ada"
    assert readiness.greenhouse_readiness_from_payload(payload, profile, {}, job()).status == "ready"


def test_conditional_questions_are_unknown_and_consent_requires_review(tmp_path):
    profile = candidate(tmp_path)
    conditional = {"questions": [question("If yes, where did you work?", "question_5")]}
    assert readiness.greenhouse_readiness_from_payload(conditional, profile, {}, job()).status == "unknown"
    availability = {"questions": [question("When can you start?", "question_6")]}
    assert readiness.greenhouse_readiness_from_payload(availability, profile, {}, job()).status == "missing_facts"
    consent = {"questions": [], "data_compliance": [{"requires_processing_consent": True}]}
    result = readiness.greenhouse_readiness_from_payload(consent, profile, {}, job())
    assert result.status == "missing_facts"
    assert result.missing == ("Data processing consent",)


def test_greenhouse_preflight_uses_fixed_api_host_and_fails_open(tmp_path, monkeypatch):
    profile = candidate(tmp_path)
    calls = []
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"questions": [question("First Name", "first_name")]}
    class Http:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def get(self, url, *, params):
            calls.append((url, params))
            return Response()
    monkeypatch.setattr(readiness, "client", lambda **_: Http())
    assert readiness.preflight_job(job(), profile, {}).status == "ready"
    assert calls == [("https://boards-api.greenhouse.io/v1/boards/example/jobs/123",
                      {"questions": "true"})]
    unsafe = job("https://evil.example/jobs/123")
    assert readiness.preflight_job(unsafe, profile, {}).status == "unknown"
    assert len(calls) == 1
    class Broken(Http):
        def get(self, *_args, **_kwargs):
            raise OSError("network down")
    monkeypatch.setattr(readiness, "client", lambda **_: Broken())
    assert readiness.preflight_job(job(), profile, {}).status == "unknown"
