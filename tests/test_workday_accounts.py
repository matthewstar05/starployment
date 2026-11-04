"""Workday sign-ins: the local vault, account creation, saved sign-in, and unattended sends.

Every page here is a local fixture; nothing contacts an employer or the real Keychain.
"""
import json
import os
import stat

import pytest
from playwright.sync_api import sync_playwright

from jobhub import config, vault
from jobhub.answers import Answer
from jobhub.apply import apply_to_job
from jobhub.apply import engine as engine_module
from jobhub.apply import workday as workday_module
from jobhub.apply.adapters import Adapter
from jobhub.models import Job, Profile


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
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    fake = FakeVault()
    monkeypatch.setattr(vault, "_vault", lambda: fake)
    return fake


def test_generated_passwords_meet_workday_rules():
    for _ in range(50):
        password = vault.generate_password()
        assert len(password) == 20 and password[0].isalnum()
        assert any(c.islower() for c in password) and any(c.isupper() for c in password)
        assert any(c.isdigit() for c in password) and any(c in vault.SPECIALS for c in password)
    assert vault.generate_password() != vault.generate_password()


def test_vault_keeps_passwords_out_of_the_accounts_file(store, assert_windows_private_acl):
    vault.save_account("bank", "Ada@Example.test", "S3cret!pass", company="Bank", status="creating")
    accounts = config.DATA / "accounts.json"
    assert "S3cret" not in accounts.read_text()
    if os.name == "nt":
        assert_windows_private_acl(accounts)
    else:
        assert stat.S_IMODE(accounts.stat().st_mode) == 0o600
    assert vault.password_for("bank") == "S3cret!pass"
    vault.set_status("bank", "active", used=True)
    [record] = vault.list_accounts()
    assert record["email"] == "ada@example.test" and record["status"] == "active" and record["last_used"]
    vault.delete_account("bank")
    assert vault.list_accounts() == [] and store.items == {}


def test_tenant_comes_only_from_workday_hosts():
    assert vault.tenant_of("https://citi.wd5.myworkdayjobs.com/en-US/2/job/x") == "citi"
    with pytest.raises(ValueError):
        vault.tenant_of("https://citi.wd5.myworkdayjobs.com.evil.test/x")


def test_passwords_stay_hidden_without_a_system_prompt(monkeypatch):
    monkeypatch.setattr(vault.sys, "platform", "linux")
    assert vault.authenticate("show a password") == (False, "Showing saved passwords needs macOS.")


def test_settings_validate_workday_options():
    settings = config.merge_settings(config.DEFAULT_SETTINGS, {"workday": {"create_accounts": True, "send": True}})
    assert config.validate_settings(settings)["workday"] == {"create_accounts": True, "send": True,
                                                             "check_portal": True}
    assert config.DEFAULT_SETTINGS["workday"] == {"create_accounts": False, "send": False, "check_portal": True}
    # Settings saved before Candidate Home tracking existed still validate.
    assert config.validate_settings({**settings, "workday": {"create_accounts": True, "send": True}})


def test_send_gates_follow_the_setting(tmp_path, monkeypatch):
    from jobhub import prepare
    drafts = [{"id": 1, "status": "ready", "ats": "workday", "company": "Fixture Analytics"}]
    assert prepare.bulk_send_candidates(drafts, [])[0] == []
    assert prepare.bulk_send_candidates(drafts, [], {"workday": {"send": True}})[0] == drafts
    url = "https://bank.wd5.myworkdayjobs.com/careers/job/NY/IB-Analyst_R1"
    job = Job(source="workday", ats="workday", company="Bank", title="Analyst", url=url, apply_url=url)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    ran = []
    monkeypatch.setattr(workday_module.WorkdayEngine, "run", lambda self: ran.append(self.assist) or
                        self._result("needs_human", "stub"))
    apply_to_job(job, Profile(email="a@example.test"), {"dry_run": False, "workday": {"send": True}}, str(resume))
    assert ran == [False]


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


