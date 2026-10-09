from pypdf import PdfReader

from jobhub.models import Education, Experience, Job, Profile, Project
from jobhub.tailor import ResumePlan, _story, build_resume, tailored_headline, tailored_summary


def sample_profile():
    return Profile(
        first_name="Casey",
        last_name="Example",
        headline="Graduate student in information systems",
        summary=(
            "Built Python data pipelines for research projects. "
            "Designed SQL schemas for a campus database project. "
            "Presented findings to student groups."
        ),
        email="casey@example.test",
        education=[Education(school="Example University", degree="M.S.", field="Information Systems")],
        experience=[Experience(company="Example Lab", title="Research Assistant", bullets=[
            "Built Python data pipelines for two research projects.",
            "Presented findings to student groups.",
        ])],
        projects=[Project(name="Campus Database", tech=["SQL"], bullets=[
            "Designed SQL schemas for a campus database project.",
        ])],
        skills=["Python", "SQL", "Public speaking"],
    )


def job(title, description):
    return Job(source="fixture", company="Example Co", title=title, url=f"https://example.test/{title}",
               description=description)


def test_summary_reorders_saved_sentences_for_each_posting_without_using_jd_claims():
    profile = sample_profile()
    data_job = job("Data Analyst", "Python pipelines and research analysis. Must have ten years of experience.")
    database_job = job("Database Analyst", "SQL schemas designed for a campus database. Must have a security clearance.")

    data_summary = tailored_summary(profile, data_job)
    database_summary = tailored_summary(profile, database_job)

    assert data_summary.startswith("Built Python data pipelines")
    assert database_summary.startswith("Designed SQL schemas")
    assert data_summary != database_summary
    for summary in (data_summary, database_summary):
        assert "ten years" not in summary
        assert "security clearance" not in summary
        assert "Presented findings" not in summary


def test_resume_pdf_contains_tailored_extracts_and_preserves_source_bullets(tmp_path):
    profile = sample_profile()
    target = tmp_path / "database-role.pdf"
    build_resume(profile, job("Database Analyst", "SQL schema database design"), target)

    text = "\n".join(page.extract_text() or "" for page in PdfReader(target).pages)

    assert "Designed SQL schemas for a campus database project." in text
    assert "Built Python data pipelines for two research projects." in text
    # Summary is omitted when experience or projects exist
    assert "Summary" not in text
    assert "Designed SQL schemas for a campus database project." in text
    assert "Database Analyst" not in text
    assert profile.headline in text
    assert "ten years" not in text


def test_empty_saved_summary_is_omitted_instead_of_written_from_job_text(tmp_path):
    profile = sample_profile()
    profile.summary = ""
    target = tmp_path / "no-invented-summary.pdf"

    build_resume(profile, job("Security Analyst", "Security clearance required"), target)

    text = "\n".join(page.extract_text() or "" for page in PdfReader(target).pages)
    assert "Summary" not in text
    assert "security clearance" not in text.lower()


def test_headline_never_adds_the_target_title_even_without_a_saved_headline():
    profile = sample_profile()
    target = job("Security Analyst", "Security clearance required")
    assert tailored_headline(profile, target) == profile.headline
    profile.headline = ""
    assert tailored_headline(profile, target) == ""


def test_job_terms_appear_in_saved_skills_and_accomplishments_only(tmp_path):
    profile = sample_profile()
    profile.skills = ["Public speaking", "Python"]
    target = tmp_path / "natural-keywords.pdf"
    build_resume(profile, job("Database Analyst", "SQL schema design; Snowflake and a security clearance required"), target)
    text = " ".join(PdfReader(target).pages[0].extract_text().split())
    skills = text.split("Skills", 1)[1].split("Summary", 1)[0].strip()
    assert skills.startswith("SQL,")  # supported by the selected project's saved tools
    assert "Designed SQL schemas for a campus database project." in text
    assert "Snowflake" not in text and "security clearance" not in text
    assert "Database Analyst" not in text


