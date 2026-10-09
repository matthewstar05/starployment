"""Submissions must not look automated: employer bot filters reject instant, fingerprinted forms.

The local fixture mimics a reCAPTCHA-style filter. It accepts only a browser
without navigator.webdriver or a "HeadlessChrome" user agent, whose text fields
received real keystrokes, after some time on the page.
"""
from __future__ import annotations


from jobhub import config
from jobhub.apply import engine as engine_module
from jobhub.apply.adapters import Adapter
from jobhub.apply.engine import FormEngine
from jobhub.models import Job, Profile

FORM = """<!doctype html><html><body><script>
const loaded = Date.now(); let keys = 0;
document.addEventListener('keydown', () => keys++);
function check(e) {
  e.preventDefault();
  const f = new FormData(e.target);
  if (f.get('first') !== 'Ada' || f.get('email') !== 'ada@example.test' || f.get('city') !== 'Springfield') {
    document.body.innerHTML = '<div role="alert" class="error">Wrong values: ' + f.get('first') + ' / '
      + f.get('email') + ' / ' + f.get('city') + '</div>'; return;
  }
  const bot = navigator.webdriver || /HeadlessChrome/.test(navigator.userAgent) || keys < 5
              || Date.now() - loaded < %d;
  document.body.innerHTML = bot
    ? '<div role="alert" class="error">Your application submission was flagged as possible spam.</div>'
    : '<h1>Thank you for applying!</h1>';
}
</script><form onsubmit="check(event)">
<label for="first">First Name</label><input id="first" name="first" value="Ada Exa" required>
<label for="email">Email</label><input id="email" name="email" type="email" value="ada@example.test" required>
<label for="city">City</label><input id="city" name="city" required>
<label for="resume">Resume</label><input id="resume" name="resume" type="file" required>
<button type="submit">Submit application</button></form></body></html>"""


def run(tmp_path, pacing: bool, monkeypatch, make_pdf):
    for name in ("BROWSER_PROFILE", "SCREENSHOTS"):
        monkeypatch.setattr(config, name, tmp_path / name.lower())
        (tmp_path / name.lower()).mkdir(exist_ok=True)
    monkeypatch.setattr(config, "HEADLESS", True)
    monkeypatch.setenv("JOBHUB_HUMAN_PACING", "1" if pacing else "0")
    monkeypatch.setattr(engine_module, "MIN_PAGE_SECONDS", 3)
    monkeypatch.setattr(engine_module.db, "mark_submission_attempted", lambda job_id: None)
    form = tmp_path / "form.html"
    form.write_text(FORM % 2500)
    resume = make_pdf(tmp_path / "resume.pdf", ["Ada Example"])
    profile = Profile(first_name="Ada", last_name="Example", email="ada@example.test", city="Springfield",
                      resume_path=str(resume))
    job = Job(source="fixture", ats="ashby", company="Fixture", title="Engineer", url="https://example.test/job")
    engine = FormEngine(job, profile, {}, str(resume), Adapter("local-fixture", form.as_uri(), settle_ms=0), dry_run=False)
    return engine.run()


def test_paced_browser_passes_a_bot_filter(tmp_path, monkeypatch, make_pdf):
    result = run(tmp_path, True, monkeypatch, make_pdf)
    assert result.status == "applied", result.message


def test_an_unpaced_submission_is_flagged_and_explained(tmp_path, monkeypatch, make_pdf):
    result = run(tmp_path, False, monkeypatch, make_pdf)
    assert result.status == "needs_human"
    assert "spam filter rejected this automated submission" in result.message
    assert "submit it yourself" in result.message
