"""Profile → Resume builder and Resume files.

The resume builder holds the candidate's whole library: any number of jobs,
projects, schools, skills, and awards. For each role, Starployment picks the
most relevant pieces and lays them out on one page. Resume files holds
uploaded PDFs by role type, for candidates who prefer to send their own.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..delegates import TwoLineDelegate
from ..editors import BulletEditor, TagEditor
from ..widgets import ElidedLabel, EmptyState, FormGroup, GlassPanel, SegmentedControl, ToggleSwitch, button, label
from ...tailor import RESUME_CATEGORIES, category_label, resume_file_category

CATEGORIES = tuple(item for item in RESUME_CATEGORIES if item[0] != "general")

SPECS = {
    "experience": (("title", "Title", "Software Engineer Intern"), ("company", "Company", "Company name"),
                   ("location", "Location", "City, ST or Remote"), ("start", "Start", "Jun 2025"),
                   ("end", "End", "Aug 2025 or Present")),
    "projects": (("name", "Project", "Project name"), ("role", "Your role", "Lead developer"),
                 ("start", "Start", "Jan 2025"), ("end", "End", "Present"), ("link", "Link", "https://github.com/…")),
    "education": (("school", "School", "University name"), ("degree", "Degree", "B.S. Computer Science"),
                  ("field", "Field", "Optional if in the degree"), ("location", "Location", "City, ST"),
                  ("start", "Start", "Aug 2022"), ("end", "End", "May 2026"), ("gpa", "GPA", "3.8 / 4.0")),
}
EMPTY = {
    "experience": {"company": "", "title": "", "location": "", "start": "", "end": "", "bullets": [], "tech": [],
                   "pinned": False, "hidden": False},
    "projects": {"name": "", "role": "", "start": "", "end": "", "bullets": [], "tech": [], "link": "",
                 "pinned": False, "hidden": False},
    "education": {"school": "", "degree": "", "field": "", "location": "", "start": "", "end": "", "gpa": "",
                  "details": []},
}


def _title(kind: str, entry: dict) -> tuple[str, str]:
    if kind == "experience":
        primary = entry.get("title") or "New role"
        secondary = " · ".join(x for x in (entry.get("company"), _dates(entry)) if x)
    elif kind == "projects":
        primary = entry.get("name") or "New project"
        secondary = " · ".join(x for x in (entry.get("role"), ", ".join((entry.get("tech") or [])[:3])) if x)
    else:
        primary = entry.get("degree") or "New school"
        secondary = " · ".join(x for x in (entry.get("school"), _dates(entry)) if x)
    flags = (" · always" if entry.get("pinned") else "") + (" · hidden" if entry.get("hidden") else "")
    return primary, secondary + flags


def _dates(entry: dict) -> str:
    return " – ".join(x for x in (entry.get("start"), entry.get("end")) if x)


class EntryListEditor(QWidget):
    """A list of resume entries on the left and the selected entry's form on the right."""

    changed = Signal()

    def __init__(self, kind: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.kind = kind
        self._entries: list[dict] = []
        self._loading = False
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(12)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(split)

        left = GlassPanel(radius=16)
        left.setMinimumWidth(240)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(6, 6, 6, 8)
        left_layout.setSpacing(6)
        self.list = QListWidget()
        self.list.setObjectName("plainList")
        self.list.setAccessibleName(kind.title())
        self.list.setItemDelegate(TwoLineDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.currentRowChanged.connect(self._show)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        left_layout.addWidget(self.list, 1)
        add_row = QHBoxLayout()
        add_row.setContentsMargins(6, 0, 6, 0)
        noun = {"experience": "role", "projects": "project", "education": "school"}[kind]
        self.add_button = button(f"Add {noun}", "secondary", icon="plus")
        self.add_button.clicked.connect(self.add_entry)
        add_row.addWidget(self.add_button)
        add_row.addStretch(1)
        left_layout.addLayout(add_row)
        split.addWidget(left)

        right = GlassPanel(radius=16)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.form_stack = QStackedWidget()
        right_layout.addWidget(self.form_stack)
        self.placeholder = EmptyState({"experience": "jobs", "projects": "code", "education": "education"}[kind],
                                      f"No {noun}s yet", f"Add as many {noun}s as you like. Starployment chooses the "
                                      "most relevant ones for each role.")
        self.form_stack.addWidget(self.placeholder)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        form_page = QWidget()
        form_page.setObjectName("page")
        scroll.setWidget(form_page)
        form = QVBoxLayout(form_page)
        form.setContentsMargins(18, 16, 18, 16)
        form.setSpacing(14)
        group = FormGroup()
        self.inputs: dict[str, QLineEdit] = {}
        for key, title, placeholder in SPECS[kind]:
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            edit.setAccessibleName(title)
            edit.textChanged.connect(lambda text, k=key: self._edit(k, text))
            self.inputs[key] = edit
            group.add_row(title, edit)
        self.tech: TagEditor | None = None
        if kind in ("experience", "projects"):
            self.tech = TagEditor("Add a tool")
            self.tech.changed.connect(lambda: self._edit("tech", self.tech.values()))
            group.add_row("Tools", self.tech)
        form.addWidget(group)
        bullets_title = "Highlights" if kind != "education" else "Honors and coursework"
        form.addWidget(label(bullets_title, "groupTitle"))
        self.bullets = BulletEditor("Honors, awards, or relevant courses" if kind == "education"
                                    else "What you did and the result, in your words")
        key = "details" if kind == "education" else "bullets"
        self.bullets.changed.connect(lambda: self._edit(key, self.bullets.values()))
        form.addWidget(self.bullets)
        self.pinned = self.hidden = None
        if kind in ("experience", "projects"):
            flags = FormGroup()
            self.pinned = ToggleSwitch()
            self.pinned.toggled.connect(lambda on: self._edit("pinned", bool(on)))
            self.hidden = ToggleSwitch()
            self.hidden.toggled.connect(lambda on: self._edit("hidden", bool(on)))
            flags.add_row("Always include", self.pinned, "Keep this on every generated resume.")
            flags.add_row("Keep out of resumes", self.hidden, "Save it here without printing it.")
            form.addWidget(flags)
        actions = QHBoxLayout()
        self.up = button("Move up", "quiet", icon="chevron-up")
        self.up.clicked.connect(lambda: self._move(-1))
        self.delete = button("Delete", "quiet", icon="trash")
        self.delete.clicked.connect(self.remove_current)
        actions.addWidget(self.up)
        actions.addStretch(1)
        actions.addWidget(self.delete)
        form.addLayout(actions)
        form.addStretch(1)
        self.form_stack.addWidget(scroll)
        self.form_scroll = scroll
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        split.setSizes([280, 520])

    # ------------------------------------------------------------------ data
    def set_entries(self, entries: list[dict]) -> None:
        keep = self.list.currentRow()   # a refresh after Save keeps the entry you were editing
        self._entries = [dict(EMPTY[self.kind], **dict(entry)) for entry in entries]
        self._refresh_list(min(max(keep, 0), len(self._entries) - 1) if self._entries else -1)

    def entries(self) -> list[dict]:
        return [dict(entry) for entry in self._entries]

    def _refresh_list(self, select: int) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for entry in self._entries:
            primary, secondary = _title(self.kind, entry)
            item = QListWidgetItem(primary + (f"\n{secondary}" if secondary else ""))
            item.setToolTip(primary)
            self.list.addItem(item)
        self.list.blockSignals(False)
        if 0 <= select < len(self._entries):
            self.list.setCurrentRow(select)
            self._show(select)
        else:
            self.form_stack.setCurrentWidget(self.placeholder)

    def _show(self, row: int) -> None:
        if not 0 <= row < len(self._entries):
            self.form_stack.setCurrentWidget(self.placeholder)
            return
        entry = self._entries[row]
        self._loading = True
        try:
            for key, edit in self.inputs.items():
                edit.setText(str(entry.get(key) or ""))
            if self.tech is not None:
                self.tech.set_values(list(entry.get("tech") or []))
            self.bullets.set_values(list(entry.get("details" if self.kind == "education" else "bullets") or []))
            if self.pinned is not None:
                self.pinned.setChecked(bool(entry.get("pinned")))
                self.hidden.setChecked(bool(entry.get("hidden")))
        finally:
            self._loading = False
        self.up.setEnabled(row > 0)
        self.form_stack.setCurrentWidget(self.form_scroll)

    def _edit(self, key: str, value) -> None:
        if self._loading:
            return
        row = self.list.currentRow()
        if not 0 <= row < len(self._entries):
            return
        self._entries[row][key] = value
        item = self.list.item(row)
        primary, secondary = _title(self.kind, self._entries[row])
        item.setText(primary + (f"\n{secondary}" if secondary else ""))
        self.changed.emit()

    def add_entry(self) -> None:
        self._entries.insert(0, dict(EMPTY[self.kind]))
        self._refresh_list(0)
        first = next(iter(self.inputs.values()))
        first.setFocus()
        self.changed.emit()

    def remove_current(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self._entries):
            del self._entries[row]
            self._refresh_list(min(row, len(self._entries) - 1))
            self.changed.emit()

    def _move(self, step: int) -> None:
        row = self.list.currentRow()
        target = row + step
        if 0 <= row < len(self._entries) and 0 <= target < len(self._entries):
            self._entries[row], self._entries[target] = self._entries[target], self._entries[row]
            self._refresh_list(target)
            self.changed.emit()

    def _menu(self, position) -> None:
        row = self.list.indexAt(position).row()
        if row < 0:
            return
        self.list.setCurrentRow(row)
        menu = QMenu(self)
        menu.addAction("Duplicate", lambda: self._duplicate(row))
        menu.addAction("Move up", lambda: self._move(-1))
        menu.addAction("Move down", lambda: self._move(1))
        menu.addSeparator()
        menu.addAction("Delete", self.remove_current)
        menu.exec(self.list.viewport().mapToGlobal(position))

    def _duplicate(self, row: int) -> None:
        self._entries.insert(row + 1, {k: (list(v) if isinstance(v, list) else v) for k, v in self._entries[row].items()})
        self._refresh_list(row + 1)
        self.changed.emit()


class ResumeBuilder(QWidget):
    """Every job, project, school, skill, and award, in one editable library."""

    changed = Signal()
    import_requested = Signal(str)
    preview_requested = Signal(str)

    SECTIONS = (("Education", "education"), ("Experience", "experience"), ("Projects", "projects"),
                ("Skills", "skills"), ("Awards", "awards"))

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._preview_jobs: list[tuple[str, str]] = []
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(12)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.section = SegmentedControl(compact=True)
        for title, key in self.SECTIONS:
            self.section.addItem(title, key)
        toolbar.addWidget(self.section)
        toolbar.addStretch(1)
        self.import_button = button("Import from PDF…", "secondary", icon="file-up")
        self.import_button.setToolTip("Add the jobs, projects, and skills from another resume")
        self.import_button.clicked.connect(self._choose_import)
        self.preview_button = button("Preview for a role", "secondary", icon="eye")
        self.preview_button.setToolTip("Build the one-page resume Starployment would send for one of your matches")
        self.preview_button.clicked.connect(self._preview_menu)
        toolbar.addWidget(self.import_button)
        toolbar.addWidget(self.preview_button)
        column.addLayout(toolbar)
        self.summary = label("", "caption")
        column.addWidget(self.summary)

        self.pages = QStackedWidget()
        self.experience = EntryListEditor("experience")
        self.projects = EntryListEditor("projects")
        self.education = EntryListEditor("education")
        skills_page = QWidget()
        skills_page.setObjectName("page")
        skills_layout = QVBoxLayout(skills_page)
        skills_layout.setContentsMargins(0, 0, 0, 0)
        skills_group = FormGroup("Skills", "Add every skill you have. The most relevant ones are listed first "
                                           "for each role, up to the limit in Settings.")
        self.skills = TagEditor("Add a skill")
        skills_group.add_widget(self.skills)
        skills_layout.addWidget(skills_group)
        skills_layout.addStretch(1)
        awards_page = QWidget()
        awards_page.setObjectName("page")
        awards_layout = QVBoxLayout(awards_page)
        awards_layout.setContentsMargins(0, 0, 0, 0)
        awards_group = FormGroup("Awards, certifications, and leadership",
                                 "One line each. The most relevant few appear on each resume.")
        self.awards = BulletEditor("Dean's List, certification, club officer role…")
        awards_group.add_widget(self.awards)
        awards_layout.addWidget(awards_group)
        awards_layout.addStretch(1)
        for page in (self.education, self.experience, self.projects):
            self.pages.addWidget(page)
        for page in (skills_page, awards_page):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setWidget(page)
            self.pages.addWidget(scroll)
        column.addWidget(self.pages, 1)
        self.section.currentIndexChanged.connect(self.pages.setCurrentIndex)
        for editor in (self.experience, self.projects, self.education, self.skills, self.awards):
            editor.changed.connect(self._changed)

    def _changed(self) -> None:
        self._update_summary()
        self.changed.emit()

    def _update_summary(self) -> None:
        exp, proj = len(self.experience.entries()), len(self.projects.entries())
        skills = len(self.skills.values())
        self.summary.setText(f"{exp} role{'s' if exp != 1 else ''} · {proj} project{'s' if proj != 1 else ''} · "
                             f"{skills} skill{'s' if skills != 1 else ''}. Starployment picks what fits each role "
                             "and keeps the resume to one page. Your wording is never changed.")

    def set_profile(self, data: dict) -> None:
        self.experience.set_entries(list(data.get("experience") or []))
        self.projects.set_entries(list(data.get("projects") or []))
        self.education.set_entries(list(data.get("education") or []))
        self.skills.set_values(list(data.get("skills") or []))
        self.awards.set_values(list(data.get("awards") or []))
        self._update_summary()

    def library(self) -> dict:
        return {"experience": self.experience.entries(), "projects": self.projects.entries(),
                "education": self.education.entries(), "skills": self.skills.values(), "awards": self.awards.values()}

    def set_preview_jobs(self, jobs: list[tuple[str, str]]) -> None:
        self._preview_jobs = list(jobs)
        self.preview_button.setEnabled(bool(jobs))

    def _preview_menu(self) -> None:
        menu = QMenu(self)
        for job_id, text in self._preview_jobs[:12]:
            menu.addAction(text, lambda jid=job_id: self.preview_requested.emit(jid))
        if menu.isEmpty():
            menu.addAction("Run a scan to find matches first").setEnabled(False)
        menu.exec(self.preview_button.mapToGlobal(self.preview_button.rect().bottomLeft()))

    def _choose_import(self) -> None:
        from pathlib import Path
        path, _ = QFileDialog.getOpenFileName(self, "Import resume", str(Path.home()), "PDF files (*.pdf)")
        if path:
            self.import_requested.emit(path)


class ResumeFiles(QWidget):
    """Uploaded resume PDFs by role type, and which resume Starployment sends."""

    strategy_changed = Signal(str)
    add_requested = Signal(str, str)
    category_changed = Signal(int, str)
    default_requested = Signal(int)
    remove_requested = Signal(int)
    open_requested = Signal(str)
    open_folder_requested = Signal()
    enabled_changed = Signal(int, bool)
    set_main_requested = Signal(int)
    rename_requested = Signal(int, str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._signature = None
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(16)
        choice = FormGroup("Resume bank", "Automatic role detection uses each resume’s name and PDF text. You can also choose a role type yourself.")
        self.strategy = SegmentedControl()
        self.strategy.addItem("Tailor from profile", "tailored")
        self.strategy.addItem("Resume bank", "library")
        self.strategy.addItem("Main resume", "original")
        self.strategy.activated.connect(lambda _i: self.strategy_changed.emit(str(self.strategy.currentData())))
        self.strategy_note = label("", "rowNote")
        choice.add_row("Send", self.strategy)
        choice.add_widget(self.strategy_note)
        self.strategy.currentIndexChanged.connect(self._describe)
        column.addWidget(choice)

        header = QHBoxLayout()
        header.addWidget(label("Resume bank", "groupTitle"))
        header.addStretch(1)
        folder = button("Open folder", "quiet", icon="folder")
        folder.clicked.connect(self.open_folder_requested)
        add = button("Add PDF…", "secondary", icon="plus")
        add.clicked.connect(self._choose)
        header.addWidget(folder)
        header.addWidget(add)
        column.addLayout(header)
        self.panel = GlassPanel(radius=14)
        self.rows = QVBoxLayout(self.panel)
        self.rows.setContentsMargins(16, 8, 12, 8)
        self.rows.setSpacing(0)
        column.addWidget(self.panel)
        self.empty = label("No resumes in your Resume bank yet. Add a PDF for each kind of role you apply to, for example one for "
                           "software engineering and one for data science. Starployment picks the matching file "
                           "for each job when “Resume bank” is selected.", "rowNote")
        column.addWidget(self.empty)
        column.addStretch(1)
        self._describe()

    def _describe(self, *_args) -> None:
        self.strategy_note.setText({
            "tailored": "A one-page resume is generated for each role from your Resume builder.",
            "library": "Matches resumes to jobs locally, with no AI calls or tokens. Your PDFs are sent unchanged.",
            "original": "Every application uses your main resume from setup.",
        }.get(str(self.strategy.currentData()), ""))

    def set_strategy(self, strategy: str) -> None:
        if strategy == "bank":
            strategy = "library"
        index = self.strategy.findData(strategy)
        if index >= 0:
            self.strategy.setCurrentIndex(index)

    def set_files(self, files: list[dict]) -> None:
        signature = tuple((f.get("id"), f.get("category"), f.get("is_default"), f.get("label"), f.get("enabled", 1), f.get("is_main", False)) for f in files)
        if signature == self._signature:
            return
        self._signature = signature
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for index, record in enumerate(files):
            row = QWidget()
            row.setObjectName("formRow")
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 8, 0, 8)
            line.setSpacing(10)
            text = QVBoxLayout()
            text.setSpacing(1)
            text.addWidget(ElidedLabel(str(record.get("label") or "Resume"), "rowPrimary"))
            if record.get("is_main"):
                subtitle = "Main resume" + (" · Default fallback" if record.get("is_default") else "")
            else:
                subtitle = "Default fallback" if record.get("is_default") else "Used for matching roles"
            text.addWidget(ElidedLabel(subtitle, "rowSecondary"))
            line.addLayout(text, 1)
            category = QComboBox()
            detected = resume_file_category({**record, "category": "auto"})
            for key, title in CATEGORIES:
                if key == "auto" and detected != "general":
                    title = f"Automatic · {category_label(detected)}"
                category.addItem(title, key)
            selected = record.get("category") or "auto"
            category.setCurrentIndex(max(0, category.findData("auto" if selected == "general" else selected)))
            category.setToolTip("Automatic detects the role from the resume name and PDF text. Choose a role to override it.")
            category.activated.connect(lambda _i, c=category, fid=record["id"]: self.category_changed.emit(int(fid), str(c.currentData())))
            category.setAccessibleName("Role type")
            line.addWidget(category)
            consider_toggle = ToggleSwitch()
            consider_toggle.setChecked(bool(record.get("enabled", 1)))
            consider_toggle.setToolTip("Consider this resume during job matching in Resume Bank")
            consider_toggle.toggled.connect(lambda checked, fid=record["id"]: self.enabled_changed.emit(int(fid), bool(checked)))
            line.addWidget(consider_toggle)
            open_button = button("", "quiet", icon="eye")
            open_button.setToolTip("Open")
            open_button.clicked.connect(lambda _=False, p=record["path"]: self.open_requested.emit(str(p)))
            line.addWidget(open_button)
            remove_button = button("", "quiet", icon="trash")
            remove_button.setToolTip("Remove from resume bank")
            remove_button.clicked.connect(lambda _=False, fid=record["id"]: self.remove_requested.emit(int(fid)))
            line.addWidget(remove_button)
            more = button("", "quiet", icon="chevron-down")
            more.setToolTip("More")
            menu = QMenu(more)
            menu.addAction("Rename…", lambda fid=record["id"], name=record.get("label") or "Resume": self._rename(int(fid), str(name)))
            menu.addSeparator()
            if not record.get("is_main"):
                menu.addAction("Set as main resume", lambda fid=record["id"]: self.set_main_requested.emit(int(fid)))
            if not record.get("is_default"):
                menu.addAction("Make default fallback", lambda fid=record["id"]: self.default_requested.emit(int(fid)))
            menu.addAction("Remove from bank", lambda fid=record["id"]: self.remove_requested.emit(int(fid)))
            more.setMenu(menu)
            line.addWidget(more)
            self.rows.addWidget(row)
            if index < len(files) - 1:
                rule = QFrame()
                rule.setFrameShape(QFrame.Shape.HLine)
                self.rows.addWidget(rule)
        self.panel.setVisible(bool(files))
        self.empty.setVisible(not files)

    def _rename(self, file_id: int, current_name: str) -> None:
        name, accepted = QInputDialog.getText(self, "Rename resume", "Resume name", text=current_name)
        if accepted:
            self.rename_requested.emit(file_id, name)

    def _choose(self) -> None:
        from pathlib import Path
        paths, _ = QFileDialog.getOpenFileNames(self, "Add resume PDFs", str(Path.home()), "PDF files (*.pdf)")
        for path in paths:
            self.add_requested.emit(path, "auto")


def guess_category(name: str) -> str:
    """Guess a role type from a file name such as ``Ada_SWE_resume`` or ``data-science-cv``."""
    return resume_file_category({"label": name})
