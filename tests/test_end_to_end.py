"""Whole local pipeline smoke test. Only the employer URL is replaced by a fixture.

A real PDF upload is parsed, a real Chromium browser fills and submits the local
form, and the API persists confirmation evidence and blocks a second submission.
No network employer requests or real candidate data are used.
"""
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from reportlab.pdfgen.canvas import Canvas

from jobhub import config, db, scheduler
import jobhub.apply as dispatcher
from jobhub.apply.adapters import Adapter
from jobhub.models import Job
from jobhub.server import app
from jobhub.setup import save_setup


def test_resume_upload_to_confirmed_application_and_duplicate_block(tmp_path, monkeypatch):
    db.close()
    for name, value in {'DATA': tmp_path, 'DB_PATH': tmp_path / 'e2e.db',
                        'RESUMES': tmp_path / 'resumes', 'TAILORED': tmp_path / 'tailored',
                        'SCREENSHOTS': tmp_path / 'shots', 'BROWSER_PROFILE': tmp_path / 'browser'}.items():
        monkeypatch.setattr(config, name, value)
        if name != 'DB_PATH': value.mkdir(exist_ok=True)
    monkeypatch.setattr(app.state, 'scheduler_enabled', False, raising=False)
    monkeypatch.setattr(config, 'HEADLESS', True)
    scheduler.STATE.running_apply = None
    form = tmp_path / 'employer.html'
    form.write_text('''<!doctype html><html><body><form onsubmit="event.preventDefault();
      const f=new FormData(this); if(f.get('first')==='Ada' && f.get('last')==='Lovelace'
      && f.get('email')==='ada@example.test' && f.get('resume').size>0)
      document.body.innerHTML='<h1>Your application has been submitted</h1>';
      else document.body.innerHTML='<h1>Invalid fixture data</h1>'">
      <label for="first">First Name</label><input id="first" name="first" required>
      <label for="last">Last Name</label><input id="last" name="last" required>
      <label for="email">Email</label><input id="email" name="email" type="email" required>
      <label for="resume">Resume</label><input id="resume" name="resume" type="file" required>
      <button type="submit">Submit application</button></form></body></html>''')
    monkeypatch.setattr(dispatcher, 'adapter_for', lambda job: Adapter('local-fixture', form.as_uri(), settle_ms=0))
    pdf = BytesIO()
    canvas = Canvas(pdf)
    for y, line in [(750, 'Ada Lovelace'), (730, 'ada@example.test'), (690, 'SKILLS'), (670, 'Python, SQL, C++')]:
        canvas.drawString(50,y,line)
    canvas.save()
    try:
        with TestClient(app, base_url='http://127.0.0.1') as client:
            upload = client.post('/api/profile/resume?use_llm=false', files={'file': ('candidate.pdf', pdf.getvalue(), 'application/pdf')})
            assert upload.status_code == 200, upload.text
            assert upload.json()['email'] == 'ada@example.test'
            job = Job(source='fixture', ats='greenhouse', company='Fixture employer', title='Software Engineer',
                      url='https://job-boards.greenhouse.io/fixture/jobs/123', description='Python and SQL engineering')
            db.upsert_jobs([job])
            practice = client.post(f'/api/jobs/{job.id}/apply?dry_run=true')
            assert practice.status_code == 200, practice.text
            record = client.get('/api/applications').json()[0]
            assert record['status'] == 'dry_run'
            assert record['submission_attempted'] == 0
            submitted = client.post(f'/api/jobs/{job.id}/apply?dry_run=false')
            assert submitted.status_code == 200, submitted.text
            record = client.get('/api/applications').json()[0]
            assert record['status'] == 'applied', record
            assert record['submission_attempted'] == 1
            assert Path(record['resume_path']).is_file()
            assert any(s.endswith('success.png') for s in record['screenshots'])
            assert all((config.SCREENSHOTS / s).is_file() for s in record['screenshots'])
            assert client.get(f'/api/jobs/{job.id}').json()['status'] == 'applied'
            assert client.post(f'/api/jobs/{job.id}/apply').status_code == 409
    finally:
        db.close()


def test_desktop_setup_answers_reach_a_local_employer_form(tmp_path, monkeypatch):
    """Candidate setup, custom answers, browser fill, and durable confirmation."""
    db.close()
    for name, value in {'DATA': tmp_path, 'DB_PATH': tmp_path / 'desktop-e2e.db',
                        'RESUMES': tmp_path / 'resumes', 'TAILORED': tmp_path / 'tailored',
                        'SCREENSHOTS': tmp_path / 'shots', 'BROWSER_PROFILE': tmp_path / 'browser'}.items():
        monkeypatch.setattr(config, name, value)
        if name != 'DB_PATH':
            value.mkdir(exist_ok=True)
    monkeypatch.setattr(config, 'HEADLESS', True)
    db.init()
    original = tmp_path / 'candidate.pdf'
    pdf = Canvas(str(original))
    pdf.drawString(50, 750, 'Ada Lovelace')
    pdf.drawString(50, 730, 'ada@example.test')
    pdf.save()
    profile, settings, missing = save_setup(
        resume_pdf=str(original),
        profile_patch={'city': 'London', 'country': 'United Kingdom'},
        settings_patch={'target_titles': ['Product Manager'], 'preferred_locations': ['London'],
                        'answers': {'field_answers': {'Preferred working pattern': 'Hybrid',
                                                       'I agree to this exact privacy notice': True}}},
    )
    assert missing == [] and settings['setup_complete']
    form = tmp_path / 'employer.html'
    form.write_text('''<!doctype html><html><body><form onsubmit="event.preventDefault();
      const f=new FormData(this); if(f.get('first')==='Ada' && f.get('last')==='Lovelace'
      && f.get('email')==='ada@example.test' && f.get('city')==='London'
      && f.get('country')==='United Kingdom' && f.get('pattern')==='Hybrid'
      && f.get('privacy')==='on' && f.get('resume').size>0)
      document.body.innerHTML='<h1>Your application has been submitted</h1>';
      else document.body.innerHTML='<h1>Invalid fixture data</h1>'">
      <label for="first">First Name</label><input id="first" name="first" required>
      <label for="last">Last Name</label><input id="last" name="last" required>
      <label for="email">Email</label><input id="email" name="email" type="email" required>
      <label for="city">City</label><input id="city" name="city" required>
      <label for="country">Country</label><input id="country" name="country" required>
      <label for="pattern">Preferred working pattern</label><select id="pattern" name="pattern" required>
        <option value="">Choose</option><option>Remote</option><option>Hybrid</option></select>
      <label><input name="privacy" type="checkbox" required> I agree to this exact privacy notice</label>
      <label for="resume">Resume</label><input id="resume" name="resume" type="file" required>
      <button type="submit">Submit application</button></form></body></html>''')
    monkeypatch.setattr(dispatcher, 'adapter_for', lambda job: Adapter('local-fixture', form.as_uri(), settle_ms=0))
    job = Job(source='fixture', ats='greenhouse', company='Fixture employer', title='Product Manager',
              url='https://job-boards.greenhouse.io/fixture/jobs/456', description='Product launch planning')
    db.upsert_jobs([job])
    try:
        result = scheduler.apply_one(job.id)
        assert result['status'] == 'applied', result
        record = db.list_applications()[0]
        assert record['status'] == 'applied'
        assert record['submission_attempted'] == 1
        assert any(name.endswith('success.png') for name in record['screenshots'])
        assert scheduler.apply_one(job.id)['status'] == 'blocked'
    finally:
        db.close()
