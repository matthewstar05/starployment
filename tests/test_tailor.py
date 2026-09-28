from pypdf import PdfReader

from jobhub.models import Education, Experience, Job, Profile, Project
from jobhub.tailor import build_resume, tailored_summary


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
    assert text.index("SUMMARY") < text.index("Built Python data pipelines for two research projects.")
    assert "Designed SQL schemas for a campus database project." in text
    assert "Database Analyst Candidate" in text
    assert "ten years" not in text


def test_empty_saved_summary_is_omitted_instead_of_written_from_job_text(tmp_path):
    profile = sample_profile()
    profile.summary = ""
    target = tmp_path / "no-invented-summary.pdf"

    build_resume(profile, job("Security Analyst", "Security clearance required"), target)

    text = "\n".join(page.extract_text() or "" for page in PdfReader(target).pages)
    assert "SUMMARY" not in text
    assert "security clearance" not in text.lower()
