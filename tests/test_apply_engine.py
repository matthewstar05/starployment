"""Exercise the application engine against local forms, never live employers.

These browser tests validate completed DOM values, blocked submissions, and
confirmation detection. They require `playwright install chromium`.
"""

import pytest
from playwright.sync_api import sync_playwright

from jobhub.answers import Answer
from jobhub.apply import engine as engine_module
from jobhub.apply.adapters import Adapter, adapter_for
from jobhub.apply.engine import FormEngine, SCAN_JS
from jobhub.models import Job, Profile


def test_formatted_phone_value_stays_recognized_as_filled():
    field = {"kind": "text", "type": "tel", "label": "Phone", "value": "(805) 555-0143",
             "empty": False, "invalid": False, "ariaInvalid": False}
    assert FormEngine._answer_retained(field, "8055550143")
    assert not FormEngine._answer_retained(field, "8055550144")


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def fixture_form(tmp_path, monkeypatch, browser):
    context = browser.new_context()
    page = context.new_page()
    monkeypatch.setattr(engine_module.config, "SCREENSHOTS", tmp_path / "screenshots")
    monkeypatch.setattr(engine_module, "llm_answers", lambda *args: {})
    attempted = []
    monkeypatch.setattr(engine_module.db, "mark_submission_attempted", attempted.append)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\nlocal test resume")
    profile = Profile(first_name="Ada", last_name="Lovelace", email="ada@example.test")
    answers = {"First Name": "Ada", "Last Name": "Lovelace", "Email": "ada@example.test"}
    monkeypatch.setattr(engine_module, "rule_answer", lambda q, *args:
                        Answer(answers[q.label], "rule") if q.label in answers else None)

    def create(fields="", *, before="", submit="", dry=False):
        path = tmp_path / "form.html"
        path.write_text(f'''<!doctype html><html><body>{before}
          <form onsubmit="event.preventDefault(); window.submissions=(window.submissions||0)+1; {submit}">
            <label for="first">First Name *</label><input id="first" required>
            <label for="email">Email *</label><input id="email" type="email" required>
            <label for="resume">Resume *</label><input id="resume" name="resume" type="file" required>
            {fields}<button type="submit">Submit application</button>
          </form></body></html>''')
        url = path.as_uri()
        job = Job(source="fixture", company="Local Fixture", title="Software Engineer", url=url)
        engine = FormEngine(job, profile, {"submission_timeout_sec": 0.75}, str(resume),
                            Adapter("fixture", url, settle_ms=0), dry)
        return engine, context, page

    yield create, answers, attempted
    context.close()


def execute(fixture_form, fields="", **kwargs):
    create, answers, attempted = fixture_form
    engine, context, page = create(fields, **kwargs)
    result = engine._run_page(context, page, engine.adapter.apply_url)
    return result, page, engine


def test_new_confirmation_is_required_and_detected(fixture_form):
    result, page, engine = execute(fixture_form, submit="document.body.innerHTML='Your application has been submitted';")
    assert result.status == "applied"
    assert result.submission_attempted
    assert fixture_form[2] == [engine.job.id]
    assert result.screenshots[-1].endswith("success.png")


def test_dry_run_fills_resume_and_identity_but_never_submits(fixture_form):
    result, page, _ = execute(fixture_form, dry=True)
    assert result.status == "dry_run"
    assert not result.submission_attempted
    assert fixture_form[2] == []
    assert page.locator("#first").input_value() == "Ada"
    assert page.locator("#email").input_value() == "ada@example.test"
    assert page.locator("#resume").evaluate("e => e.files[0].name") == "resume.pdf"
    assert page.evaluate("window.submissions || 0") == 0


def test_unknown_required_checkbox_blocks_submission(fixture_form):
    result, page, _ = execute(fixture_form, '<label><input type="checkbox" required> I certify I have a security clearance</label>')
    assert result.status == "needs_human"
    assert not result.submission_attempted
    assert any("security clearance" in question and "required" in question for question in result.unanswered)
    assert not page.locator('input[type="checkbox"]').is_checked()


