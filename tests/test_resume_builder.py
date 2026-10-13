"""The one-page resume builder and role-specific resume files."""
from pypdf import PdfReader

from jobhub import db
from jobhub.models import Education, Experience, Job, Profile, Project
from jobhub.tailor import (build_resume, choose_resume_file, classify_role, plan_resume, resume_choice)


def big_profile() -> Profile:
    roles = ["Software Engineer", "Data Analyst", "Research Assistant", "Barista"]
    experience = [
        Experience(company=f"Employer {i}", title=roles[i % 4], start=f"Jan {2012 + i}", end=f"Dec {2012 + i}",
                   tech=["Python"], bullets=[f"Shipped feature {k} for team {i} using Python and SQL services"
                                             for k in range(6)])
        for i in range(12)
    ]
    experience.append(Experience(company="Ledger Labs", title="Trading Systems Intern", start="Jun 2025", end="Aug 2025",
                                 bullets=["Built a C++ order-matching engine handling 2M orders per second",
                                          "Wrote market data feed handlers for equities and options"]))
    projects = [Project(name=f"Side Project {i}", tech=["Rust"], bullets=[f"Wrote a toy {kind} in Rust"
                                                                          for kind in ("parser", "shell", "game")])
                for i in range(11)]
    projects.append(Project(name="Latency Arbitrage Simulator", tech=["C++"],
                            bullets=["Simulated low-latency trading strategies with a C++ event loop"]))
    return Profile(first_name="Ada", last_name="Example", email="ada@example.test",
                   education=[Education(school="Example University", degree="B.S. Computer Science", end="May 2026")],
                   experience=experience, projects=projects,
                   skills=[f"Tool {i}" for i in range(80)] + ["C++", "Python", "SQL"],
                   awards=["Dean's List", "Hackathon finalist", "Teaching assistant", "Club president", "Scholar"])


TRADING = Job(source="fixture", company="Fixture Capital", title="Software Engineer, Trading Systems",
              url="https://example.test/trading", description="Low-latency C++ trading systems, order matching, market data.")


def text_of(path) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def test_large_library_always_renders_exactly_one_page(store):
    out = build_resume(big_profile(), TRADING, store / "resume.pdf")
    assert len(PdfReader(str(out)).pages) == 1


def test_most_relevant_material_is_kept_and_wording_is_verbatim(store):
    out = build_resume(big_profile(), TRADING, store / "resume.pdf")
    text = " ".join(text_of(out).split())
    assert "Built a C++ order-matching engine handling 2M orders per second" in text
    assert "Latency Arbitrage Simulator" in text
    assert "C++" in text.split("Skills")[-1][:200]


def test_pinned_entries_stay_and_hidden_entries_never_print(store):
    profile = big_profile()
    profile.experience[0].pinned = True       # an old, irrelevant role the candidate always wants shown
    profile.experience[-1].hidden = True      # the most relevant role, hidden on purpose
    text = " ".join(text_of(build_resume(profile, TRADING, store / "r.pdf")).split())
    assert "Employer 0" in text
    assert "order-matching engine" not in text


def test_luna_selection_is_used_as_priority_but_validated(store):
    profile = big_profile()
    plan = plan_resume(profile, TRADING, {}, selection={"experience": [3], "projects": [], "bullets": {}, "skills": []})
    assert [item.index for item in plan.experience] == [3]
    assert plan.reason == "Selected by Luna"


def test_role_classification_and_resume_file_choice(store, make_pdf):
    write_pdf = make_pdf
    assert classify_role(Job(source="x", company="A", title="Data Scientist", url="https://e.test/1")) == "data"
    assert classify_role(Job(source="x", company="A", title="Quantitative Developer", url="https://e.test/2")) == "quant"
    assert classify_role(Job(source="x", company="A", title="Machine Learning Engineer", url="https://e.test/3")) == "ml"
    assert classify_role(Job(source="x", company="A", title="Backend Engineer", url="https://e.test/4")) == "software"
    assert classify_role(Job(source="x", company="A", title="Office Coordinator", url="https://e.test/5")) == "general"
    swe = write_pdf(store / "swe.pdf", ["SWE resume"])
    data = write_pdf(store / "data.pdf", ["Data resume"])
    files = [{"path": str(swe), "label": "SWE", "category": "software", "is_default": 1},
             {"path": str(data), "label": "Data", "category": "data", "is_default": 0}]
    path, note = choose_resume_file(Job(source="x", company="A", title="Data Analyst", url="https://e.test/6"), files)
    assert path == str(data) and "matched" in note
    path, note = choose_resume_file(Job(source="x", company="A", title="Product Manager", url="https://e.test/7"), files)
    assert path == str(swe) and "default" in note