ACCOUNT_PAGE = """<!doctype html><html><body>
<div id="signin" data-automation-id="signInContent">
  <input data-automation-id="email" type="email"><input data-automation-id="password" type="password">
  <button data-automation-id="signInSubmitButton" onclick="signIn()">Sign In</button>
  <button data-automation-id="createAccountLink" onclick="showCreate()">Create Account</button>
</div>
<div id="create" style="display:none">
  <input data-automation-id="email" type="email" id="ce"><input data-automation-id="password" type="password" id="cp">
  <input data-automation-id="verifyPassword" type="password" id="cv">
  <input type="checkbox" data-automation-id="createAccountCheckbox" id="terms">
  <button data-automation-id="createAccountSubmitButton" onclick="create()">Create Account</button>
</div>
<div data-automation-id="applyFlowPage" id="flow" style="display:none">
  <div data-automation-id="progressBarActiveStep" id="step">My Information</div>
  <div id="info">
    <div data-automation-id="formField-legalNameSection_firstName"><label for="fn">First Name*</label><input id="fn" type="text"></div>
    __EXTRA__
  </div>
  <div id="error" data-automation-id="errorMessage" style="display:none">Errors found</div>
  <button data-automation-id="bottom-navigation-next-button" id="next" onclick="next()">Save and Continue</button>
</div>
<div id="done" role="dialog" style="display:none">Application Submitted</div>
<script>
  const show = (id, on) => document.getElementById(id).style.display = on ? 'block' : 'none';
  function showCreate() { show('signin', false); show('create', true); }
  function signIn() {
    const box = document.getElementById('signin');
    window.signedIn = [box.querySelector('[type=email]').value, box.querySelector('[type=password]').value];
    show('signin', false); show('flow', true);
  }
  function create() {
    const pw = document.getElementById('cp').value;
    if (!pw || pw !== document.getElementById('cv').value || !document.getElementById('terms').checked) return;
    window.created = [document.getElementById('ce').value, pw];
    show('create', false); show('flow', true);
  }
  function next() {
    window.saved = (window.saved || 0) + 1;
    const step = document.getElementById('step');
    if (step.innerText === 'Review') { window.submitted = (window.submitted || 0) + 1; show('done', true); return; }
    if (!document.getElementById('fn').value) { show('error', true); return; }
    step.innerText = 'Review'; show('info', false); document.getElementById('next').innerText = 'Submit';
  }
</script></body></html>"""


def _engine(tmp_path, monkeypatch, html, settings):
    page_path = tmp_path / "workday.html"
    page_path.write_text(html)
    url = page_path.as_uri()
    monkeypatch.setattr(engine_module.config, "SCREENSHOTS", tmp_path / "shots")
    attempted = []
    monkeypatch.setattr(workday_module.db, "mark_submission_attempted", attempted.append)
    monkeypatch.setattr(workday_module, "SEND_SECONDS", 40)
    monkeypatch.setattr(workday_module.WorkdayEngine, "_tenant", lambda self: "bank")
    monkeypatch.setattr(workday_module, "rule_answer", lambda q, *args:
                        Answer("Ada", "rule") if q.label == "First Name" else None)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    job = Job(source="fixture", ats="workday", company="Bank", title="Analyst", url=url, apply_url=url)
    engine = workday_module.WorkdayEngine(job, Profile(email="ada@example.test"), settings, str(resume),
                                          Adapter("workday", url, assist_only=True), False, assist=False)
    return engine, url, attempted


SEND = {"workday": {"create_accounts": True, "send": True}}


def test_unattended_send_creates_the_account_saves_each_page_and_submits(tmp_path, monkeypatch, store, browser):
    engine, url, attempted = _engine(tmp_path, monkeypatch, ACCOUNT_PAGE.replace("__EXTRA__", ""), SEND)
    context = browser.new_context()
    page = context.new_page()
    try:
        result = engine._run_page(context, page, url)
        assert result.status == "applied", (result.message, engine.log)
        email, password = page.evaluate("window.created")
        assert email == "ada@example.test" and password == vault.password_for("bank")
        assert vault.get_account("bank")["status"] == "active"
        assert page.evaluate("window.submitted") == 1 and page.evaluate("window.saved") == 2
        assert attempted == [engine.job.id]
        assert not any(password in line for line in engine.log)
    finally:
        context.close()