def test_required_extra_upload_is_not_silently_skipped(fixture_form):
    result, page, _ = execute(fixture_form, '<label for="transcript">Transcript *</label><input id="transcript" type="file" required>')
    assert result.status == "needs_human"
    assert not result.submission_attempted
    assert "Transcript (required)" in result.unanswered
    assert page.locator("#transcript").evaluate("e => e.files.length") == 0


def test_false_checkbox_answer_does_not_become_truthy_string(fixture_form):
    fixture_form[1]["Newsletter"] = "false"
    result, page, engine = execute(fixture_form, '<label><input type="checkbox"> Newsletter</label>', dry=True)
    assert not page.locator('input[type="checkbox"]').is_checked()
    assert any("explicit true or false" in message for message in engine.log)


def test_explicit_checkbox_and_select_values_are_filled(fixture_form):
    fixture_form[1].update({"I agree to the privacy policy": True, "Degree": "Bachelor's Degree"})
    result, page, _ = execute(fixture_form, '''
      <label><input id="consent" type="checkbox" required> I agree to the privacy policy</label>
      <label for="degree">Degree *</label><select id="degree" required>
        <option value="">Select...</option><option>Bachelor's Degree</option><option>Master's Degree</option>
      </select>''', dry=True)
    assert not result.unanswered
    assert page.locator("#consent").is_checked()
    assert page.locator("#degree").input_value() == "Bachelor's Degree"


def test_radio_group_remains_visible_to_repeated_scans(fixture_form):
    fixture_form[1]["Work authorization"] = "No"
    result, page, engine = execute(fixture_form, '''<fieldset><legend>Work authorization *</legend>
      <label><input name="authorized" type="radio" value="yes" required>Yes</label>
      <label><input name="authorized" type="radio" value="no" required>No</label>
    </fieldset>''', dry=True)
    first, second = page.evaluate(SCAN_JS), page.evaluate(SCAN_JS)
    radio_first = [f for f in first if f["kind"] == "radio"]
    radio_second = [f for f in second if f["kind"] == "radio"]
    assert len(radio_first) == len(radio_second) == 1
    assert radio_first[0]["optionIdx"] == radio_second[0]["optionIdx"]
    assert radio_first[0]["required"]
    assert not radio_first[0]["empty"]
    assert page.locator('input[value="no"]').is_checked()


def test_duplicate_labels_are_each_filled(fixture_form):
    fixture_form[1]["Project name"] = "Sample Project"
    result, page, _ = execute(fixture_form, '''
      <label for="project1">Project name *</label><input id="project1" required>
      <label for="project2">Project name *</label><input id="project2" required>''', dry=True)
    assert page.locator("#project1").input_value() == "Sample Project"
    assert page.locator("#project2").input_value() == "Sample Project"
    assert not result.unanswered


def test_conditionally_revealed_required_field_is_filled(fixture_form):
    fixture_form[1].update({"Add portfolio": True, "Portfolio URL": "https://example.test/ada"})
    result, page, _ = execute(fixture_form, '''
      <label><input id="add" type="checkbox" onchange="document.querySelector('#conditional').hidden=!this.checked"> Add portfolio</label>
      <div id="conditional" hidden><label for="portfolio">Portfolio URL *</label><input id="portfolio" type="url" required></div>
    ''', dry=True)
    assert page.locator("#portfolio").input_value() == "https://example.test/ada"
    assert not result.unanswered


def test_invalid_native_value_blocks_submission(fixture_form):
    fixture_form[1]["Email"] = "invalid email"
    result, page, _ = execute(fixture_form)
    assert result.status == "needs_human"
    assert "Email" in result.message
    assert not result.submission_attempted


def test_existing_thank_you_text_is_not_submission_confirmation(fixture_form):
    result, page, _ = execute(fixture_form, before="<p>Thanks for applying</p>")
    assert result.status == "needs_human"
    assert result.submission_attempted
    assert page.evaluate("window.submissions") == 1
    assert "no new confirmation" in result.message


def test_confirmation_url_alone_does_not_prove_submission(fixture_form):
    result, page, _ = execute(fixture_form, submit="location.hash='confirmation';")
    assert result.status == "needs_human"
    assert result.submission_attempted
    assert page.url.endswith("#confirmation")


