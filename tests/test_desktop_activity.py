"""Dashboard activity should describe user decisions without internal identifiers."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_activity_copy_hides_internal_ids_and_source_noise():
    from jobhub.desktop.ui import _activity_label

    job_id = "a1b2c3d4e5f60718"
    apps = {job_id: {"company": "Example", "title": "Data Engineer"}}
    label = _activity_label(
        f"needs_human: {job_id}: Required or invalid fields need attention: postal code",
        apps,
    )
    assert label == "Needs review · Example · Data Engineer"
    assert job_id not in label
    assert _activity_label("Source greenhouse: oldboard: HTTP 404", apps) is None
    assert _activity_label("Tailored resume built for Example / Data Engineer", apps) is None
    assert _activity_label("Scan started: greenhouse, lever, ashby", apps) == "Searching for new jobs"
    assert _activity_label("Scan ok: 2782 jobs from 6/6 sources, 62 new", apps) == "Job search finished"


def test_dashboard_review_action_opens_applications():
    from PySide6.QtWidgets import QApplication
    from jobhub.desktop.ui import JobHubWindow

    app = QApplication.instance() or QApplication([])
    window = JobHubWindow()
    window.set_data(
        applications=[{
            "job_id": "a1b2c3d4e5f60718",
            "company": "Example",
            "title": "Data Engineer",
            "status": "needs_human",
            "unanswered": ["Current location"],
        }],
        stats={"new": 0, "applied": 0, "needs_human": 1, "total": 1},
        events=[{
            "ts": "2026-09-24T12:00:00+00:00",
            "msg": "needs_human: a1b2c3d4e5f60718: Current location",
        }],
    )
    assert window.review_button.isVisible() is False  # hidden until the shell is shown
    window.show()
    app.processEvents()
    assert window.review_button.isVisible()
    assert window.activity_rows.count() == 1
    window.review_button.click()
    assert window.stack.currentIndex() == 2
    window.close()
