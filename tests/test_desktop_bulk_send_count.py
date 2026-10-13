"""Send ready counts only drafts that the bulk-send action can queue."""
from copy import deepcopy

import pytest

from jobhub.desktop.views.applications import ApplicationsView


def draft(number, ats="greenhouse", status="ready", **extra):
    return {"id": number, "job_id": str(number), "company": "Example", "title": "Analyst",
            "ats": ats, "status": status, "questions": [], **extra}


@pytest.mark.parametrize("workday_status", ["ready", "approved"])
def test_send_ready_excludes_workday_from_mixed_batch_and_disables_when_only_workday_remains(app, workday_status):
    view = ApplicationsView()
    eligible = [draft(i) for i in range(29)]
    workday = [draft(i, "workday", workday_status) for i in range(29, 49)]
    view.set_applications([], eligible + workday)
    assert view.send_all_button.text() == "Send ready (29)"
    assert view.send_all_button.isEnabled()
    view.set_applications([], workday)
    assert view.send_all_button.text() == "Send ready"
    assert not view.send_all_button.isEnabled()


def test_send_ready_counts_match_star_and_queued_send_eligibility(app):
    view = ApplicationsView()
    view.set_context(priority=["Priority Example"])
    view.set_applications([], [draft(1), draft(2, status="approved"),
                               draft(3, starred=1), draft(4, company="Priority Example"),
                               draft(5, status="needs_answers"), draft(6, "workday"),
                               draft(7, status="approved", starred=1)])
    assert view.send_all_button.text() == "Send ready (3)"
    view.set_sending({1, 7})
    assert view.send_all_button.text() == "Send ready (1)"
    view.set_sending({1, 2, 7})
    assert not view.send_all_button.isEnabled()
    view.set_sending(set())
    assert view.send_all_button.text() == "Send ready (3)"


def test_send_ready_refreshes_when_form_support_changes(app):
    view = ApplicationsView()
    record = draft(1, "workday")
    view.set_applications([], [deepcopy(record)])
    assert not view.send_all_button.isEnabled()
    record["ats"] = "greenhouse"
    view.set_applications([], [record])
    assert view.send_all_button.text() == "Send ready (1)"
    assert view.send_all_button.isEnabled()