def test_post_submit_email_code_is_reported_as_unconfirmed(fixture_form):
    result, page, _ = execute(fixture_form, submit="document.body.insertAdjacentHTML('beforeend','<p>A verification code was sent to ada@example.test. Enter the 8-character code to confirm you are a human.</p>');")
    assert result.status == "needs_human"
    assert result.submission_attempted
    assert "email verification code" in result.message.lower()
    assert "Email verification code (required)" in result.unanswered
    assert result.screenshots[-1].endswith("email_verification.png")
    assert page.evaluate("window.submissions") == 1


def test_opt_in_fresh_email_code_requires_employer_confirmation(fixture_form, monkeypatch):
    class FakeInbox:
        def __init__(self, settings, recipient):
            assert recipient == "ada@example.test"

        def baseline(self):
            return 20, "before-submit"

        def wait_for_code(self, uid, started, length):
            assert (uid, started, length) == (20, "before-submit", 8)
            return "aBcD1234"

    monkeypatch.setattr(engine_module, "EmailVerificationInbox", FakeInbox)
    create, _, attempted = fixture_form
    fields = "".join("<input maxlength='1' aria-label='Code character'>" for _ in range(8))
    otp = ("<p>A verification code was sent to ada@example.test. Enter the 8-character code to confirm you are a human.</p>"
           "<form id='otp' onsubmit='event.preventDefault();window.codeSubmissions=(window.codeSubmissions||0)+1;"
           "window.codeEntered=[...document.querySelectorAll(&quot;#otp input&quot;)].map(e=>e.value).join(&quot;&quot;);"
           "document.body.textContent=&quot;Your application has been submitted&quot;'>"
           f"{fields}<button type='submit'>Verify code</button></form>")
    engine, context, page = create(
        fields="<label for='postal_code'>Postal Code</label><input id='postal_code' name='postal_code'>",
        submit=f"document.body.insertAdjacentHTML('beforeend', `{otp}`);")
    engine.adapter.name = "greenhouse"
    engine.settings["email_verification"] = {"enabled": True}
    result = engine._run_page(context, page, engine.adapter.apply_url)
    assert result.status == "applied"
    assert result.submission_attempted
    assert "Email verification code (required)" not in result.unanswered
    assert page.evaluate("window.submissions") == 1
    assert page.evaluate("window.codeSubmissions") == 1
    assert page.evaluate("window.codeEntered") == "aBcD1234"
    assert attempted == [engine.job.id]
    assert not any("email_code_filled" in name for name in result.screenshots)
    assert any("Employer confirmation after email verification" in line for line in result.log)
    assert all("aBcD1234" not in line for line in result.log)


def test_verification_widget_does_not_select_postal_code(fixture_form):
    create, _, _ = fixture_form
    engine, context, page = create()
    page.set_content("<input name='postal_code'><div>Enter the 8-character code</div>" +
                     "".join("<input maxlength='1'>" for _ in range(8)))
    fields = engine._verification_fields(page, 8)
    assert len(fields) == 8
    assert all(field.get_attribute("maxlength") == "1" for field in fields)


def test_opt_in_email_mailbox_failure_still_allows_normal_confirmation(fixture_form, monkeypatch):
    class MissingMailbox:
        def __init__(self, settings, recipient):
            raise RuntimeError("mailbox unavailable")

    monkeypatch.setattr(engine_module, "EmailVerificationInbox", MissingMailbox)
    create, _, attempted = fixture_form
    engine, context, page = create(submit="document.body.innerHTML='Your application has been submitted';")
    engine.adapter.name = "greenhouse"
    engine.settings["email_verification"] = {"enabled": True}
    result = engine._run_page(context, page, engine.adapter.apply_url)
    assert result.status == "applied"
    assert result.submission_attempted
    assert attempted == [engine.job.id]
    assert page.evaluate("window.submissions") == 1


