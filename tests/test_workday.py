"""Workday: public board scanning and the sign-in-first browser filler. Never contacts an employer."""
import json

import pytest
from playwright.sync_api import sync_playwright

from jobhub.answers import Answer
from jobhub.apply import apply_to_job
from jobhub.apply import engine as engine_module
from jobhub.apply import workday as workday_module
from jobhub.apply.adapters import Adapter, adapter_for
from jobhub.models import Job, Profile, board_of
from jobhub.sources import workday as source


def test_workday_board_is_learned_from_posting_urls():
    assert board_of("https://citi.wd5.myworkdayjobs.com/en-US/2/job/Tampa/Analyst_123") == ("workday", "citi.wd5/2")
    assert board_of("https://aig.wd1.myworkdayjobs.com/Early_Careers/job/NY/X_JR1") == ("workday", "aig.wd1/early_careers")
    assert board_of("https://evil.example.test/aig.wd1.myworkdayjobs.com/x") is None
    assert board_of("https://myworkdayjobs.com/x/job/y") is None
    assert source.parse_board("citi.wd5/2") == ("citi", "wd5", "2")
    assert source.parse_board("citi/2") is None and source.parse_board("citi.wd5/../x") is None


def test_us_facet_is_found_inside_a_location_group():
    facets = [{"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locationCountry", "values": [
            {"descriptor": "India", "id": "in1"}, {"descriptor": "United States of America", "id": "us1"}]}]}]
    assert source._us_facet(facets) == {"locationCountry": ["us1"]}
    assert source._us_facet([{"facetParameter": "jobFamily", "values": [{"descriptor": "Tech", "id": "t"}]}]) == {}


class _Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Client:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        self.calls.append(("post", url, json["appliedFacets"]))
        facets = [{"facetParameter": "locationCountry",
                   "values": [{"descriptor": "United States of America", "id": "us1"}]}]
        postings = [{"title": "Investment Banking Analyst", "externalPath": "/job/NY/IB-Analyst_R1"},
                    {"title": "Marketing Coordinator", "externalPath": "/job/NY/Marketing_R2"}]
        return _Response({"total": 2, "jobPostings": postings, "facets": facets})

    def get(self, url, headers=None):
        self.calls.append(("get", url, None))
        return _Response({"jobPostingInfo": {
            "title": "Investment Banking Analyst", "jobDescription": "<p>Economics or finance degree. $110,000 - $125,000</p>",
            "location": "New York, NY", "startDate": "2026-10-09", "canApply": True, "jobReqId": "R1",
            "country": {"descriptor": "United States of America"},
            "externalUrl": "https://bank.wd5.myworkdayjobs.com/careers/job/NY/IB-Analyst_R1"}})


def test_board_reads_us_postings_and_only_fetches_matching_descriptions(monkeypatch):
    calls = []
    monkeypatch.setattr(source, "client", lambda: _Client(calls))
    settings = {"target_titles": ["investment banking analyst"], "exclude_title_words": []}
    jobs = source._fetch_board("bank.wd5/careers", settings)
    assert [job.title for job in jobs] == ["Investment Banking Analyst"]
    job = jobs[0]
    assert job.ats == "workday" and job.posted_at == "2026-10-09" and job.salary_min == 110000
    assert job.apply_url.startswith("https://bank.wd5.myworkdayjobs.com/careers/job/")
    assert ("post", "https://bank.wd5.myworkdayjobs.com/wday/cxs/bank/careers/jobs", {"locationCountry": ["us1"]}) in calls
    assert [c for c in calls if c[0] == "get"] == [
        ("get", "https://bank.wd5.myworkdayjobs.com/wday/cxs/bank/careers/job/NY/IB-Analyst_R1", None)]


def _workday_job(url="https://bank.wd5.myworkdayjobs.com/careers/job/NY/IB-Analyst_R1"):
    return Job(source="workday", ats="workday", company="Bank", title="Analyst", url=url, apply_url=url)


def test_workday_is_never_sent_unattended(tmp_path, monkeypatch):
    adapter = adapter_for(_workday_job())
    assert adapter is not None and adapter.name == "workday" and adapter.assist_only
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(workday_module.WorkdayEngine, "run", lambda self: pytest.fail("no browser without assist"))
    result = apply_to_job(_workday_job(), Profile(email="a@example.test"), {"dry_run": False}, str(resume))
    assert result.status == "needs_human" and "Finish in browser" in result.message
    assert not result.submission_attempted


def test_workday_drafts_skip_send_ready_and_approve_and_send():
    from jobhub import prepare
    send, held = prepare.bulk_send_candidates([{"id": 1, "status": "ready", "ats": "workday", "company": "Bank"}], [])
    assert send == [] and held == []
    assert "workday" in prepare.PREPARABLE_ATS and "workday" not in prepare.SUPPORTED_ATS


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


