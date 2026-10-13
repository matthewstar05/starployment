"""Public metadata, form schemas, and duplicate protection for guest ATS flows."""
import pytest

from jobhub import config, db, prepare, scheduler
from jobhub.apply.adapters import adapter_for
from jobhub.models import ApplyResult, Job, Profile, job_fingerprint
from jobhub.sources import import_job


@pytest.fixture(autouse=True)
def isolated_scheduler_state(monkeypatch):
    state = scheduler.State()
    state.running_scan = False
    state.running_apply = None
    state.running_applies = set()
    state.last_scan_at = None
    state.next_scan_at = None
    state.last_error = ""
    state.stop = False
    monkeypatch.setattr(scheduler, "STATE", state)


class Client:
    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


@pytest.mark.parametrize("ats,url,payload", [
    ("workable", "https://apply.workable.com/example/j/ABC123/apply/", {
        "shortcode": "ABC123", "state": "published", "title": "Engineer",
        "location": {"city": "Chicago", "region": "Illinois", "country": "United States"},
        "description": "<p>Build Python services</p>", "published": "2026-10-08T00:00:00Z"}),
    ("rippling", "https://ats.rippling.com/example/jobs/12345678-1234-4321-9876-123456789abc/apply", {
        "uuid": "12345678-1234-4321-9876-123456789abc", "name": "Engineer", "companyName": "Example",
        "workLocations": ["Chicago, IL"], "description": {"job": "<p>Build Python services</p>"},
        "createdOn": "2026-10-08T00:00:00Z"}),
])
def test_secondary_metadata_and_form_urls_keep_one_vacancy(monkeypatch, ats, url, payload):
    client = Client(payload)
    monkeypatch.setattr(import_job, "client", lambda **kwargs: client)
    job = import_job.fetch_job(url)
    assert job.ats == ats and job.title == "Engineer" and "Python" in job.description
    adapter = adapter_for(job)
    assert adapter is not None and adapter.name == ats
    assert "/apply/apply" not in adapter.apply_url
    again = job.model_copy(update={"url": adapter.apply_url, "apply_url": adapter.apply_url})
    assert job_fingerprint(again) == job_fingerprint(job)
    assert import_job.parse_job_url(adapter.apply_url).posting_id == import_job.parse_job_url(url).posting_id
    client.payload = {**payload, "shortcode": "different", "uuid": "different"}
    with pytest.raises(import_job.JobImportError, match="matches"):
        import_job.fetch_job(url)


@pytest.mark.parametrize("url", [
    "https://apply.workable.com/", "https://apply.workable.com/example",
    "https://apply.workable.com.evil.test/example/j/ABC123/",
    "https://ats.rippling.com/example/jobs/invalid",
    "https://ats.rippling.com.evil.test/example/jobs/12345678-1234-4321-9876-123456789abc",
])
def test_secondary_adapters_reject_other_hosts_and_board_pages(url):
    assert adapter_for(Job(source="test", company="Example", title="Engineer", url=url)) is None


def test_optional_workable_history_does_not_require_unopened_rows(monkeypatch):
    client = Client([{"fields": [
        {"id": "experience", "label": "Experience", "type": "group", "required": False,
         "fields": [{"id": "title", "label": "Title", "required": True}]},
        {"id": "firstname", "label": "First name", "required": True, "type": "text"},
        {"id": "choice", "label": "Choose", "required": True, "type": "choice",
         "options": [{"value": "42", "label": "Visible choice"}]},
    ]}])
    monkeypatch.setattr(prepare, "client", lambda **kwargs: client)
    job = Job(source="test", ats="workable", company="Example", title="Engineer",
              url="https://apply.workable.com/example/j/ABC123/")
    questions = prepare.workable_questions(job)
    assert [q["label"] for q in questions] == ["First name", "Choose"]
    assert questions[1]["options"] == ["Visible choice"]
    client.payload = {}
    with pytest.raises(ValueError, match="no fields"):
        prepare.workable_questions(job)


def test_rippling_custom_questions_keep_scope_and_choice_labels(monkeypatch):
    client = Client({"uuid": "12345678-1234-4321-9876-123456789abc", "activeJobApplication": {
        "basicQuestions": [{"oid": "resume", "fieldType": "FILE", "title": "Resume", "required": True}],
        "additionalQuestions": [
            {"id": "form1", "form": {"questions": [{"uniqueKey": "reused", "title": "Languages",
              "questionType": "MULTI_SELECT_CHECKBOX", "isRequired": True, "strChoices": ["Python", "Go"]}]}},
            {"id": "form2", "form": {"questions": [{"uniqueKey": "reused", "title": "AI experience",
              "questionType": "YES_NO_SCALE_4", "isRequired": True, "intChoices": [1, 2, 3, 4]}]}},
        ]}})
    monkeypatch.setattr(prepare, "client", lambda **kwargs: client)
    job = Job(source="test", ats="rippling", company="Example", title="Engineer",
              url="https://ats.rippling.com/example/jobs/12345678-1234-4321-9876-123456789abc")
    questions = prepare.rippling_questions(job)
    assert questions[0]["kind"] == "file" and questions[0]["required"]
    assert questions[1]["kind"] == "checkbox" and questions[1]["options"] == ["Python", "Go"]
    assert questions[1]["name"] != questions[2]["name"]
    assert questions[2]["options"] == ["Strong No", "No", "Yes", "Strong Yes"]