def test_opt_in_email_mailbox_failure_with_otp_needs_human(fixture_form, monkeypatch):
    class MissingMailbox:
        def __init__(self, settings, recipient):
            raise RuntimeError("mailbox unavailable")

    monkeypatch.setattr(engine_module, "EmailVerificationInbox", MissingMailbox)
    create, _, attempted = fixture_form
    engine, context, page = create(submit="document.body.insertAdjacentHTML('beforeend','<p>A verification code was sent to ada@example.test. Enter the 8-character code to confirm you are a human.</p>');")
    engine.adapter.name = "greenhouse"
    engine.settings["email_verification"] = {"enabled": True}
    result = engine._run_page(context, page, engine.adapter.apply_url)
    assert result.status == "needs_human"
    assert result.submission_attempted
    assert "mailbox or system credential vault was unavailable" in result.message
    assert attempted == [engine.job.id]
    assert page.evaluate("window.submissions") == 1


def test_rejected_email_code_never_becomes_applied_or_retries(fixture_form, monkeypatch):
    class FakeInbox:
        def __init__(self, settings, recipient):
            pass

        def baseline(self):
            return 20, "before-submit"

        def wait_for_code(self, uid, started, length):
            return "ABCD1234"

    monkeypatch.setattr(engine_module, "EmailVerificationInbox", FakeInbox)
    create, _, attempted = fixture_form
    otp = ("<p>A verification code was sent to ada@example.test. Enter the 8-character code to confirm you are a human.</p>"
           "<form id='otp' onsubmit='event.preventDefault();window.codeSubmissions=(window.codeSubmissions||0)+1;"
           "document.body.insertAdjacentHTML(&quot;beforeend&quot;,&quot;<p role=alert>Incorrect code</p>&quot;)'>"
           "<input name='verification_code'><button type='submit'>Verify code</button></form>")
    engine, context, page = create(submit=f"document.body.insertAdjacentHTML('beforeend', `{otp}`);")
    engine.adapter.name = "greenhouse"
    engine.settings["email_verification"] = {"enabled": True}
    result = engine._run_page(context, page, engine.adapter.apply_url)
    assert result.status == "needs_human"
    assert "employer reported an error" in result.message.lower()
    assert "ABCD1234" not in result.message
    assert result.submission_attempted
    assert page.evaluate("window.submissions") == 1
    assert page.evaluate("window.codeSubmissions") == 1
    assert attempted == [engine.job.id]
    assert "Email verification code (required)" in result.unanswered
    assert not any("email_code" in name for name in result.screenshots)


def test_submit_validation_errors_are_reported(fixture_form):
    result, page, _ = execute(fixture_form, submit="document.body.insertAdjacentHTML('beforeend','<p role=alert>Employer rejected this email address</p>');")
    assert result.status == "needs_human"
    assert result.submission_attempted
    assert "Employer rejected" in result.message


def test_visible_security_check_stops_before_filling(fixture_form):
    result, page, _ = execute(fixture_form, before="<p>Verify you are human</p>")
    assert result.status == "needs_human"
    assert not result.submission_attempted
    assert page.locator("#first").input_value() == ""


def test_readonly_custom_combobox_selects_an_actual_option(fixture_form):
    fixture_form[1]["Country"] = "United States"
    result, page, _ = execute(fixture_form, '''
      <label id="country-label">Country *</label>
      <button type="button" id="country" role="combobox" aria-labelledby="country-label"
        aria-required="true" onclick="document.querySelector('#options').hidden=false">Select...</button>
      <div id="options" hidden>
        <div role="option" onclick="document.querySelector('#country').textContent=this.textContent;this.parentElement.hidden=true">United States</div>
        <div role="option">Canada</div>
      </div>''', dry=True)
    assert page.locator("#country").inner_text() == "United States"
    assert not result.unanswered


def test_collapsed_embedded_form_is_opened_before_filling(fixture_form, tmp_path):
    create, _, _ = fixture_form
    engine, context, page = create(dry=True)
    form = tmp_path / "application_form.html"
    form.write_text('''<form>
      <label for="first">First Name *</label><input id="first" required>
      <label for="email">Email *</label><input id="email" type="email" required>
      <label for="resume">Resume *</label><input id="resume" name="resume" type="file" required>
      <button type="submit">Submit application</button></form>''')
    wrapper = tmp_path / "collapsed.html"
    wrapper.write_text(f'''<div style="height:1100px">Job description</div>
      <button type="button" onclick="document.querySelector('#wrap').style.height='720px'">Apply Now</button>
      <div id="wrap" style="height:0;overflow:hidden">
        <iframe src="{form.as_uri()}" style="width:700px;height:700px;border:0"></iframe>
      </div>''')
    engine.adapter.apply_url = wrapper.as_uri()
    result = engine._run_page(context, page, engine.adapter.apply_url)
    assert result.status == "dry_run"
    assert not result.unanswered
    assert page.locator("#wrap").evaluate("e => e.getBoundingClientRect().height") == 720
    assert page.frame_locator("iframe").locator("#first").input_value() == "Ada"


