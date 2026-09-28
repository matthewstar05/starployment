import pytest

from jobhub.models import Job
from jobhub.sources import import_job as importer


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeHTTP:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.payload)


def test_parse_job_url_allows_supported_hosts_and_rejects_lookalikes():
    parsed = importer.parse_job_url("https://job-boards.greenhouse.io/singlestore/jobs/8205389")
    assert (parsed.ats, parsed.slug, parsed.posting_id) == ("greenhouse", "singlestore", "8205389")
    for bad in (
        "http://job-boards.greenhouse.io/acme/jobs/1",
        "https://job-boards.greenhouse.io.evil.test/acme/jobs/1",
        "https://jobs.lever.co.evil.test/acme/jobs/1",
        "https://127.0.0.1/acme/jobs/1",
        "https://jobs.ashbyhq.com/acme/1/extra",
        "https://jobs.ashbyhq.com/acme/jobs/1%2Fsecret",
    ):
        with pytest.raises(importer.JobImportError):
            importer.parse_job_url(bad)


def test_greenhouse_import_uses_fixed_api_and_normalizes_description(monkeypatch):
    fake = FakeHTTP({
        "id": 8205389,
        "title": "Software Engineer New Grad",
        "company_name": "SingleStore",
        "location": {"name": "Remote, United States"},
        "absolute_url": "https://job-boards.greenhouse.io/singlestore/jobs/8205389",
        "content": "&lt;p&gt;Build C++ &amp;amp; Python systems&lt;/p&gt;",
        "first_published": "2026-01-01T00:00:00Z",
    })
    monkeypatch.setattr(importer, "client", lambda **kwargs: fake)
    job = importer.fetch_job("https://job-boards.greenhouse.io/singlestore/jobs/8205389")
    assert isinstance(job, Job)
    assert job.id == "a78843404609c0f3"
    assert job.description == "Build C++ & Python systems"
    assert fake.calls == [("https://boards-api.greenhouse.io/v1/boards/singlestore/jobs/8205389",
                           {"params": {"content": "true"}})]


def test_ashby_import_requires_matching_public_listing(monkeypatch):
    fake = FakeHTTP({"jobs": [{"id": "other", "title": "Wrong"}]})
    monkeypatch.setattr(importer, "client", lambda **kwargs: fake)
    with pytest.raises(importer.JobImportError, match="not found"):
        importer.fetch_job("https://jobs.ashbyhq.com/acme/expected")


def test_import_job_uses_upsert_and_returns_created(monkeypatch):
    job = Job(source="lever", ats="lever", company="Acme", title="Engineer",
              url="https://jobs.lever.co/acme/posting")
    monkeypatch.setattr(importer, "fetch_job", lambda url: job)

    class DB:
        def __init__(self):
            self.saved = []

        def upsert_jobs(self, jobs):
            self.saved.extend(jobs)
            return 1

        def get_job(self, job_id):
            return self.saved[0]

    db = DB()
    result, created = importer.import_job(job.url, db)
    assert created is True
    assert result.id == job.id
    assert db.saved == [job]