WORKDAY_PAGE = """<!doctype html><html><body>
<div id="signin" data-automation-id="signInContent">
  <input data-automation-id="email" type="email"><input data-automation-id="password" type="password">
  <button>Sign In</button>
</div>
<div data-automation-id="applyFlowPage" id="flow" style="display:none">
  <div data-automation-id="progressBarActiveStep">My Information</div>
  <div data-automation-id="formField-legalNameSection_firstName"><label for="fn">First Name*</label><input id="fn" type="text"></div>
  <div data-automation-id="formField-countryDropdown"><label>Country*</label>
    <button aria-haspopup="listbox" id="country" onclick="document.getElementById('cl').style.display='block'">Select One</button>
    <ul role="listbox" id="cl" style="display:none">
      <li role="option" onclick="pick(this)">Canada</li><li role="option" onclick="pick(this)">United States of America</li>
    </ul></div>
  <div data-automation-id="formField-previousWorker"><label>Have you previously worked for Bank?*</label>
    <input type="radio" name="pw" id="y"><label for="y">Yes</label><input type="radio" name="pw" id="n"><label for="n">No</label></div>
  <div data-automation-id="formField-terms"><label for="t">I agree to the terms*</label><input type="checkbox" id="t"></div>
  <button data-automation-id="bottom-navigation-next-button" onclick="window.saved=(window.saved||0)+1">Save and Continue</button>
  <button onclick="window.submitted=(window.submitted||0)+1">Submit</button>
</div>
<div id="done" role="dialog" style="display:none">Application Submitted</div>
<script>
  function pick(li) { document.getElementById('country').innerText = li.innerText; li.parentElement.style.display='none'; }
  // The candidate signs in after a moment, then submits once the form is filled.
  setTimeout(() => { document.getElementById('signin').remove(); document.getElementById('flow').style.display='block'; }, 3500);
  const finish = setInterval(() => {
    if (document.getElementById('fn').value && document.getElementById('country').innerText !== 'Select One') {
      clearInterval(finish); document.getElementById('done').style.display='block';
    }
  }, 300);
</script></body></html>"""


def test_workday_filler_waits_for_sign_in_fills_the_step_and_never_presses_save_or_submit(tmp_path, monkeypatch, browser):
    page_path = tmp_path / "workday.html"
    page_path.write_text(WORKDAY_PAGE)
    url = page_path.as_uri()
    monkeypatch.setattr(engine_module.config, "SCREENSHOTS", tmp_path / "shots")
    attempted = []
    monkeypatch.setattr(workday_module.db, "mark_submission_attempted", attempted.append)
    monkeypatch.setattr(workday_module, "ASSIST_SECONDS", 20)
    saved = {"First Name": "Ada", "Country": "United States of America", "I agree to the terms": True}
    monkeypatch.setattr(workday_module, "rule_answer", lambda q, *args:
                        Answer(saved[q.label], "rule") if q.label in saved else None)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    job = Job(source="fixture", ats="workday", company="Bank", title="Analyst", url=url, apply_url=url)
    engine = workday_module.WorkdayEngine(job, Profile(email="ada@example.test"), {}, str(resume),
                                          Adapter("workday", url, assist_only=True), False, assist=True)
    context = browser.new_context()
    page = context.new_page()
    try:
        result = engine._run_page(context, page, url)
        assert result.status == "applied", result.message
        assert attempted == [job.id]
        assert page.input_value("#fn") == "Ada"
        assert page.inner_text("#country") == "United States of America"
        assert any("Waiting for you to sign in" in line for line in engine.log)
        assert not page.is_checked("#y") and not page.is_checked("#n")   # the candidate's own history
        assert page.evaluate("window.saved || 0") == 0 and page.evaluate("window.submitted || 0") == 0
        assert "Have you previously worked for Bank?" in engine.unanswered
    finally:
        context.close()


def test_finance_and_software_roles_share_degree_credit():
    from jobhub import scoring
    education = (("Master of Science in Quantitative Economics", "", ("Relevant Coursework: Advanced Econometrics, Probability",)),
                 ("Bachelor of Science in Computer Science", "", ("Concentration: AI | Minor: Economics",)))
    terms = scoring._academic_terms(education)
    assert {"quantitative economics", "economics", "econometrics", "probability", "computer science"} <= set(terms)
    assert "science" not in terms and "science in computer science" not in terms
    assert scoring.ENTRY_RE.search("2027 Analyst, Investment Banking") and scoring.ENTRY_RE.search("Analyst Program")


def test_workday_settings_validate():
    from jobhub import config
    settings = config.merge_settings(config.DEFAULT_SETTINGS, {"companies": {"workday": ["citi.wd5/2"]}})
    assert config.validate_settings(settings)["companies"]["workday"] == ["citi.wd5/2"]
    assert json.dumps(config.DEFAULT_SETTINGS["sources"]).count("workday") == 1