def test_replaced_form_reconciles_retained_answers_and_refills_upload(fixture_form):
    fixture_form[1]["Work authorization"] = "Yes"
    fields = '''<fieldset><legend>Work authorization *</legend>
      <label><input type="radio" name="auth" required value="yes" onchange="
        setTimeout(() => {
          const form = document.querySelector('form');
          const clone = form.cloneNode(true);
          clone.querySelectorAll('[data-jh]').forEach(e => e.removeAttribute('data-jh'));
          form.replaceWith(clone);
        }, 100)">Yes</label>
      <label><input type="radio" name="auth" required value="no">No</label>
    </fieldset>'''
    result, page, _ = execute(fixture_form, fields, dry=True)
    assert result.status == "dry_run"
    assert not result.unanswered
    assert page.locator('input[value="yes"]').is_checked()
    assert page.locator('#resume').evaluate('e => e.files[0].name') == 'resume.pdf'


def test_city_autocomplete_uses_unique_city_and_country_match(fixture_form):
    fixture_form[1]["What city do you live in?"] = "San Luis Obispo"
    create, _, _ = fixture_form
    engine, context, page = create('''
      <label for="city">What city do you live in? *</label>
      <input id="city" role="combobox" aria-required="true"
        oninput="document.querySelector('#city-options').hidden=false">
      <div id="city-options" hidden>
        <div role="option" onclick="document.querySelector('#city').value=this.textContent;this.parentElement.hidden=true">San Luis Obispo, California, United States</div>
        <div role="option">San Luis, San Luis, Argentina</div>
      </div>''', dry=True)
    engine.profile.city = "San Luis Obispo"
    engine.profile.country = "United States"
    result = engine._run_page(context, page, engine.adapter.apply_url)
    assert result.status == "dry_run"
    assert not result.unanswered
    assert page.locator("#city").input_value() == "San Luis Obispo, California, United States"


def test_ashby_current_location_autocomplete_matches_saved_city_state_country(fixture_form):
    create, answers, _ = fixture_form
    answers["Where are you currently located?"] = "San Luis Obispo, CA, United States"
    engine, context, page = create('''
      <label for="where">Where are you currently located? *</label>
      <input id="where" role="combobox" aria-required="true"
        oninput="document.querySelector('#where-options').hidden=false">
      <div id="where-options" hidden>
        <div role="option" onclick="document.querySelector('#where').value=this.textContent;this.parentElement.hidden=true">San Luis Obispo, California, United States</div>
        <div role="option">San Luis Obispo, Arizona, United States</div>
        <div role="option">San Luis, Arizona, United States</div>
        <div role="option">San Luis, San Luis, Argentina</div>
      </div>''', dry=True)
    engine.profile.city = "San Luis Obispo"
    engine.profile.state = "CA"
    engine.profile.country = "United States"
    result = engine._run_page(context, page, engine.adapter.apply_url)
    assert result.status == "dry_run"
    assert not result.unanswered, result.log
    assert page.locator("#where").input_value() == "San Luis Obispo, California, United States"


