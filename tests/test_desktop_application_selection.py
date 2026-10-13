"""Saving a draft while changing selection must not re-enter the save."""
from copy import deepcopy

import pytest

from jobhub.desktop.views.applications import ApplicationsView


def draft(number):
    return {"id": number, "job_id": str(number), "title": "Analyst", "company": "Example",
            "status": "needs_answers", "updated_at": str(number),
            "questions": [{"id": "q", "label": "Example answer", "kind": "text", "answer": "", "source": "you"}]}


@pytest.mark.parametrize("target", ["draft", "attempt"])
def test_selection_saves_once_even_when_save_refreshes_the_list(app, target):
    view = ApplicationsView()
    drafts = [draft(1), draft(2)] if target == "draft" else [draft(1)]
    attempts = [] if target == "draft" else [
        {"id": 3, "job_id": "3", "title": "Analyst", "company": "Example", "status": "applied"}]
    view.set_applications(attempts, deepcopy(drafts))
    view.select_draft_for_job("1")
    view.draft_detail.rows[0].editor.setText("Saved answer")
    saves = []

    def save(draft_id, edits):
        saves.append((draft_id, edits))
        if len(saves) > 3:   # bound the old recursive bug so a regression cannot hang pytest
            return
        drafts[0]["updated_at"] = f"saved-{len(saves)}"
        drafts[0]["questions"][0]["answer"] = edits["q"]
        if target == "draft":
            drafts[1]["questions"][0]["answer"] = "Shared saved answer"
        view.set_applications(attempts, deepcopy(drafts))

    view.save_draft_requested.connect(save)
    if target == "draft":
        view.select_draft_for_job("2")
        assert view.draft_detail.draft["id"] == 2
        assert view.draft_detail.rows[0].value() == "Shared saved answer"
    else:
        view.select_application_id(3)
        assert view.selected_application["id"] == 3
        assert view.draft_detail.draft is None
    assert saves == [(1, {"q": "Saved answer"})]
    assert not view._selecting_row


def test_refresh_of_current_draft_preserves_unsaved_answers(app):
    view = ApplicationsView()
    record = draft(1)
    view.set_applications([], [deepcopy(record)])
    view.draft_detail.rows[0].editor.setText("Still editing")
    saves = []
    view.save_draft_requested.connect(lambda *args: saves.append(args))
    record["updated_at"] = "background refresh"
    view.set_applications([], [record])
    assert view.draft_detail.rows[0].value() == "Still editing"
    assert view.draft_detail.dirty()
    assert not saves
