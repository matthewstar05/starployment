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
    assert "C++" in text.split("SKILLS")[-1][:200]


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
