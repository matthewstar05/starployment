from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_application_note_is_saved_without_changing_attempt_evidence(tmp_path, monkeypatch):
    from jobhub import config, db

    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "notes.db")
    db.close()
    db.init()
    try:
        with db.conn() as con:
            con.execute("INSERT INTO jobs(id, company, title, status) VALUES ('job-1','Example','Analyst','needs_human')")
            con.execute("INSERT INTO applications(job_id,status,submission_attempted) VALUES ('job-1','needs_human',0)")
        db.set_application_note(1, "Follow up next week")
        record = db.get_application(1)
        assert record["candidate_note"] == "Follow up next week"
        assert record["status"] == "needs_human"
        assert record["submission_attempted"] == 0
        try:
            db.set_application_note(1, "x" * 4001)
        except ValueError:
            pass
        else:
            raise AssertionError("oversized notes must be rejected")
    finally:
        db.close()


def test_applications_view_selects_application_and_preserves_note_signal():
    from PySide6.QtWidgets import QApplication
    from jobhub.desktop.ui import ApplicationsView

    app = QApplication.instance() or QApplication([])
    view = ApplicationsView()
    view.set_applications([{
        "id": 7, "title": "Analyst", "company": "Example", "status": "needs_human",
        "candidate_note": "Ask about team structure", "unanswered": ["Location"],
        "submission_attempted": False, "started_at": "2026-09-27T10:00:00Z",
        "url": "https://example.test/job", "resume_path": "/tmp/resume.pdf",
    }])
    assert view.list.count() == 1
    assert view.selected_application["id"] == 7
    assert view.status_label.text().casefold() == "needs review"
    assert "Location" in view.questions_label.text()
    assert "Submission not attempted" in view.meta_label.text()
    saved = []
    view.note_save_requested.connect(lambda app_id, note: saved.append((app_id, note)))
    view.note.setPlainText("Ask about schedule")
    view._save_note()
    assert saved == [(7, "Ask about schedule")]
    opened = []
    view.open_requested.connect(opened.append)
    view._open_posting()
    assert opened == ["https://example.test/job"]
    view.close()


def test_application_detail_distinguishes_confirmed_from_uncertain_attempts():
    from PySide6.QtWidgets import QApplication
    from jobhub.desktop.ui import ApplicationsView

    app = QApplication.instance() or QApplication([])
    view = ApplicationsView()
    view.set_applications([
        {"id": 1, "title": "Engineer", "company": "Confirmed", "status": "applied",
         "submission_attempted": True, "unanswered": []},
        {"id": 2, "title": "Engineer", "company": "Uncertain", "status": "needs_human",
         "submission_attempted": True, "unanswered": ["Email code"]},
    ])
    assert "confirmation recorded" in view.next_step.text().lower()
    assert view.questions_label.isHidden()
    view.list.setCurrentRow(1)
    assert "unconfirmed" in view.next_step.text().lower()
    assert "Email code" in view.questions_label.text()
    assert view.answer_button.isHidden()
    view.close()
