"""Workday: public board scanning and the sign-in-first browser filler. Never contacts an employer."""
import json

import time

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


def test_a_closed_workday_posting_stops_before_the_submit_boundary(tmp_path, monkeypatch, browser):
    page_path = tmp_path / "closed.html"
    page_path.write_text("<!doctype html><html><body><a>Sign In</a><h2>The page you are looking for doesn't exist."
                         "</h2><button>Search for Jobs</button></body></html>")
    url = page_path.as_uri()
    monkeypatch.setattr(engine_module.config, "SCREENSHOTS", tmp_path / "shots")
    attempted = []
    monkeypatch.setattr(workday_module.db, "mark_submission_attempted", attempted.append)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    job = Job(source="fixture", ats="workday", company="Bank", title="Analyst", url=url, apply_url=url)
    engine = workday_module.WorkdayEngine(job, Profile(email="ada@example.test"),
                                          {"workday": {"send": True, "create_accounts": True}}, str(resume),
                                          Adapter("workday", url, assist_only=True), False)
    context = browser.new_context()
    page = context.new_page()
    try:
        started = time.monotonic()
        result = engine._run_page(context, page, url)
        assert result.status == "failed" and "closed this posting" in result.message
        assert not result.submission_attempted and attempted == []
        assert time.monotonic() - started < 30
    finally:
        context.close()


def test_a_shared_myworkdaysite_link_opens_the_employers_own_workday():
    url = "https://wd1.myworkdaysite.com/recruiting/fmr/FidelityCareers/job/Westlake-TX/Full-Stack-Developer_2134996-1"
    adapter = adapter_for(_workday_job(url))
    assert adapter is not None and adapter.name == "workday"
    assert adapter.apply_url == "https://fmr.wd1.myworkdayjobs.com/FidelityCareers/job/Westlake-TX/Full-Stack-Developer_2134996-1"
    from jobhub import vault
    assert vault.tenant_of(adapter.apply_url) == "fmr"


EXPERIENCE_PAGE = """<!doctype html><html><body><div data-automation-id="applyFlowPage">
<div data-automation-id="progressBarActiveStep">My Experience</div>
<div id="work"><h4>Work Experience 1</h4>
  <div data-automation-id="formField-jobTitle"><label for="jt">Job Title*</label><input id="jt" type="text"></div>
  <div data-automation-id="formField-companyName"><label for="co">Company*</label><input id="co" type="text"></div>
  <div data-automation-id="formField-currentlyWorkHere"><label for="cw">I currently work here</label><input id="cw" type="checkbox"></div>
  <div data-automation-id="formField-startDate"><label>From*</label>
    <input data-automation-id="dateSectionMonth-input" id="fm"><input data-automation-id="dateSectionYear-input" id="fy"></div>
</div>
<div id="edu"><h4>Education 1</h4>
  <div data-automation-id="formField-school"><label for="sc">School or University*</label><input id="sc" type="text"></div>
  <div data-automation-id="formField-firstYearAttended"><label>From*</label>
    <input data-automation-id="dateSectionMonth-input" id="em"><input data-automation-id="dateSectionYear-input" id="ey"></div>
</div></div></body></html>"""


def test_work_experience_comes_from_the_resume_and_education_dates_are_left_alone(tmp_path, monkeypatch, browser):
    from jobhub.models import Experience
    page_path = tmp_path / "exp.html"
    page_path.write_text(EXPERIENCE_PAGE)
    url = page_path.as_uri()
    monkeypatch.setattr(workday_module, "rule_answer", lambda *a: None)
    profile = Profile(email="ada@example.test", experience=[
        Experience(title="Data Analyst", company="Example Co", start="October 2024", end="Present")])
    job = Job(source="fixture", ats="workday", company="Bank", title="Analyst", url=url, apply_url=url)
    engine = workday_module.WorkdayEngine(job, profile, {}, "", Adapter("workday", url, assist_only=True), False)
    engine._tried, engine._failures, engine.answered, engine.log = set(), {}, {}, []
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(url)
        engine._fill_step(page)
        assert page.input_value("#jt") == "Data Analyst" and page.input_value("#co") == "Example Co"
        assert page.is_checked("#cw")
        assert page.input_value("#fm") == "10" and page.input_value("#fy") == "2024"
        assert page.input_value("#em") == "" and page.input_value("#ey") == ""   # school years untouched
    finally:
        context.close()


