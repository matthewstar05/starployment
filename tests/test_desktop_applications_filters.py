"""Applications filters: Ready vs Incomplete, counts on every filter, Starred only, and compact rows."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def draft(i: int, status: str, **extra) -> dict:
    return {"id": i, "job_id": f"job{i}", "status": status, "company": f"Company {i}", "title": "Engineer",
            "questions": [], "updated_at": "2026-10-20T10:00:00+00:00", **extra}


def view_with_records():
    from PySide6.QtWidgets import QApplication
    from jobhub.desktop.ui import ApplicationsView

    app = QApplication.instance() or QApplication([])  # noqa: F841
    view = ApplicationsView()
    drafts = [draft(1, "ready"), draft(2, "practice"), draft(3, "needs_answers"),
              draft(4, "error", error="The form could not be read\nTraceback"), draft(5, "approved"),
              draft(6, "ready", starred=1)]
    attempts = [{"id": 9, "job_id": "job9", "status": "applied", "company": "Sent Co", "title": "Engineer",
                 "submission_attempted": 1, "finished_at": "2026-10-19T10:00:00+00:00"}]
    view.set_applications(attempts, drafts)
    return view


def shown(view) -> set[str]:
    return {view.list.item(i).data(0x0100)["company"] for i in range(view.list.count())}


def test_ready_and_incomplete_are_separate_filters_with_counts():
    view = view_with_records()
    texts = [view.filter.itemText(i) for i in range(view.filter.count())]
    assert texts == ["All 7", "Ready 3", "Incomplete 2", "Approved 1", "Sent 1", "Issues"]
    view.show_filter("incomplete")
    assert shown(view) == {"Company 3", "Company 4"}
    error_row = next(view.list.item(i) for i in range(view.list.count())
                     if view.list.item(i).data(0x0100)["company"] == "Company 4")
    from jobhub.desktop.delegates import SUBTITLE_ROLE
    assert error_row.data(SUBTITLE_ROLE) == "The form could not be read"   # an incomplete row says why
    view.show_filter("review")                                              # older callers land on Ready
    assert view.filter.currentData() == "ready" and shown(view) == {"Company 1", "Company 2", "Company 6"}


def test_starred_only_combines_with_filters_and_updates_counts():
    view = view_with_records()
    view.show_filter("starred")
    assert view.starred_only.isChecked() and view.filter.currentData() == "all"
    assert shown(view) == {"Company 6"}
    assert view.filter.itemText(1) == "Ready 1" and view.filter.itemText(2) == "Incomplete"


def test_compact_view_halves_row_height():
    view = view_with_records()
    tall = view.list.item(0).sizeHint().height()
    view.view.choose("compact")
    assert view.delegate.compact and view.list.item(0).sizeHint().height() < tall * 0.6
    view.view.choose("comfortable")
    assert view.list.item(0).sizeHint().height() == tall
