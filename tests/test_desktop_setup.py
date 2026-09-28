"""Desktop setup and candidate-owned answer-bank regressions."""
from __future__ import annotations

from pathlib import Path

import pytest

from jobhub import config, db, scheduler
from jobhub.answers import Q, rule_answer
from jobhub.models import Job
from jobhub.setup import missing_setup, save_setup
from jobhub.sources.base import title_relevant


@pytest.fixture
def local_store(tmp_path, monkeypatch):
    db.close()
    for name, value in {
        "DATA": tmp_path, "DB_PATH": tmp_path / "test.db", "RESUMES": tmp_path / "resumes",
        "TAILORED": tmp_path / "tailored", "SCREENSHOTS": tmp_path / "screenshots",
    }.items():
        monkeypatch.setattr(config, name, value)
    config.RESUMES.mkdir()
    config.TAILORED.mkdir()
    config.SCREENSHOTS.mkdir()
    db.init()
    yield tmp_path
    db.close()


def _resume(path: Path) -> Path:
    from reportlab.pdfgen import canvas
    cv = canvas.Canvas(str(path))
    cv.drawString(72, 730, "Ada Lovelace")
    cv.drawString(72, 710, "ada@example.test")
    cv.drawString(72, 690, "Product manager with launch planning experience")
    cv.save()
    return path


def test_setup_imports_resume_and_saves_user_facts_and_arbitrary_answers(local_store):
    resume = _resume(local_store / "resume.pdf")
    profile, settings, missing = save_setup(
        resume_pdf=str(resume),
        profile_patch={"city": "London", "country": "United Kingdom",
                       "custom_facts": {"Portfolio focus": "Accessibility"}},
        settings_patch={"target_titles": ["product manager"], "preferred_locations": ["London"],
                        "years_experience": 4.0, "auto_apply": True,
                        "answers": {"requires_sponsorship": "no", "field_answers": {
                            "What is your preferred working pattern?": "Hybrid",
                            "I agree to this exact privacy notice": True}}},
    )
    assert missing == []
    assert settings["setup_complete"] is True
    assert settings["auto_apply"] is True
    assert settings["answers"]["requires_sponsorship"] == "No"
    assert "requires_sponsorship" in settings["answers"]["confirmed_fields"]
    assert Path(profile.resume_path).is_file()
    assert profile.custom_facts["Portfolio focus"] == "Accessibility"
    assert rule_answer(Q("Preferred working pattern", "select", ["Remote", "Hybrid", "Onsite"]),
                       profile, settings, None).value == "Hybrid"
    assert rule_answer(Q("I agree to this exact privacy notice *", "checkbox"),
                       profile, settings, None).value is True


def test_missing_required_candidate_fact_keeps_autonomy_locked(local_store):
    resume = _resume(local_store / "resume.pdf")
    profile, settings, missing = save_setup(
        resume_pdf=str(resume),
        profile_patch={"country": "United Kingdom"},
        settings_patch={"target_titles": ["product manager"], "auto_apply": True},
    )
    assert "city" in missing
    assert settings["setup_complete"] is False
    assert scheduler.auto_apply_batch() == []
    assert "city" in missing_setup(profile, settings)


def test_discovery_uses_candidate_profession_instead_of_technical_whitelist():
    settings = {"target_titles": ["Product Manager"], "exclude_title_words": [], "include_internships": False}
    assert title_relevant("Senior Product Manager, Growth", settings)
    assert not title_relevant("Software Engineer", settings)