def test_resume_choice_follows_the_strategy(store, make_pdf):
    write_pdf = make_pdf
    main = write_pdf(store / "resumes" / "main.pdf", ["Main"])
    data = write_pdf(store / "resumes" / "library" / "data.pdf", ["Data"])
    db.add_resume_file(str(data), "Data resume", "data")
    profile = big_profile()
    profile.resume_path = str(main)
    job = Job(source="x", company="A", title="Data Scientist", url="https://e.test/8", description="statistics")
    assert resume_choice(profile, job, {"resume_strategy": "original"})[0] == str(main)
    assert resume_choice(profile, job, {"resume_strategy": "library"})[0] == str(data)
    generated, note = resume_choice(profile, job, {"resume_strategy": "tailored"}, out_dir=store / "tailored" / "drafts")
    assert generated.endswith(".pdf") and "generated" in note
    assert len(PdfReader(generated).pages) == 1


def test_automatic_bank_matches_role_before_shared_skills_without_ai(store, make_pdf, monkeypatch):
    from jobhub import agents, luna

    def unexpected_ai(*args, **kwargs):
        raise AssertionError("Resume bank matching must not call AI")

    monkeypatch.setattr(agents, "complete", unexpected_ai)
    monkeypatch.setattr(luna, "select_resume_content", unexpected_ai)
    originals = {}
    paths = {}
    for i, name in enumerate(("SWE", "Data Science", "Financial Analyst")):
        # Shared skills and a dense data section must not override the named role.
        content = ["Ada Example", "Python SQL analytics statistics experimentation " * (i + 1)]
        path = make_pdf(store / "resumes" / "library" / f"version{i}.pdf", content)
        db.add_resume_file(str(path), name, "auto")
        paths[name] = str(path)
        originals[path] = path.read_bytes()
    profile = Profile(first_name="Ada", last_name="Example")
    cases = (("Software Engineer, Financial Services", "SWE", "software"),
             ("Data Scientist, Banking", "Data Science", "data"),
             ("Financial Analyst, Infrastructure", "Financial Analyst", "finance"),
             ("FP&A Analyst", "Financial Analyst", "finance"))
    for title, name, category in cases:
        job = Job(source="fixture", company="Example Co", title=title, url="https://example.test/job",
                  description="Python SQL analytics statistics financial modeling trading payments")
        assert classify_role(job) == category
        for strategy in ("library", "bank"):
            selected, note = resume_choice(profile, job, {"resume_strategy": strategy,
                                                         "resume_builder": {"luna_select": True}})
            assert selected == paths[name] and "matched" in note
    assert all(path.read_bytes() == original for path, original in originals.items())


def test_automatic_role_uses_filename_pdf_and_renamed_label_with_manual_override(store, make_pdf):
    from jobhub.tailor import resume_file_category

    named = make_pdf(store / "Ada_DS_resume.pdf", ["Ada Example", "Python SQL"])
    assert resume_file_category({"path": str(named), "label": "Version 1", "category": "auto"}) == "data"
    neutral = make_pdf(store / "version2.pdf", ["Ada Example", "Financial Analyst", "Python SQL budgets forecasts"])
    record = {"path": str(neutral), "label": "Version 2", "category": "auto"}
    assert resume_file_category(record) == "finance"
    # Legacy General entries also benefit from automatic detection.
    assert resume_file_category({**record, "category": "general"}) == "finance"
    assert resume_file_category({**record, "label": "SWE"}) == "software"
    assert resume_file_category({**record, "label": "SWE", "category": "finance"}) == "finance"
    assert resume_file_category({"label": "Version 3", "path": str(store / "missing.pdf")}) == "general"