@pytest.mark.parametrize("url, expected", [
    ("https://job-boards.greenhouse.io/acme/jobs/123", "greenhouse"),
    ("https://jobs.lever.co/acme/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee?source=JobHub", "lever"),
    ("https://jobs.eu.lever.co/acme/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/apply", "lever"),
    ("https://jobs.ashbyhq.com/acme/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "ashby"),
    ("https://greenhouse.io.example.test/jobs/123", None),
    ("https://example.test/?next=jobs.lever.co", None),
    ("https://example.test/jobs/123", None),
    ("https://www.linkedin.com/jobs/123", None),
])
def test_adapter_checks_actual_supported_hosts(url, expected):
    job = Job(source="test", ats="greenhouse", title="Engineer", company="Fixture", url=url)
    adapter = adapter_for(job)
    assert (adapter.name if adapter else None) == expected
    if adapter and expected == "lever" and "source=" in url:
        assert adapter.apply_url.endswith("/apply?source=JobHub")


def test_ashby_hidden_yes_no_controls_are_scanned_and_required(fixture_form):
    fields = '''<div class="ashby-application-form-field-entry">
      <label class="ashby-application-form-question-title _required_example">Work authorization</label>
      <div class="ashby-application-form-input-yesno">
        <button type="button" data-option="yes" aria-pressed="false"
          onclick="this.setAttribute('aria-pressed','true')">Yes</button>
        <button type="button" data-option="no" aria-pressed="false"
          onclick="this.setAttribute('aria-pressed','true')">No</button>
        <input name="authorization" type="checkbox" style="display:none">
      </div></div>'''
    result, page, _ = execute(fixture_form, fields)
    assert result.status == "needs_human"
    assert not result.submission_attempted
    assert "Work authorization (required)" in result.unanswered
    fixture_form[1]["Work authorization"] = "No"
    result, page, _ = execute(fixture_form, fields, dry=True)
    assert page.locator('[data-option="no"]').get_attribute("aria-pressed") == "true"
    assert not result.unanswered


def test_ashby_radio_group_required_heading_blocks_unknown_choice(fixture_form):
    result, page, _ = execute(fixture_form, '''
      <fieldset class="ashby-application-form-input-radio-group">
        <label class="ashby-application-form-question-title _required_example">On-site preference</label>
        <label><input type="radio" name="onsite" value="yes">I will work in office</label>
        <label><input type="radio" name="onsite" value="no">I will not work in office</label>
      </fieldset>''')
    assert result.status == "needs_human"
    assert not result.submission_attempted
    assert "On-site preference (required)" in result.unanswered


def test_ashby_label_class_marks_custom_location_required(fixture_form):
    result, page, _ = execute(fixture_form, '''
      <div class="ashby-application-form-field-entry">
        <label class="ashby-application-form-question-title _required_example">Location</label>
        <input role="combobox" placeholder="Start typing...">
      </div>''')
    assert result.status == "needs_human"
    assert "Location (required)" in result.unanswered
    assert not result.submission_attempted


def test_greenhouse_checkbox_group_requires_one_choice_not_every_choice(fixture_form):
    fixture_form[1]["Locations - Chicago"] = True
    result, page, engine = execute(fixture_form, '''
      <fieldset aria-required="true"><legend>Locations *</legend>
        <label><input type="checkbox" name="locations[]" required>Chicago</label>
        <label><input type="checkbox" name="locations[]" required>London</label>
      </fieldset>''', dry=True)
    assert not result.unanswered
    fields = page.evaluate(SCAN_JS)
    assert not any(field["invalid"] for field in fields)
    assert page.locator('input[type="checkbox"]').first.is_checked()
    assert not page.locator('input[type="checkbox"]').last.is_checked()


def test_required_checkbox_group_counts_as_one_question(fixture_form):
    result, _, _ = execute(fixture_form, '''
      <fieldset aria-required="true"><legend>Languages *</legend>
        <label><input type="checkbox" name="languages[]" required>English</label>
        <label><input type="checkbox" name="languages[]" required>Spanish</label>
      </fieldset>''', dry=True)
    assert "1 required question still needs an answer" in result.message
    assert result.unanswered == ["Languages (required)"]


def test_lever_radio_question_is_outside_options_common_ancestor(fixture_form):
    fixture_form[1]["Work authorization"] = "No"
    result, page, _ = execute(fixture_form, '''
      <li class="application-question custom-question"><div>
        <div class="application-label">Work authorization <span>✱</span></div>
        <div class="application-field full-width required-field"><ul>
          <li><label><input type="radio" name="auth" required>Yes</label></li>
          <li><label><input type="radio" name="auth" required>No</label></li>
        </ul></div>
      </div></li>''', dry=True)
    assert not result.unanswered
    assert page.locator('input[type="radio"]').last.is_checked()


def test_preselected_unknown_required_answer_is_not_candidate_evidence(fixture_form):
    result, page, _ = execute(fixture_form, '''
      <label for="citizen">US citizen *</label>
      <select id="citizen" required><option selected>Yes</option><option>No</option></select>''')
    assert result.status == "needs_human"
    assert not result.submission_attempted
    assert "US citizen (required)" in result.unanswered


ASHBY_DEGREES = '''
  <fieldset class="_container_x _fieldEntry_x ashby-application-form-input-checkbox-group">
    <label class="ashby-application-form-question-title _required_x" for="degree">Degree Type</label>
    <div class="ashby-application-form-input-checkbox-group-option"><span><input type="checkbox" id="d0"
      name="Undergraduate/Bachelor's"></span><label for="d0">Undergraduate/Bachelor's</label></div>
    <div class="ashby-application-form-input-checkbox-group-option"><span><input type="checkbox" id="d1"
      name="Master's"></span><label for="d1">Master's</label></div>
    <div class="ashby-application-form-input-checkbox-group-option"><span><input type="checkbox" id="d2"
      name="PhD"></span><label for="d2">PhD</label></div>
  </fieldset>'''


def test_ashby_checkbox_group_fieldset_is_one_question_with_prefixed_options(fixture_form):
    fixture_form[1].update({"Degree Type - Undergraduate/Bachelor's": True, "Degree Type - Master's": True,
                            "Degree Type - PhD": False})
    result, page, _ = execute(fixture_form, ASHBY_DEGREES, dry=True)
    assert not result.unanswered
    assert [page.locator(f"#d{i}").is_checked() for i in range(3)] == [True, True, False]
    labels = [field["label"] for field in page.evaluate(SCAN_JS) if field["kind"] == "checkbox"]
    assert labels == ["Degree Type - Undergraduate/Bachelor's", "Degree Type - Master's", "Degree Type - PhD"]


def test_unanswered_ashby_checkbox_group_counts_once(fixture_form):
    result, _, _ = execute(fixture_form, ASHBY_DEGREES, dry=True)
    assert result.unanswered == ["Degree Type (required)"]


# A date picker that, like react-datepicker, only registers a date typed key by key:
# a value set directly is discarded when the field loses focus.
DATE_PICKER = '''
  <div class="ashby-application-form-field-entry">
    <label class="ashby-application-form-question-title _required_x" for="grad">Graduation Date</label>
    <div class="react-datepicker-wrapper"><div class="react-datepicker__input-container">
      <input id="grad" type="text" placeholder="Pick date..." class="ashby-application-form-input-date" required>
    </div></div>
  </div>
  <script>
    const grad = document.getElementById('grad'); let typed = '';
    grad.addEventListener('focus', () => { typed = ''; });
    grad.addEventListener('keydown', e => {
      if (e.key.length === 1) typed += e.key; else if (e.key === 'Backspace') typed = typed.slice(0, -1); });
    grad.addEventListener('blur', () => {
      if (/^\\d{2}\\/\\d{2}\\/\\d{4}$/.test(typed)) { grad.value = typed; grad.setCustomValidity(''); }
      else { grad.value = ''; grad.setCustomValidity('Pick a date'); }
    });
  </script>'''


def test_date_picker_receives_a_typed_full_date(fixture_form):
    fixture_form[1]["Graduation Date"] = "May 2027"
    result, page, _ = execute(fixture_form, DATE_PICKER, dry=True)
    assert not result.unanswered
    assert page.locator("#grad").input_value() == "05/01/2027"
    assert page.locator("#grad").evaluate("e => e.validity.valid")
    assert any("Entered May 2027 as 05/01/2027" in line for line in result.log)


def test_date_picker_reports_an_unreadable_date_instead_of_guessing(fixture_form):
    fixture_form[1]["Graduation Date"] = "Spring 2027"
    result, page, _ = execute(fixture_form, DATE_PICKER, dry=True)
    assert result.unanswered == ["Graduation Date (required)"]
    assert page.locator("#grad").input_value() == ""


def test_practice_run_names_what_would_stop_a_real_send(fixture_form):
    fixture_form[1]["Postal code"] = "not a code"
    result, _, _ = execute(fixture_form, '''
      <label for="zip">Postal code *</label><input id="zip" required pattern="[0-9]{5}">''', dry=True)
    assert result.status == "dry_run"
    assert "A real send would stop at: Postal code" in result.message
    assert any("Invalid after filling: Postal code" in line for line in result.log)


def _located(fixture_form, fields, **kwargs):
    """Run a fixture form for a candidate who lives in San Francisco, CA."""
    create, _, _ = fixture_form
    engine, context, page = create(fields, **kwargs)
    engine.profile = Profile(first_name="Ada", last_name="Lovelace", email="ada@example.test",
                             city="San Francisco", state="CA", country="United States")
    return engine._run_page(context, page, engine.adapter.apply_url), page


# Lever's location box searches on key presses and keeps only a chosen suggestion.
LEVER_LOCATION = '''
  <label><div class="application-label">Current location *</div><div class="application-field">
    <input class="location-input" id="loc" name="location" required>
    <input id="picked" type="hidden" name="selectedLocation">
    <div class="dropdown-container"><div class="dropdown-results"></div></div></div></label>
  <script>
    const loc = document.getElementById('loc'), results = document.querySelector('.dropdown-results');
    loc.addEventListener('keyup', () => setTimeout(() => {
      results.innerHTML = /^san fr/i.test(loc.value) ? ['San Francisco, Agusan Del Sur, PHL', 'San Francisco, CA, USA']
        .map(t => '<div class="dropdown-location">' + t + '</div>').join('') : '';
      results.querySelectorAll('.dropdown-location').forEach(d => d.addEventListener('click', () => {
        loc.value = d.textContent; document.getElementById('picked').value = d.textContent; results.innerHTML = ''; }));
    }, 300));
    loc.addEventListener('blur', () => setTimeout(() => {
      if (!document.getElementById('picked').value) { loc.value = ''; } }, 50));
  </script>'''


def test_lever_location_is_chosen_from_keystroke_suggestions(fixture_form):
    fixture_form[1]["Current location"] = "San Francisco, CA, United States"
    result, page = _located(fixture_form, LEVER_LOCATION, dry=True)
    assert not result.unanswered
    assert page.locator("#picked").input_value() == "San Francisco, CA, USA"


def test_a_lone_box_under_a_heading_is_labelled_like_a_group_option(fixture_form):
    fixture_form[1]["Please review our privacy policy - Acknowledge/Confirm"] = True
    result, page, _ = execute(fixture_form, '''
      <fieldset aria-required="true"><legend>Please review our privacy policy *</legend>
        <label><input type="checkbox" id="ack" required>Acknowledge/Confirm</label></fieldset>''', dry=True)
    assert not result.unanswered and page.locator("#ack").is_checked()


# Ashby lists schools with their country and domain under the name.
SCHOOL_SEARCH = '''
  <label for="school">School *</label><input id="school" role="combobox" aria-autocomplete="list" required>
  <div id="menu"></div>
  <script>
    const school = document.getElementById('school'), menu = document.getElementById('menu');
    school.addEventListener('input', () => {
      const all = ['University of California, Berkeley', 'University of California, Davis', 'Other'];
      menu.innerHTML = all.filter(s => school.value && s.toLowerCase().startsWith(school.value.toLowerCase()))
        .map(s => '<div role="option">' + s + '<br>United States<br>example.edu</div>').join('');
      menu.querySelectorAll('[role=option]').forEach(o => o.addEventListener('click', () => {
        school.value = o.innerText.split('\\n')[0]; menu.innerHTML = ''; }));
    });
  </script>'''


def test_school_suggestions_match_by_their_first_line_and_fall_back_to_other(fixture_form):
    fixture_form[1]["School"] = "University of California, Berkeley"
    result, page = _located(fixture_form, SCHOOL_SEARCH, dry=True)
    assert not result.unanswered and page.locator("#school").input_value() == "University of California, Berkeley"
    fixture_form[1]["School"] = "Example State University"
    result, page = _located(fixture_form, SCHOOL_SEARCH, dry=True)
    assert page.locator("#school").input_value() == "Other"
    assert any("chose Other" in line for line in result.log)