def test_saved_sign_in_is_used_and_an_unknown_question_stops_before_saving(tmp_path, monkeypatch, store, browser):
    extra = ('<div data-automation-id="formField-previousWorker"><label>Have you previously worked for Bank?*</label>'
             '<input type="radio" name="pw" id="y"><label for="y">Yes</label>'
             '<input type="radio" name="pw" id="n"><label for="n">No</label></div>')
    vault.save_account("bank", "ada@example.test", "Saved-pass1!", company="Bank", status="active")
    engine, url, _ = _engine(tmp_path, monkeypatch, ACCOUNT_PAGE.replace("__EXTRA__", extra), SEND)
    context = browser.new_context()
    page = context.new_page()
    try:
        result = engine._run_page(context, page, url)
        assert page.evaluate("window.signedIn") == ["ada@example.test", "Saved-pass1!"]
        assert page.evaluate("window.created || null") is None
        assert result.status == "needs_human" and "previously worked" in result.message
        assert page.evaluate("window.saved || 0") == 0
    finally:
        context.close()


def test_without_send_an_unattended_workday_attempt_never_opens_the_form(tmp_path, monkeypatch, store, browser):
    engine, url, attempted = _engine(tmp_path, monkeypatch, ACCOUNT_PAGE.replace("__EXTRA__", ""),
                                     {"workday": {"create_accounts": True, "send": False}})
    context = browser.new_context()
    page = context.new_page()
    try:
        result = engine._run_page(context, page, url)
        assert result.status == "needs_human" and "Finish in browser" in result.message
        assert attempted == [] and vault.list_accounts() == []
    finally:
        context.close()


def test_practice_runs_create_no_account(tmp_path, monkeypatch, store, browser):
    engine, url, attempted = _engine(tmp_path, monkeypatch, ACCOUNT_PAGE.replace("__EXTRA__", ""), SEND)
    engine.dry_run = True
    context = browser.new_context()
    page = context.new_page()
    try:
        result = engine._run_page(context, page, url)
        assert result.status == "dry_run"
        assert attempted == [] and vault.list_accounts() == []
        assert page.evaluate("window.created || null") is None
    finally:
        context.close()


def test_accounts_file_is_plain_json_list(store):
    vault.save_account("bank", "ada@example.test", "S3cret!pass")
    assert json.loads((config.DATA / "accounts.json").read_text())["bank"]["tenant"] == "bank"


def test_finish_in_browser_saves_the_password_the_candidate_signs_in_with(tmp_path, monkeypatch, store, browser):
    html = ACCOUNT_PAGE.replace("__EXTRA__", "").replace(
        '<button data-automation-id="createAccountLink" onclick="showCreate()">Create Account</button>', "").replace(
        "</script>", """
  setTimeout(() => { const box = document.getElementById('signin');
    box.querySelector('[type=email]').value = 'ada@example.test';
    box.querySelector('[type=password]').value = 'Typed-pass9!'; }, 1500);
  setTimeout(signIn, 3500);
  const finish = setInterval(() => { if (document.getElementById('fn').value) { clearInterval(finish); show('done', true); } }, 300);
</script>""")
    engine, url, _ = _engine(tmp_path, monkeypatch, html, SEND)
    engine.assist = True
    monkeypatch.setattr(workday_module, "ASSIST_SECONDS", 30)
    context = browser.new_context()
    page = context.new_page()
    try:
        result = engine._run_page(context, page, url)
        assert result.status == "applied", (result.message, engine.log)
        assert vault.password_for("bank") == "Typed-pass9!"
        assert vault.get_account("bank")["note"] == "Saved from your sign-in"
        assert not any("Typed-pass9!" in line for line in engine.log)
    finally:
        context.close()


def test_settings_and_sign_ins_sheet():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from jobhub.desktop.views.passwords import HIDDEN, PasswordsSheet
    from jobhub.desktop.views.settings import SettingsView
    view = SettingsView()
    view.set_settings(config.merge_settings(config.DEFAULT_SETTINGS, {"workday": {"create_accounts": True}}))
    assert view.workday_accounts.isChecked() and not view.workday_send.isChecked()
    view.workday_send.setChecked(True)
    patches = []
    view.save_requested.connect(patches.append)
    view._save()
    assert patches[-1]["workday"] == {"create_accounts": True, "send": True, "check_portal": True}
    sheet = PasswordsSheet()
    sheet.set_accounts([{"tenant": "bank", "company": "Bank", "email": "ada@example.test", "status": "active"}])
    asked = []
    sheet.reveal_requested.connect(asked.append)
    row = sheet._rows["bank"]
    row.show_button.click()
    assert asked == ["bank"] and row.secret.text() == HIDDEN
    sheet.show_password("bank", "S3cret!pass")
    assert row.secret.text() == "S3cret!pass"
    row.hide_password()
    assert row.secret.text() == HIDDEN