def test_education_sorting_places_ms_above_bs_regardless_of_job_keywords():
    profile = sample_profile()
    profile.education = [
        Education(school="Example University", degree="B.S.", field="Computer Science", end="2026"),
        Education(school="Example University", degree="M.S.", field="Computer Science", end="2027"),
    ]
    # Job explicitly favors bachelor / B.S. keywords
    bs_job = job("Software Engineer", "Software engineer requiring bachelor degree or B.S. in computer science")
    plan = ResumePlan(profile, bs_job)

    assert len(plan.education) == 2
    assert plan.education[0].degree == "M.S."
    assert plan.education[1].degree == "B.S."


def test_education_two_row_formatting_and_gpa_layout():
    profile = sample_profile()
    profile.education = [
        Education(school="Stanford University", degree="M.S.", field="Computer Science",
                  location="Stanford, CA", end="June 2024", gpa="3.95", details=["Thesis on RL"]),
        Education(school="UC Berkeley", degree="B.S.", field="EECS",
                  location="", end="May 2022", gpa="", details=[]),
    ]
    target_job = job("Software Engineer", "Python backend engineering")
    plan = ResumePlan(profile, target_job)
    story = _story(plan)

    from reportlab.platypus import Paragraph, Table
    tables = [elem for elem in story if isinstance(elem, Table)]
    # Look for table rows corresponding to education
    # Education 1: Row 1 has school and location; Row 2 has degree · GPA and end date
    # Find table with Stanford University
    edu1_row1 = next(t for t in tables if any("Stanford University" in cell.text for cell in t._cellvalues[0] if isinstance(cell, Paragraph)))
    assert edu1_row1._cellvalues[0][0].text == "<b>Stanford University</b>"
    assert edu1_row1._cellvalues[0][1].text == "Stanford, CA"

    edu1_row2 = next(t for t in tables if any("GPA 3.95" in getattr(cell, "text", "") for cell in t._cellvalues[0]))
    assert "M.S. in Computer Science · GPA 3.95" in edu1_row2._cellvalues[0][0].text
    assert edu1_row2._cellvalues[0][1].text == "Jun 2024"

    # Education 2 (no location): Row 1 has school and date (May 2022); Row 2 has degree only, right is empty
    edu2_row1 = next(t for t in tables if any("UC Berkeley" in cell.text for cell in t._cellvalues[0] if isinstance(cell, Paragraph)))
    assert edu2_row1._cellvalues[0][0].text == "<b>UC Berkeley</b>"
    assert edu2_row1._cellvalues[0][1].text == "May 2022"


def test_education_clean_field_pipe_splitting_and_summary_condition():
    profile = sample_profile()
    profile.experience = []
    profile.projects = []
    profile.summary = "Passionate computer scientist."
    profile.education = [
        Education(school="MIT", degree="B.S. in Computer Science", field="Computer Science | Artificial Intelligence",
                  location="", end="2025", gpa="4.0", details=[]),
    ]
    plan = ResumePlan(profile, job("SWE", "Python"))
    story = _story(plan)

    from reportlab.platypus import Paragraph, Table
    # Summary should be present since experience and projects are empty
    assert any(isinstance(elem, Paragraph) and "Passionate computer scientist." in elem.text for elem in story)

    tables = [elem for elem in story if isinstance(elem, Table)]
    edu_row1 = next(t for t in tables if any("MIT" in getattr(cell, "text", "") for cell in t._cellvalues[0]))
    assert edu_row1._cellvalues[0][0].text == "<b>MIT</b>"
    assert edu_row1._cellvalues[0][1].text == "2025"

    # Degree should not repeat "in Computer Science"
    edu_row2 = next(t for t in tables if any("B.S. in Computer Science" in getattr(cell, "text", "") for cell in t._cellvalues[0]))
    assert edu_row2._cellvalues[0][0].text == "B.S. in Computer Science"
    assert edu_row2._cellvalues[0][1].text == "GPA: 4.0"