def test_picker_options_leave_out_other_pickers_chosen_values(browser):
    """The School pill was read as the Degree list's only option (2026-11-05)."""
    from jobhub.apply.workday import DEGREE_LEVELS, OPEN_PROMPT_OPTION
    html = ('<div data-automation-id="selectedItemList"><div data-automation-id="selectedItem">'
            '<div data-automation-id="promptOption">Example State University</div></div></div>'
            '<div role="listbox"><div data-automation-id="promptOption">Master of Science</div>'
            '<div data-automation-id="promptOption">Doctorate</div></div>')
    page = browser.new_page()
    try:
        page.set_content(html)
        assert page.locator(OPEN_PROMPT_OPTION).all_inner_texts() == ["Master of Science", "Doctorate"]
    finally:
        page.close()
    assert "master of science" in DEGREE_LEVELS["master's degree"] and "doctorate" not in DEGREE_LEVELS["master's degree"]


def test_a_degree_dropdown_takes_the_catalogs_wording_of_the_saved_level(tmp_path, monkeypatch, browser):
    """Stblaw and Marvell list "Master of Science (MS)" but no "Master's Degree" (2026-11-05)."""
    page_html = """<!doctype html><html><body><div data-automation-id="applyFlowPage">
      <div data-automation-id="formField-degree"><label>Degree*</label>
        <button aria-haspopup="listbox" id="degree" onclick="document.getElementById('dl').style.display='block'">Select One</button>
        <ul role="listbox" id="dl" style="display:none">
          <li role="option" onclick="pick(this)">High School (HS)</li><li role="option" onclick="pick(this)">Bachelor of Science (BS)</li>
          <li role="option" onclick="pick(this)">Master of Science (MS)</li><li role="option" onclick="pick(this)">Doctorate (PhD)</li>
        </ul></div></div>
      <script>function pick(li) { document.getElementById('degree').innerText = li.innerText; li.parentElement.style.display='none'; }</script>
      </body></html>"""
    path = tmp_path / "degree.html"
    path.write_text(page_html)
    monkeypatch.setattr(workday_module, "rule_answer", lambda q, *args:
                        Answer("Master's Degree", "user") if q.label.startswith("Degree") else None)
    job = Job(source="fixture", ats="workday", company="Firm", title="Analyst", url=path.as_uri())
    engine = workday_module.WorkdayEngine(job, Profile(), {}, str(tmp_path / "r.pdf"),
                                          Adapter("workday", path.as_uri(), assist_only=True), False)
    page = browser.new_page()
    try:
        page.goto(path.as_uri())
        engine.root = page.main_frame
        engine._fill_step(page)
        assert page.inner_text("#degree") == "Master of Science (MS)", engine.log
    finally:
        page.close()


@pytest.mark.parametrize("listed, expected", [
    (["Corporate Website", "Linkedln", "Indeed"], "Linkedln"),
    (["Crowe Career Site", "Other", "Referral - A Crowe Employee Referred Me"], "Other"),
    (["Career Builder", "Indeed", "Monster"], "Select One"),
])
def test_how_heard_uses_the_lists_spelling_or_other_never_a_different_source(tmp_path, monkeypatch, browser,
                                                                            listed, expected):
    items = "".join(f'<li role="option" onclick="pick(this)">{o}</li>' for o in listed)
    page_html = f"""<!doctype html><html><body><div data-automation-id="applyFlowPage">
      <div data-automation-id="formField-source"><label>How Did You Hear About Us?*</label>
        <button aria-haspopup="listbox" id="src" onclick="document.getElementById('sl').style.display='block'">Select One</button>
        <ul role="listbox" id="sl" style="display:none">{items}</ul></div></div>
      <script>function pick(li) {{ document.getElementById('src').innerText = li.innerText; li.parentElement.style.display='none'; }}</script>
      </body></html>"""
    path = tmp_path / "source.html"
    path.write_text(page_html)
    monkeypatch.setattr(workday_module, "rule_answer", lambda q, *args:
                        Answer("LinkedIn", "user") if q.label.startswith("How Did You Hear") and not q.options else None)
    job = Job(source="fixture", ats="workday", company="Firm", title="Analyst", url=path.as_uri())
    engine = workday_module.WorkdayEngine(job, Profile(), {}, str(tmp_path / "r.pdf"),
                                          Adapter("workday", path.as_uri(), assist_only=True), False)
    page = browser.new_page()
    try:
        page.goto(path.as_uri())
        engine.root = page.main_frame
        engine._fill_step(page)
        assert page.inner_text("#src") == expected, engine.log
    finally:
        page.close()