def _secondary_job(ats, company="Example", *, suffix="1", starred=None, score=95):
    if ats == "workable":
        url = f"https://apply.workable.com/{company.lower()}/j/{suffix}/"
    else:
        url = f"https://ats.rippling.com/{company.lower()}/jobs/12345678-1234-4321-9876-{suffix.zfill(12)}"
    return Job(source=ats, ats=ats, company=company, title="Engineer", location="United States",
               description="Python services", url=url, starred=starred, score=score,
               score_json={"auto_apply_eligible": True})


@pytest.mark.parametrize("ats", ["workable", "rippling"])
def test_scheduler_auto_apply_considers_secondary_providers(store, monkeypatch, ats):
    jobs = [_secondary_job(ats, suffix="1"), _secondary_job(ats, suffix="2", company="Other")]
    db.upsert_jobs(jobs)
    db.set_settings({"auto_apply": True, "setup_complete": True, "max_apply_per_run": 3})
    monkeypatch.setattr(scheduler, "rescore", lambda: None)
    monkeypatch.setattr(scheduler, "preflight_job", lambda *args: type("Ready", (), {"status": "ready"})())
    attempted = []
    monkeypatch.setattr(scheduler, "apply_one", lambda job_id, **kwargs:
                        attempted.append(job_id) or {"status": "dry_run"})

    results = scheduler.auto_apply_batch()

    assert len(results) == 2
    assert set(attempted) == {job.id for job in jobs}
    assert {db.get_job(job_id).ats for job_id in attempted} == {ats}


@pytest.mark.parametrize("ats", ["workable", "rippling"])
def test_secondary_confirmed_submit_is_durably_applied(store, monkeypatch, ats):
    resume = config.RESUMES / "candidate.pdf"
    resume.write_bytes(b"%PDF-1.7\nfixture")
    db.save_profile(Profile(first_name="Ada", last_name="Lovelace", email="ada@example.test",
                            resume_path=str(resume)))
    job = _secondary_job(ats)
    db.upsert_jobs([job])
    monkeypatch.setattr(scheduler, "enrich_job", lambda saved: saved)
    monkeypatch.setattr(scheduler, "resume_for_job", lambda *args: str(resume))

    def confirmed(*args, **kwargs):
        db.mark_submission_attempted(job.id)
        return ApplyResult(status="applied", message="Employer confirmation found",
                           submission_attempted=True)

    monkeypatch.setattr(scheduler, "apply_to_job", confirmed)
    result = scheduler.apply_one(job.id)

    record = db.get_application(result["application_id"])
    assert result["status"] == record["status"] == "applied"
    assert record["submission_attempted"] == 1
    assert db.get_job(job.id).status == "applied"
    with pytest.raises(ValueError, match="submission record"):
        db.reserve_application(job.id)


@pytest.mark.parametrize("ats", ["workable", "rippling"])
def test_secondary_uncertain_submit_is_durable_but_never_applied(store, monkeypatch, ats):
    resume = config.RESUMES / "candidate.pdf"
    resume.write_bytes(b"%PDF-1.7\nfixture")
    db.save_profile(Profile(first_name="Ada", last_name="Lovelace", email="ada@example.test",
                            resume_path=str(resume)))
    job = _secondary_job(ats)
    db.upsert_jobs([job])
    monkeypatch.setattr(scheduler, "enrich_job", lambda saved: saved)
    monkeypatch.setattr(scheduler, "resume_for_job", lambda *args: str(resume))
    calls = []

    def uncertain(*args, **kwargs):
        calls.append(True)
        db.mark_submission_attempted(job.id)
        raise RuntimeError("connection lost after submit")

    monkeypatch.setattr(scheduler, "apply_to_job", uncertain)
    result = scheduler.apply_one(job.id)

    record = db.get_application(result["application_id"])
    assert result["status"] == record["status"] == "needs_human"
    assert record["submission_attempted"] == 1
    assert db.get_job(job.id).status == "needs_human"
    assert scheduler.apply_one(job.id)["status"] == "blocked"
    assert len(calls) == 1
    assert all(row["status"] != "applied" for row in db.list_applications())


def test_secondary_priority_holds_and_canonical_dedupe(store, monkeypatch):
    workable = _secondary_job("workable", suffix="ABC123")
    workable_alias = workable.model_copy(update={
        "url": workable.url + "?utm_source=board",
        "apply_url": "https://apply.workable.com/example/j/ABC123/apply/",
    })
    rippling = _secondary_job("rippling", company="Other", suffix="1")
    priority_company = _secondary_job("workable", company="Priority Co", suffix="2")
    starred = _secondary_job("rippling", company="Starred Co", suffix="3", starred=True)
    db.upsert_jobs([workable, workable_alias, rippling, priority_company, starred])
    db.set_job_star(starred.id, True)
    assert db.job_stats()["total"] == 4
    db.set_settings({"auto_apply": True, "setup_complete": True, "max_apply_per_run": 5,
                     "priority_companies": ["Priority Co"]})
    monkeypatch.setattr(scheduler, "rescore", lambda: None)
    monkeypatch.setattr(scheduler, "preflight_job", lambda *args: type("Ready", (), {"status": "ready"})())
    attempted = []
    monkeypatch.setattr(scheduler, "apply_one", lambda job_id, **kwargs:
                        attempted.append(job_id) or {"status": "dry_run"})

    scheduler.auto_apply_batch()

    assert set(attempted) == {workable.id, rippling.id}
    assert priority_company.id not in attempted
    assert starred.id not in attempted