def test_own_sign_ins_are_added_and_exported_for_passwords(store):
    import csv
    record = vault.add_own("https://bank.wd5.myworkdayjobs.com/External", "Ada@Example.test", "Mine-pass1!", "Bank")
    assert record["tenant"] == "bank" and record["host"] == "bank.wd5.myworkdayjobs.com"
    assert record["email"] == "ada@example.test" and vault.password_for("bank") == "Mine-pass1!"
    assert vault.site_url(record) == "https://bank.wd5.myworkdayjobs.com/"
    with pytest.raises(ValueError):
        vault.add_own("https://example.test/jobs", "ada@example.test", "x")      # not a Workday address
    vault.add_own("bank.wd5.myworkdayjobs.com", "ada@example.test", "Changed-pass2!")  # change the password
    assert vault.password_for("bank") == "Changed-pass2!" and len(vault.list_accounts()) == 1
    path, count = vault.export_for_passwords()
    assert count == 1 and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    rows = list(csv.reader(path.open(encoding="utf-8")))
    assert rows[0] == list(vault.PASSWORDS_HEADER)
    assert rows[1][:4] == ["Bank (Workday)", "https://bank.wd5.myworkdayjobs.com/", "ada@example.test", "Changed-pass2!"]
    vault.clear_export()
    assert not path.exists() and not path.parent.exists()
    assert "Changed-pass2!" not in (config.DATA / "accounts.json").read_text()


def test_sign_ins_sheet_adds_and_edits_without_showing_the_password(store):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from jobhub.desktop.views.passwords import PasswordsSheet
    sheet = PasswordsSheet(default_email="ada@example.test")
    sheet.set_accounts([])
    assert not sheet.export_button.isEnabled()
    saved = []
    sheet.save_requested.connect(saved.append)
    sheet.add_button.click()
    assert sheet.form.isVisibleTo(sheet) and sheet.form.email.text() == "ada@example.test"
    sheet.form.save.click()
    assert saved == [] and "Enter the Workday address" in sheet.message.text()
    sheet.form.address.setText("https://bank.wd5.myworkdayjobs.com")
    sheet.form.password.setText("Mine-pass1!")
    sheet.form.save.click()
    assert saved[-1] == {"address": "https://bank.wd5.myworkdayjobs.com", "company": "",
                         "email": "ada@example.test", "password": "Mine-pass1!"}
    sheet.saved()
    assert not sheet.form.isVisibleTo(sheet) and sheet.form.password.text() == ""
    account = {"tenant": "bank", "company": "Bank", "email": "ada@example.test", "status": "exists",
               "host": "bank.wd5.myworkdayjobs.com"}
    sheet.set_accounts([account])
    assert sheet.export_button.isEnabled() and sheet._rows["bank"].open.isVisibleTo(sheet._rows["bank"])
    sheet._rows["bank"].edit.click()
    assert sheet.form.address.isReadOnly() and sheet.form.password.text() == ""


def test_a_tenant_naming_its_footer_button_page_footer_next_still_moves_on(tmp_path, monkeypatch, store, browser):
    html = ACCOUNT_PAGE.replace("__EXTRA__", "").replace('data-automation-id="bottom-navigation-next-button"',
                                                          'data-automation-id="pageFooterNextButton"')
    engine, url, attempted = _engine(tmp_path, monkeypatch, html, SEND)
    context = browser.new_context()
    page = context.new_page()
    try:
        result = engine._run_page(context, page, url)
        assert result.status == "applied", (result.message, engine.log)
        assert page.evaluate("window.submitted") == 1 and page.evaluate("window.saved") == 2
    finally:
        context.close()
