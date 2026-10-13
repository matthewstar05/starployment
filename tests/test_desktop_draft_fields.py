"""Every open question on a draft has an editor, including uploads and refused answers."""
from PySide6.QtCore import Qt

from jobhub.desktop.delegates import RECORD_ROLE, SUBTITLE_ROLE
from jobhub.desktop.views.applications import ApplicationsView, FilePicker


def test_uploads_get_a_file_picker_and_refused_answers_come_first(app, tmp_path):
    transcript = tmp_path / "transcript.pdf"
    transcript.write_bytes(b"%PDF-1.4")
    questions = [
        {"id": "a", "label": "Why us?", "kind": "textarea", "answer": "Mission", "source": "you", "approved": True},
        {"id": "b", "label": "Your organization", "kind": "text", "answer": "Example Lab", "source": "saved",
         "approved": False, "rejected": True, "note": "The employer's form didn't accept this answer."},
        {"id": "c", "label": "Transcript", "kind": "file", "required": True, "answer": None, "source": "none"},
    ]
    view = ApplicationsView()
    view.set_applications([], [{"id": 1, "job_id": "1", "title": "Analyst", "company": "Example",
                                "status": "needs_answers", "updated_at": "1", "questions": questions}])
    view.select_draft_for_job("1")
    rows = {row.question["id"]: row for row in view.draft_detail.rows}
    assert view.draft_detail.rows[0].question["id"] in ("b", "c")      # what needs you comes first
    assert rows["b"].value() == "Example Lab"                              # editable, not a dead end
    picker = rows["c"].editor
    assert isinstance(picker, FilePicker) and rows["c"].value() == ""
    picker._set(str(transcript))
    assert rows["c"].edited() and rows["c"].value() == str(transcript)
    assert "2 questions need" in view.draft_detail.next_step.text()


def _attempt(error):
    return {"id": 7, "job_id": "7", "title": "Engineer", "company": "Example", "status": "needs_human",
            "submission_attempted": 1, "error": error, "log": [], "screenshots": [], "unanswered": []}


def test_a_captcha_stop_offers_finish_in_browser(app):
    view = ApplicationsView()
    view.set_applications([_attempt("A CAPTCHA or security check appeared after Submit.")], [])
    view._select_application(view.records[0])
    assert not view.browser_button.isHidden() and view.reopen_button.isHidden()
    assert "Finish in browser" in view.next_step.text()


def test_a_form_that_asked_for_corrections_offers_prepare_again(app):
    view = ApplicationsView()
    view.set_applications([_attempt("The form reported: Your form needs corrections Missing entry for required "
                                    "field: Are you legally authorized to work in the United States?")], [])
    view._select_application(view.records[0])
    assert not view.reopen_button.isHidden() and view.browser_button.isHidden()
    assert "not received" in view.next_step.text()


def test_priority_sort_and_hiding_rejections(app):
    from jobhub.desktop.format import email_link, priority_rank
    def sent(i, stage, finished):
        return {"id": i, "job_id": str(i), "canonical_key": str(i), "title": "Engineer", "company": f"C{i}",
                "status": "applied", "submission_attempted": 1, "stage": stage, "finished_at": finished,
                "log": [], "screenshots": [], "unanswered": []}
    attempts = [sent(1, "rejected", "2026-10-09"), sent(2, "applied", "2026-10-08"), sent(3, "interview", "2026-10-01"),
                sent(4, "offer", "2026-09-01"), sent(5, "assessment", "2026-10-02")]
    draft = {"id": 9, "job_id": "9", "title": "Analyst", "company": "D", "status": "needs_answers", "updated_at": "2026-10-10",
             "questions": []}
    view = ApplicationsView()
    view.set_applications(attempts, [draft])
    view.sort.set_value("priority")
    view._rebuild_list()
    order = [view.list.item(i).data(RECORD_ROLE)["company"] for i in range(view.list.count())]
    assert order == ["C4", "C3", "C5", "D", "C2", "C1"]          # offer, interview, assessment … rejected last
    assert view.list.item(1).data(RECORD_ROLE)["_display_status"] == "interview"
    view.hide_rejected.setChecked(True)
    assert "C1" not in [view.list.item(i).data(RECORD_ROLE)["company"] for i in range(view.list.count())]
    assert priority_rank({"kind": "attempt", "status": "needs_human"}) < priority_rank(sent(2, "applied", ""))
    assert email_link({"stage_mail_subject": "Interview", "stage_mail_sender": "Recruiting jobs@c3.test"},
                      {"imap_host": "imap.gmail.com", "imap_username": "ada@gmail.com"}).startswith(
        "https://mail.google.com/mail/u/ada@gmail.com/#search/from%3Ajobs%40c3.test")


def test_tracker_priority_sort_and_hiding_rejections(app):
    from jobhub.desktop.views.tracker import TrackerView
    rows = [{"id": i, "title": "Engineer", "company": f"C{i}", "status": "applied", "stage": stage,
             "finished_at": f"2026-10-0{i}T12:00:00Z", "stage_updated_at": f"2026-10-0{i}T12:00:00Z"}
            for i, stage in ((1, "offer"), (2, "rejected"), (3, "applied"), (4, "interview"), (5, "assessment"),
                             (6, "interview"), (7, "withdrawn"))]
    view = TrackerView()
    view.set_rows(rows)

    def order():
        return [view.table.item(r, view.ROLE).data(SUBTITLE_ROLE) for r in range(view.table.rowCount())
                if not view.table.isRowHidden(r)]
    assert order()[0] == "C7"                                     # newest applied by default
    view.sort.choose("priority")
    assert order() == ["C1", "C6", "C4", "C5", "C3", "C7", "C2"]  # offer, interviews (newest first) … rejected last
    view.set_rows([dict(r, candidate_note="x") for r in rows])     # a refresh keeps the order
    assert order()[0] == "C1"
    view.hide_rejected.setChecked(True)
    assert "C2" not in order() and len(order()) == 6
    view.table.sortItems(view.UPDATED, Qt.SortOrder.DescendingOrder)
    assert view.sort.value() == "updated"