GUESS_PAGE = """<!doctype html><html><body><div data-automation-id="applyFlowPage">
  <div data-automation-id="formField-auth"><label>Are you legally authorized to work in the country where this job is located?*</label>
    <button aria-haspopup="listbox" id="auth" onclick="document.getElementById('al').style.display='block'">Select One</button>
    <ul role="listbox" id="al" style="display:none">
      <li role="option" onclick="pick('auth', this)">Yes</li><li role="option" onclick="pick('auth', this)">No</li></ul></div>
  <div data-automation-id="formField-prev"><legend>Are you a previous employee of Example Group?*</legend>
    <input type="radio" name="prev" id="p1" value="Yes"><label for="p1">Yes</label>
    <input type="radio" name="prev" id="p2" value="No"><label for="p2">No</label></div>
  <div data-automation-id="formField-travel"><label>Are you willing to travel as part of your job?*</label>
    <button aria-haspopup="listbox" id="travel" onclick="document.getElementById('tl').style.display='block'">Select One</button>
    <ul role="listbox" id="tl" style="display:none">
      <li role="option" onclick="pick('travel', this)">Yes</li><li role="option" onclick="pick('travel', this)">No</li></ul></div>
  <div data-automation-id="formField-why"><label>Why are you interested in this role?*</label><textarea id="why"></textarea></div>
  <div data-automation-id="formField-ack"><label><input type="checkbox" id="ack">I have read the job requirements*</label></div>
</div>
<script>function pick(id, li) { document.getElementById(id).innerText = li.innerText; li.parentElement.style.display='none'; }</script>
</body></html>"""


def _guess_engine(tmp_path, monkeypatch, mode, autofill):
    path = tmp_path / "guess.html"
    path.write_text(GUESS_PAGE)
    monkeypatch.setattr(workday_module, "rule_answer", lambda *a, **kw: None)
    from jobhub import luna
    monkeypatch.setattr(luna, "autofill_answers", autofill)
    job = Job(source="fixture", ats="workday", company="Example Group", title="Analyst", url=path.as_uri())
    engine = workday_module.WorkdayEngine(job, Profile(), {"jarvis_mode": mode}, str(tmp_path / "r.pdf"),
                                          Adapter("workday", path.as_uri(), assist_only=True), False)
    engine._guesses = {}
    return engine, path


def test_all_plus_guesses_every_open_workday_question_instead_of_stopping(tmp_path, monkeypatch, browser):
    """2026-11-08 night: all 43 Workday attempts stopped on a question saved answers did not settle."""
    asked = []

    def autofill(job, profile, settings, questions):
        asked.extend(questions)
        answers = {"legally authorized": "Yes", "interested": "I like the team's data work."}
        return {q["id"]: {"answer": a} for q in questions for k, a in answers.items() if k in q["label"]}
    engine, path = _guess_engine(tmp_path, monkeypatch, "all_plus", autofill)
    page = browser.new_page()
    try:
        page.goto(path.as_uri())
        engine.root = page.main_frame
        _, open_required = engine._fill_step(page)
        assert open_required == [], engine.log
        assert page.inner_text("#auth") == "Yes"
        assert page.is_checked("#p2")                    # fallback: "previous employee" is No
        assert page.inner_text("#travel") == "Yes"       # fallback: willingness is Yes
        assert page.input_value("#why") == "I like the team's data work."
        assert page.is_checked("#ack")
        assert {"Yes", "No"} <= set(next(q for q in asked if "authorized" in q["label"])["options"])
        assert any(" Guessed " in line for line in engine.log)
    finally:
        page.close()