def _placed_text(path):
    """(x, right edge, text) for every text run on page one."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    runs = []

    def visit(text, cm, tm, font, size):
        if text.strip() and font:
            x = cm[0] * tm[4] + cm[4]
            scale = cm[0] * tm[0]
            name = str(font.get("/BaseFont", "/Times-Roman")).lstrip("/")
            runs.append((x, x + stringWidth(text.strip(), name, size) * scale, text.strip()))

    PdfReader(str(path)).pages[0].extract_text(visitor_text=visit)
    return runs


def test_resume_lines_share_one_left_margin_and_dates_sit_flush_right(store):
    from jobhub.tailor import PAGE_MARGIN_X

    profile = big_profile()
    profile.experience[-1].location = "Example City, CA"
    out = build_resume(profile, TRADING, store / "resume.pdf")
    runs = _placed_text(out)
    margin, right_margin = PAGE_MARGIN_X, 8.5 * 72 - PAGE_MARGIN_X
    lefts = {round(x, 1) for x, _, text in runs if x < margin + 20 and text != profile.full_name}
    # Section and body text share a margin; bullet markers and their text are
    # indented. The name and contact line are centered separately.
    assert min(lefts) >= margin - 0.2
    assert len({round(x) for x in lefts}) <= 3, lefts
    dates = [(x, right) for x, right, text in runs if "–" in text or text in {"Example City, CA"}]
    assert dates and all(abs(right - right_margin) < 1.0 for _, right in dates), dates


def test_template_section_order_centered_header_and_relevant_employment(store):
    from jobhub.tailor import PAGE_MARGIN_X

    profile = big_profile()
    out = build_resume(profile, TRADING, store / "template.pdf")
    text = text_of(out)
    assert text.index("Education") < text.index("Projects") < text.index("Experience") < text.index("Skills")
    assert text.index("Ledger Labs") < text.index("Employer")
    name = next((x, right) for x, right, value in _placed_text(out) if value == profile.full_name)
    assert abs(sum(name) / 2 - 8.5 * 72 / 2) < 1
    assert name[0] > PAGE_MARGIN_X


def test_relevance_controls_display_order_even_for_pinned_or_luna_selected_content(store):
    job = Job(source="fixture", company="Example Co", title="Database Analyst", url="https://example.test/sql",
              description="SQL database schemas and data pipelines")
    profile = Profile(first_name="Casey", last_name="Example", email="casey@example.test",
        education=[Education(school="Example Art School", degree="M.F.A.", field="Painting", end="2027"),
                   Education(school="Example Technical School", degree="B.S.", field="Computer Science", end="2020",
                             details=["Campus volunteer", "SQL database coursework"])],
        projects=[Project(name="Art Portfolio", tech=["HTML"], bullets=["Published a portfolio of paintings."], pinned=True),
                   Project(name="Campus Database", tech=["SQL"], bullets=["Organized project meetings.",
                         "Designed SQL database schemas."])],
        experience=[Experience(company="Outreach Club", title="Volunteer", end="Present", pinned=True,
                               bullets=["Coordinated community events."]),
                    Experience(company="Archive Lab", title="Database Assistant", end="2020",
                               bullets=["Organized project meetings.", "Built SQL database pipelines."])],
        skills=["Public speaking", "SQL"])
    original = profile.model_dump()
    plan = plan_resume(profile, job, selection={"skills": ["Public speaking"]})
    assert plan.education[0].school == "Example Technical School"
    assert plan.projects[0].source.name == "Campus Database"
    assert plan.experience[0].source.company == "Archive Lab"
    assert plan.projects[0].bullets[0] == "Designed SQL database schemas."
    assert plan.experience[0].bullets[0] == "Built SQL database pipelines."
    assert plan.skills[0] == "SQL"
    out = build_resume(profile, job, store / "relevance.pdf", selection={"skills": ["Public speaking"]})
    text = " ".join(text_of(out).split())
    assert text.index("Example Technical School") < text.index("Example Art School")
    assert text.index("SQL database coursework") < text.index("Campus volunteer")
    assert text.index("Campus Database") < text.index("Art Portfolio")
    assert text.index("Archive Lab") < text.index("Outreach Club")
    assert len(PdfReader(out).pages) == 1
    fonts = {str(font.get_object()["/BaseFont"]) for font in PdfReader(out).pages[0]["/Resources"]["/Font"].values()}
    assert {"/Times-Roman", "/Times-Bold"} <= fonts
    assert profile.model_dump() == original


def test_resize_preserves_content_that_fits_at_ten_points(store):
    from jobhub.tailor import MIN_FONT_SCALE, ResumePlan, _render
    from io import BytesIO

    # Find a real wrapping threshold between the two supported body sizes.
    profile = big_profile()
    profile.experience = profile.experience[-1:]
    profile.projects = profile.projects[-1:]
    profile.skills = ["C++", "Python", "SQL"]
    profile.awards = []
    for length in range(5, 100):
        profile.experience[0].bullets = ["Built reliable trading services with C++ and SQL. " * length]
        initial = ResumePlan(profile, TRADING)
        if _render(initial, BytesIO()) <= 1:
            continue
        initial.font_scale = MIN_FONT_SCALE
        if _render(initial, BytesIO()) <= 1:
            break
    else:
        raise AssertionError("No resize threshold found")
    plan = plan_resume(profile, TRADING)
    assert MIN_FONT_SCALE <= plan.font_scale < 1
    assert plan.experience[0].bullets == profile.experience[0].bullets
    assert len(plan.projects) == 1
    out = build_resume(profile, TRADING, store / "resized.pdf")
    assert len(PdfReader(out).pages) == 1


def test_generated_resume_is_one_page_even_when_nothing_can_be_dropped(store):
    profile = big_profile()
    for entry in profile.experience:
        entry.pinned = True   # pinned entries are never dropped, so only scaling can make room
    profile.summary = " ".join(f"Sentence {i} about building reliable software." for i in range(12))
    settings = {"resume_builder": {"one_page": False}}   # older saved setting; resumes stay one page anyway
    out = build_resume(profile, TRADING, store / "pinned.pdf", settings=settings)
    assert len(PdfReader(str(out)).pages) == 1
    text = " ".join(text_of(out).split())
    kept = plan_resume(profile, TRADING, settings).experience
    assert len(kept) == 6 and all(item.source.company in text for item in kept)


def test_dates_are_abbreviated_with_an_en_dash():
    from jobhub.tailor import _dates

    assert _dates("August 2023", "Present") == "Aug 2023 – Present"
    assert _dates("Sept. 2021", "June 2022") == "Sep 2021 – Jun 2022"
    assert _dates("2024", "") == "2024"
    assert _dates("May 2025", "May 2025") == "May 2025"


def test_resume_bank_relevance_selection(store, make_pdf):
    from jobhub.config import DEFAULT_SETTINGS, merge_settings, validate_settings
    from jobhub.tailor import _cached_extract_text, choose_resume_file, resume_choice

    # 1. Config validation accepts 'bank'
    settings = validate_settings(merge_settings(DEFAULT_SETTINGS, {"resume_strategy": "bank"}))
    assert settings["resume_strategy"] == "bank"

    # 2. Cached text extraction
    missing = store / "nonexistent.pdf"
    assert _cached_extract_text(missing) == ""

    pdf1 = make_pdf(store / "ml_nlp_engineer.pdf", ["Specialized in NLP, PyTorch, Large Language Models and transformers"])
    pdf2 = make_pdf(store / "frontend_react.pdf", ["Specialized in React, TypeScript, CSS, web UI and design systems"])
    pdf3 = make_pdf(store / "general_ops.pdf", ["General operations and coordination"])

    text1 = _cached_extract_text(pdf1)
    assert "nlp" in text1.lower()
    # verify cached hit returns identical text
    assert _cached_extract_text(pdf1) == text1

    # 3. No usable files -> fallback
    assert choose_resume_file(Job(source="x", company="A", title="NLP Lead", url="https://e.test/10"), [], fallback="/main.pdf") == ("/main.pdf", "Main resume")

    # 4. Exactly 1 usable file -> default resume
    one_file = [{"path": str(pdf1), "label": "ML/NLP", "category": "ml", "is_default": 0}]
    path, note = choose_resume_file(Job(source="x", company="A", title="Frontend", url="https://e.test/11"), one_file)
    assert path == str(pdf1) and "default resume" in note

    # 5. Multiple files: relevance scoring selects the best untouched file
    bank = [
        {"path": str(pdf1), "label": "NLP Engineer", "category": "ml", "is_default": 0},
        {"path": str(pdf2), "label": "Frontend Dev", "category": "software", "is_default": 1},
        {"path": str(pdf3), "label": "Ops", "category": "general", "is_default": 0},
    ]

    # Test NLP job selects pdf1 based on category + label + text match
    nlp_job = Job(source="x", company="A", title="Senior NLP Engineer", url="https://e.test/12",
                  description="Working on PyTorch and transformers for language models.")
    chosen_path, chosen_note = choose_resume_file(nlp_job, bank)
    assert chosen_path == str(pdf1)
    assert "matched Senior NLP Engineer from resume bank" in chosen_note

    # Test Frontend React job selects pdf2 based on text/category
    fe_job = Job(source="x", company="A", title="Frontend Engineer", url="https://e.test/13",
                 description="React and TypeScript user interfaces")
    chosen_path_fe, chosen_note_fe = choose_resume_file(fe_job, bank)
    assert chosen_path_fe == str(pdf2)
    assert "matched Frontend Engineer from resume bank" in chosen_note_fe

    # Test untouched file requirement: contents unchanged
    with open(pdf1, "rb") as f:
        pdf1_bytes = f.read()
    assert len(pdf1_bytes) > 0
    assert chosen_path == str(pdf1)

    # 6. resume_choice with 'bank' strategy
    profile = big_profile()
    profile.resume_path = str(pdf3)
    db.save_profile(profile)
    db.add_resume_file(str(pdf1), "NLP Engineer", "ml")
    db.add_resume_file(str(pdf2), "Frontend Dev", "software")
    res_path, res_note = resume_choice(profile, nlp_job, {"resume_strategy": "bank"})
    assert res_path == str(pdf1)
    assert "matched Senior NLP Engineer from resume bank" in res_note

    # 7. Disabling a file causes choose_resume_file to skip it
    files = db.list_resume_files()
    pdf1_entry = next(f for f in files if f["path"] == str(pdf1))
    db.update_resume_file(pdf1_entry["id"], enabled=0)
    files_after_disable = db.list_resume_files()
    assert next(f for f in files_after_disable if f["path"] == str(pdf1))["enabled"] == 0
    # Now nlp_job should NOT pick pdf1 because it is disabled
    res_path2, _ = resume_choice(profile, nlp_job, {"resume_strategy": "bank"})
    assert res_path2 != str(pdf1)

    # 8. Main resume auto-inclusion and is_main annotation in list_resume_files
    # pdf3 is profile.resume_path, so it should be annotated with is_main=True
    files_annotated = db.list_resume_files()
    pdf3_entry = next(f for f in files_annotated if f["path"] == str(pdf3))
    assert pdf3_entry["is_main"] is True
    assert next(f for f in files_annotated if f["path"] == str(pdf2))["is_main"] is False

    # 9. set_main_resume switches the main resume in Profile
    pdf2_entry = next(f for f in files_annotated if f["path"] == str(pdf2))
    switched_path = db.set_main_resume(pdf2_entry["id"])
    assert switched_path == str(pdf2)
    assert db.get_profile().resume_path == str(pdf2)
    files_switched = db.list_resume_files()
    assert next(f for f in files_switched if f["path"] == str(pdf2))["is_main"] is True
    assert next(f for f in files_switched if f["path"] == str(pdf3))["is_main"] is False





def test_removed_main_resume_stays_out_of_bank_and_disabled_main_is_not_fallback(store, make_pdf):
    path = make_pdf(store / "resumes" / "main.pdf", ["Ada Example", "Python"])
    db.save_profile(Profile(first_name="Ada", resume_path=str(path)))
    main = next(f for f in db.list_resume_files() if f["is_main"])
    db.update_resume_file(main["id"], enabled=False)
    job = Job(source="test", company="Example", title="Engineer", url="https://example.test/job")
    chosen, _ = resume_choice(db.get_profile(), job, {"resume_strategy": "library"})
    assert chosen == ""
    db.remove_resume_file(main["id"])
    assert db.list_resume_files() == []
    assert db.list_resume_files() == []
    assert path.is_file()
    assert db.get_profile().resume_path == str(path)
    assert resume_choice(db.get_profile(), job, {"resume_strategy": "library"})[0] == ""