def test_setup_wizard_emits_candidate_owned_payload(local_store, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from jobhub.desktop.ui import SetupWizard
    app = QApplication.instance() or QApplication([])
    wizard = SetupWizard({}, db.get_settings())
    wizard.fields["first_name"].setText("Ada")
    wizard.custom_facts.setPlainText("Favorite domain | accessibility")
    wizard.custom_answers.setPlainText("I agree to this notice | true\nPreferred team | Platform")
    wizard.target_titles.setPlainText("product manager")
    wizard.auto_apply.setChecked(True)
    wizard.years_experience.setValue(4)
    wizard.confirm_years_experience.setChecked(True)
    captured = []
    wizard.completed.connect(captured.append)
    wizard._submit()
    assert captured[0]["profile"]["custom_facts"] == {"Favorite domain": "accessibility"}
    assert captured[0]["settings"]["answers"]["field_answers"]["I agree to this notice"] is True
    assert captured[0]["settings"]["auto_apply"] is True
    assert captured[0]["settings"]["answers"]["years_experience"] == "4"
    assert "years_experience" in captured[0]["settings"]["answers"]["confirmed_fields"]
    wizard.close()


def test_setup_wizard_previews_resume_facts_before_save(local_store, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from jobhub.desktop.ui import SetupWizard

    app = QApplication.instance() or QApplication([])
    wizard = SetupWizard({}, db.get_settings())
    wizard._preview_resume(str(_resume(local_store / "preview.pdf")))
    assert wizard.fields["first_name"].text() == "Ada"
    assert wizard.fields["last_name"].text() == "Lovelace"
    assert wizard.fields["email"].text() == "ada@example.test"
    assert wizard.fields["country"].text() == ""
    assert wizard.resume_path.text().endswith("preview.pdf")
    wizard.close()


def test_application_questions_open_in_answer_bank_without_saving_blanks(local_store, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from jobhub.desktop.ui import SetupWizard

    app = QApplication.instance() or QApplication([])
    wizard = SetupWizard({}, db.get_settings(), pending_questions=[
        "What is your work pattern? (required)",
        "Why this company? (required)",
        "What is your work pattern? (required)",
    ])
    assert wizard.tabs.currentIndex() == 3
    assert wizard.pending_hint.isHidden() is False
    assert wizard.custom_answers.toPlainText().count("What is your work pattern? | ") == 1
    wizard.custom_answers.setPlainText(
        wizard.custom_answers.toPlainText().replace(
            "What is your work pattern? | ", "What is your work pattern? | Hybrid"
        )
    )
    captured = []
    wizard.completed.connect(captured.append)
    wizard._submit()
    assert captured[0]["settings"]["answers"]["field_answers"] == {
        "What is your work pattern?": "Hybrid",
    }
    wizard.close()


def test_review_needed_job_retries_only_before_submit_boundary(local_store):
    early = Job(source="fixture", ats="greenhouse", company="Example", title="Product Manager",
                url="https://job-boards.greenhouse.io/example/jobs/111")
    uncertain = Job(source="fixture", ats="greenhouse", company="Other", title="Product Manager",
                    url="https://job-boards.greenhouse.io/other/jobs/222")
    db.upsert_jobs([early, uncertain])
    for job, attempted in ((early, False), (uncertain, True)):
        app_id = db.reserve_application(job.id)
        db.start_application(app_id, job.id)
        db.finish_application(app_id, "needs_human", [], [], ["Country"], submission_attempted=attempted)
    assert db.requeue_preflight_needs_human() == 1
    assert db.get_job(early.id).status == "new"
    assert db.get_job(uncertain.id).status == "needs_human"


def test_jobs_view_review_filter_and_safe_retry_button(local_store, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from jobhub.desktop.ui import JobsView
    app = QApplication.instance() or QApplication([])
    review = Job(source="fixture", company="Example", title="Product Manager",
                 url="https://example.test/review", status="needs_human")
    fresh = Job(source="fixture", company="Another", title="Product Manager",
                url="https://example.test/new", status="new")
    view = JobsView()
    view.set_jobs([review, fresh], retryable_ids={review.id})
    assert view.table.item(0, JobsView.ACTION).text() == "Retry"
    assert view.table.item(1, JobsView.ACTION).text() == "Apply"
    requested = []
    view.apply_requested.connect(requested.append)
    view._cell_clicked(0, JobsView.ACTION)
    assert requested == [review.id]
    view.filter.setCurrentIndex(3)
    assert view.selected_status == "needs_human"
    assert not view.table.isRowHidden(0)
    assert view.table.isRowHidden(1)
    view.close()


def test_jobs_search_and_pagination_reach_beyond_first_500(local_store, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from jobhub.desktop.ui import JobsView

    app = QApplication.instance() or QApplication([])
    jobs = [
        Job(source="fixture", company="Example", title=f"Role {i}",
            url=f"https://example.test/jobs/{i}")
        for i in range(520)
    ]
    jobs[-1].title = "Only searchable role"
    db.upsert_jobs(jobs)
    assert db.count_jobs() == 520
    assert len(db.list_jobs(limit=100, offset=500)) == 20
    assert db.count_jobs(q="Only searchable") == 1
    assert db.list_jobs(q="Only searchable")[0].title == "Only searchable role"

    view = JobsView()
    view.set_jobs(jobs[:100], total_count=520)
    changed = []
    view.query_changed.connect(lambda: changed.append(view.page))
    assert view.page_label.text() == "1–100 of 520 jobs"
    view.next_page.click()
    assert changed == [1]
    assert view.page == 1
    view.set_jobs(db.list_jobs(limit=100, offset=100), total_count=520)
    assert view.page_label.text() == "101–200 of 520 jobs"
    view.close()