@pytest.mark.parametrize("mode", ["all", "off"])
def test_only_all_plus_guesses_workday_answers(tmp_path, monkeypatch, browser, mode):
    engine, path = _guess_engine(tmp_path, monkeypatch, mode, lambda *a, **kw: {})   # ALL may still write text
    page = browser.new_page()
    try:
        page.goto(path.as_uri())
        engine.root = page.main_frame
        _, open_required = engine._fill_step(page)
        assert any("authorized" in label for label in open_required)
    finally:
        page.close()


def test_guessing_declines_demographics_and_dates_education_from_the_resume():
    from jobhub.models import Education
    job = Job(source="fixture", ats="workday", company="Firm", title="Analyst", url="https://x.example.test")
    profile = Profile(education=[Education(school="Example State", start="Aug 2020", end="May 2024")])
    engine = workday_module.WorkdayEngine(job, profile, {"jarvis_mode": "all_plus"}, "",
                                          Adapter("workday", "https://x.example.test", assist_only=True), False)
    gender = {"label": "Gender", "kind": "dropdown", "ancestors": ""}
    assert engine._fallback_guess(gender, ["Male", "Female", "I do not wish to answer"]) == "I do not wish to answer"
    assert engine._fallback_guess({**gender, "label": "Race"}, ["White", "Asian"]) is None
    engine.settings = {"jarvis_mode": "all_plus",
                       "answers": {"field_answers": {"What is your gender/gender identity?": "Male"}}}
    assert engine._fallback_guess({**gender, "label": "What is your Gender?"}, ["Female", "Male"]) == "Male"
    assert engine._fallback_guess({**gender, "label": "Are you Hispanic or Latino?"}, ["Yes", "No"]) is None
    to = {"label": "To (Actual or Expected)", "kind": "date", "ancestors": "education-1 < educationSection"}
    assert engine._fallback_guess(to, []) == "05/01/2024"
    assert engine._fallback_guess({**to, "label": "From"}, []) == "08/01/2020"


def test_workday_something_went_wrong_page_is_refreshed_not_left(tmp_path, monkeypatch, browser):
    """Resumed drafts (2026-11-08) opened to Workday's "Something went wrong. Please refresh the page"."""
    broken = WORKDAY_PAGE.replace(
        "setTimeout(() => { document.getElementById('signin').remove(); document.getElementById('flow').style.display='block'; }, 3500);",
        """setTimeout(() => {
             document.getElementById('signin').remove();
             if (!sessionStorage.getItem('seen')) {
               sessionStorage.setItem('seen', '1');
               const flow = document.getElementById('flow');
               flow.innerHTML = '<div data-automation-id="progressBarActiveStep">My Information</div>' +
                 '<h2>Something went wrong</h2><p>Please refresh the page and then try again.</p>';
               flow.style.display = 'block';
             } else { document.getElementById('flow').style.display='block'; }
           }, 1500);""")
    assert broken != WORKDAY_PAGE
    page_path = tmp_path / "workday.html"
    page_path.write_text(broken)
    url = page_path.as_uri()
    monkeypatch.setattr(engine_module.config, "SCREENSHOTS", tmp_path / "shots")
    monkeypatch.setattr(workday_module.db, "mark_submission_attempted", lambda job_id: None)
    monkeypatch.setattr(workday_module, "ASSIST_SECONDS", 25)
    saved = {"First Name": "Ada", "Country": "United States of America"}
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
        assert result.status == "applied", (result.message, engine.log[-8:])
        assert any("something went wrong; refreshing" in line for line in engine.log)
    finally:
        context.close()
