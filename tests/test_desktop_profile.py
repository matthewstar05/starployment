"""Profile page: details, resume builder, and answers save through the controller without syntax."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("JOBHUB_REDUCED_MOTION", "1")

from jobhub import db, scheduler  # noqa: E402
from jobhub.models import Experience, Profile, Project  # noqa: E402


def test_profile_page_saves_library_answers_and_facts(store, make_pdf, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from jobhub.desktop.controller import DesktopController
    from jobhub.desktop.ui import JobHubWindow

    resume = make_pdf(store / "resumes" / "r.pdf", ["Ada Example"])
    db.save_profile(Profile(first_name="Ada", last_name="Example", email="ada@example.test", city="Springfield",
                            country="United States", resume_path=str(resume), skills=["Python"],
                            experience=[Experience(company="Lab", title="Assistant", bullets=["Built tools"])],
                            projects=[Project(name="Parser", bullets=["Wrote a parser"])],
                            custom_facts={"Home city": "Springfield, IL"}))
    db.set_settings({"target_titles": ["Engineer"], "setup_complete": True})
    monkeypatch.setattr(scheduler, "rescore", lambda: None)
    app = QApplication.instance() or QApplication([])  # noqa: F841
    window = JobHubWindow()
    controller = DesktopController(window, scheduler_enabled=False, show_setup_on_start=False)
    try:
        view = window.profile
        assert view.custom_facts.values() == {"Home city": "Springfield, IL"}   # no "label | value" text
        builder = view.builder
        assert [e["company"] for e in builder.experience.entries()] == ["Lab"]
        builder.experience.add_entry()
        builder.experience.inputs["company"].setText("Northwind")
        builder.experience.inputs["title"].setText("Intern")
        builder.experience.bullets._add("Shipped a feature")
        builder.experience.bullets.fields[-1].setPlainText("Shipped a feature used by 10k people")
        builder.skills.input.setText("SQL, Go")
        builder.skills._commit()
        view.yes_no["work_authorized_us"].setCurrentIndex(view.yes_no["work_authorized_us"].findData("Yes"))
        view.question_bank.add_row("Why do you want to work here?", "I like the mission.")
        view.custom_facts.add_row("Languages spoken", "English, Spanish")
        assert view.save_button.isEnabled()
        view.save_button.click()
        profile = db.get_profile()
        assert [e.company for e in profile.experience] == ["Northwind", "Lab"]
        assert profile.experience[0].bullets == ["Shipped a feature used by 10k people"]
        assert profile.skills == ["Python", "SQL", "Go"]
        assert profile.custom_facts == {"Home city": "Springfield, IL", "Languages spoken": "English, Spanish"}
        answers = db.get_settings()["answers"]
        assert answers["work_authorized_us"] == "Yes" and "work_authorized_us" in answers["confirmed_fields"]
        assert answers["field_answers"]["Why do you want to work here?"] == "I like the mission."
        # The refresh after saving keeps the entry being edited selected.
        builder.experience.list.setCurrentRow(1)
        controller.refresh(include_account=True)
        assert builder.experience.list.currentRow() == 1
    finally:
        controller.stop()
        window.close()
        window.deleteLater()


def test_resume_files_are_copied_into_the_library_and_matched_by_role(store, make_pdf, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from jobhub.desktop.controller import DesktopController
    from jobhub.desktop.ui import JobHubWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841
    window = JobHubWindow()
    controller = DesktopController(window, scheduler_enabled=False, show_setup_on_start=False)
    try:
        source = make_pdf(store / "Ada_DS_resume.pdf", ["Data resume"])
        window.profile.files.add_requested.emit(str(source), "data")
        files = db.list_resume_files()
        assert len(files) == 1 and files[0]["category"] == "data" and files[0]["is_default"] == 1
        assert files[0]["path"].startswith(str(store / "resumes" / "library"))
        window.profile.files.strategy_changed.emit("library")
        assert db.get_settings()["resume_strategy"] == "library"
        window.profile.files.enabled_changed.emit(int(files[0]["id"]), False)
        assert db.list_resume_files()[0]["enabled"] == 0
        window.profile.files.remove_requested.emit(int(files[0]["id"]))
        assert db.list_resume_files() == []
    finally:
        controller.stop()
        window.close()
        window.deleteLater()


def test_resume_files_set_main_and_remove_protection(store, make_pdf):
    from pathlib import Path
    from PySide6.QtWidgets import QApplication
    from jobhub.desktop.controller import DesktopController
    from jobhub.desktop.ui import JobHubWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841
    window = JobHubWindow()
    controller = DesktopController(window, scheduler_enabled=False, show_setup_on_start=False)
    try:
        source = make_pdf(store / "Ada_SWE_resume.pdf", ["SWE resume"])
        window.profile.files.add_requested.emit(str(source), "software")
        files = db.list_resume_files()
        assert len(files) == 1
        window.profile.files.set_main_requested.emit(int(files[0]["id"]))
        assert db.get_profile().resume_path == files[0]["path"]
        assert db.list_resume_files()[0]["is_main"] is True
        saved_path = files[0]["path"]
        window.profile.files.remove_requested.emit(int(files[0]["id"]))
        assert Path(saved_path).is_file()  # Protected because it was the main resume!
    finally:
        controller.stop()
        window.close()
        window.deleteLater()


def test_resume_bank_rename_persists_and_updates_automatic_role_without_changing_pdf(store, make_pdf, monkeypatch):
    from pathlib import Path
    from PySide6.QtWidgets import QApplication, QComboBox, QPushButton
    from jobhub.desktop.controller import DesktopController
    from jobhub.desktop.ui import JobHubWindow
    from jobhub.desktop.views import resume_builder
    from jobhub.tailor import resume_file_category

    app = QApplication.instance() or QApplication([])  # noqa: F841
    window = JobHubWindow()
    controller = DesktopController(window, scheduler_enabled=False, show_setup_on_start=False)
    try:
        source = make_pdf(store / "Ada_SWE_resume.pdf", ["Ada Example", "Software Engineer", "Python SQL"])
        monkeypatch.setattr(resume_builder.QFileDialog, "getOpenFileNames", lambda *a: ([str(source)], ""))
        files_view = window.profile.files
        files_view._choose()
        record = db.list_resume_files()[0]
        assert record["category"] == "auto" and resume_file_category(record) == "software"
        files_view.set_main_requested.emit(record["id"])
        bank_path = record["path"]
        original = Path(bank_path).read_bytes()

        def row_widgets():
            row = files_view.rows.itemAt(0).widget()
            more = next(w for w in row.findChildren(QPushButton) if w.toolTip() == "More")
            role = next(w for w in row.findChildren(QComboBox) if w.accessibleName() == "Role type")
            return more, role

        more, role = row_widgets()
        assert role.currentData() == "auto" and "Software engineering" in role.currentText()
        answers = iter((("  Financial Analyst  ", True), ("Cancelled", False), ("   ", True)))
        monkeypatch.setattr(resume_builder.QInputDialog, "getText", lambda *a, **kw: next(answers))
        for _ in range(3):
            more, _role = row_widgets()
            next(action for action in more.menu().actions() if action.text() == "Rename…").trigger()
            renamed = db.list_resume_files()[0]
            assert renamed["label"] == "Financial Analyst"
            assert renamed["id"] == record["id"] and renamed["path"] == bank_path
            assert renamed["is_main"] and renamed["is_default"] and renamed["enabled"]
            assert resume_file_category(renamed) == "finance"
        _, role = row_widgets()
        assert "Financial analysis" in role.currentText()
        role.setCurrentIndex(role.findData("data"))
        role.activated.emit(role.currentIndex())
        assert resume_file_category(db.list_resume_files()[0]) == "data"
        assert db.get_profile().resume_path == bank_path
        assert Path(bank_path).read_bytes() == original == source.read_bytes()
    finally:
        controller.stop()
        window.close()
        window.deleteLater()
