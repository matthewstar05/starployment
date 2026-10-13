"""Candidate facts are evidence, not defaults or model-generated claims."""
import json
from types import SimpleNamespace

import pytest

from jobhub import config
from jobhub.answers import Q, cover_letter, llm_answers, pick_option, rule_answer
from jobhub.llm import LLM
from jobhub.models import Job
from jobhub.profile import heuristic_profile
from jobhub.tailor import polish_bullets

RESUME = """Ada Lovelace
Software Developer
ada@example.com | (805) 555-1234 | https://example.com
EDUCATION
Example University  Boston, MA
M.S. Computer Science  September 2025 - May 2027
▪ GPA: 3.9 / 4.0
B.S. Mathematics  September 2020 - June 2024
PROJECTS & RESEARCH
Exact Search  Boston, MA | January 2025 - Present
Developer
▪ Built a Python search service using a local corpus.
▪ Evaluated ranking quality with 40 queries and
  published the evaluation results.
EXPERIENCE
Example Club  Boston, MA | October 2024 - Present
President (2025-2026), Developer (2024-2025)
▪ Maintained the member directory.
SKILLS & INTERESTS
▪ Technical Skills:
  o Languages: Python, C++, SQL
  o Tools: Git, Docker, LLM
  Integration
"""


@pytest.fixture
def candidate():
    return heuristic_profile(RESUME)


@pytest.fixture
def job():
    return Job(source="fixture", company="Example", title="Software Engineer", url="https://example.org/job",
               description="Python search ranking and SQL")


def test_local_resume_ingest_preserves_records_and_wrapped_facts(candidate):
    assert candidate.full_name == "Ada Lovelace"
    assert len(candidate.education) == 2
    assert candidate.education[0].school == candidate.education[1].school == "Example University"
    assert candidate.education[0].gpa == "3.9 / 4.0"
    assert len(candidate.experience) == 1
    assert candidate.experience[0].company == "Example Club"
    assert candidate.experience[0].title == "President (2025-2026), Developer (2024-2025)"
    assert candidate.projects[0].bullets[1] == "Evaluated ranking quality with 40 queries and published the evaluation results."
    assert "LLM Integration" in candidate.skills
    assert (candidate.city, candidate.state, candidate.country) == ("", "", "")


def test_unknown_resume_does_not_invent_education_or_location():
    profile = heuristic_profile("Sam Example\nsam@example.com\nEXPERIENCE\nExample University")
    assert profile.education == []
    for label in ["Degree", "City", "Country", "Field of study"]:
        assert rule_answer(Q(label, "text"), profile, {}, None) is None


@pytest.mark.parametrize("label", [
    "Will you now or in the future require sponsorship?", "Are you legally authorized to work in the United States?",
    "Are you willing to relocate?", "Are you at least 18?", "Are you a US citizen?", "Do you have a security clearance?",
    "Can you commute to our office?", "Have you previously applied here?", "Desired salary", "When can you start?",
    "How many years of professional experience?", "What is your nationality?",
])
def test_unknown_personal_facts_do_not_get_default_answers(label, candidate, job):
    legacy = {"answers": {"work_authorized_us": "Yes", "requires_sponsorship": "No", "willing_to_relocate": "Yes"}}
    assert rule_answer(Q(label, "text", required=True), candidate, legacy, job) is None
    assert llm_answers([Q(label, "textarea", required=True)], candidate, job, legacy) == {}


def test_confirmed_answers_and_exact_overrides(candidate, job):
    settings = {"answers": {"requires_sponsorship": "No", "confirmed_fields": ["requires_sponsorship"],
                            "field_answers": {"I agree to the privacy notice": True, "What is your nationality?": "Example"}}}
    assert rule_answer(Q("Will you require sponsorship?", "select", ["Yes", "No"]), candidate, settings, job).value == "No"
    assert rule_answer(Q("I agree to the privacy notice *", "checkbox"), candidate, settings, job).value is True
    assert rule_answer(Q("What is your nationality?", "text"), candidate, settings, job).value == "Example"
    assert rule_answer(Q("Do you NOT require sponsorship?", "select", ["Yes", "No"]), candidate, settings, job) is None
    assert rule_answer(Q("I certify this application is accurate", "checkbox"), candidate, settings, job) is None
    settings["answers"]["field_answers"]["I certify this application is accurate"] = "true"
    assert rule_answer(Q("I certify this application is accurate", "checkbox"), candidate, settings, job) is None


def test_demographics_never_guess_a_negative(candidate):
    assert rule_answer(Q("Veteran status", "select", ["Yes", "No"]), candidate, {}, None) is None
    assert rule_answer(Q("Disability", "select", ["Yes", "No", "I do not wish to answer"]), candidate, {}, None).value == "I do not wish to answer"
    racial = Q("Which racial categories best describe you? Select all that apply.", "select",
               ["Asian", "White", "I don't wish to answer"], True)
    assert rule_answer(racial, candidate, {}, None).value == "I don't wish to answer"


def test_select_matching_rejects_partial_and_ambiguous_answers():
    assert pick_option(["California University", "California State University"], "California") is None
    assert pick_option(["Yes, citizen", "Yes, permanent resident", "No"], "Yes") is None
    assert pick_option(["United Kingdom", "United States"], "United") is None
    assert pick_option(["Master’s Degree"], "Master's Degree") == "Master’s Degree"
    assert pick_option(["Yes", "No"], "No") == "No"


def test_completed_degree_is_distinct_from_current_degree(candidate):
    assert rule_answer(Q("Degree", "text"), candidate, {}, None).value == "Master's Degree"
    assert rule_answer(Q("Highest completed degree", "text"), candidate, {}, None).value == "Bachelor's Degree"


def test_tailoring_cannot_invent_nonnumeric_claims(candidate, job, monkeypatch):
    monkeypatch.setattr("jobhub.llm.get_llm", lambda: pytest.fail("Tailoring must not call a model"))
    bullets = ["Built a Python search service using a local corpus."]
    assert polish_bullets(bullets, job, candidate) == bullets


def test_local_factual_essay_and_cover_letter_preserve_source(candidate, job):
    answer = rule_answer(Q("Describe a relevant project", "textarea"), candidate, {}, job)
    assert answer.source == "resume"
    assert candidate.projects[0].bullets[0] in answer.value
    assert rule_answer(Q("Describe a time you showed grit", "textarea"), candidate, {}, job) is None
    letter = cover_letter(candidate, job, {})
    assert candidate.projects[0].bullets[0] in letter
    assert "applying for the Software Engineer role" in letter


def test_llm_auto_detection_never_invokes_a_cli(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr("jobhub.llm.shutil.which", lambda _: "/bin/example")
    monkeypatch.setattr("jobhub.llm.subprocess.run", lambda *a, **kw: pytest.fail("Detection made an external call"))
    assert not LLM("auto").available()
    assert LLM("claude_cli").available()
    assert not LLM("codex_cli").available()


def test_explicit_claude_is_tool_free_and_bounded(monkeypatch):
    monkeypatch.setattr("jobhub.llm.shutil.which", lambda _: "/bin/example")
    calls = []
    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": '{"ok":true}'}), stderr="")
    monkeypatch.setattr("jobhub.llm.subprocess.run", fake_run)
    assert LLM("claude_cli").json("job description", timeout=999) == {"ok": True}
    args, kwargs = calls[0]
    assert args[args.index("--tools") + 1] == ""
    assert "--safe-mode" in args and "--strict-mcp-config" in args
    assert kwargs["timeout"] == 45


def test_malformed_llm_json_is_not_retried(monkeypatch):
    monkeypatch.setattr("jobhub.llm.shutil.which", lambda _: "/bin/example")
    llm = LLM("claude_cli")
    calls = []
    monkeypatch.setattr(llm, "_call", lambda *a, **kw: calls.append(1) or "not json")
    with pytest.raises(ValueError):
        llm.json("one attempt")
    assert len(calls) == 1


def test_degree_options_completed_and_expected_are_not_interchangeable(candidate):
    options = ["Bachelor's", "Master's"]
    assert rule_answer(Q("Degree you are currently pursuing", "select", options), candidate, {}, None).value == "Master's"
    assert rule_answer(Q("Highest degree earned", "select", options), candidate, {}, None).value == "Bachelor's"
    status_options = ["Bachelor's degree (completed)", "Master's degree (in progress)", "Master's degree (completed)"]
    assert rule_answer(Q("Degree", "select", status_options), candidate, {}, None).value == "Master's degree (in progress)"
    assert rule_answer(Q("Highest completed degree", "select", status_options), candidate, {}, None).value == "Bachelor's degree (completed)"
    assert rule_answer(Q("Graduation date", "text"), candidate, {}, None).value == "May 2027"


def test_eligibility_can_use_explicit_opt_out_without_assuming_facts(candidate):
    options = ["Yes", "No", "I don't wish to answer"]
    labels = ["Are you legally authorized to work in the United States?",
              "Do you currently, or will you in the future, require sponsorship for employment authorization?"]
    for label in labels:
        answer = rule_answer(Q(label, "select", options, required=True), candidate, {}, None)
        assert answer.value == "I don't wish to answer"
        assert rule_answer(Q(label, "select", ["Yes", "No"], required=True), candidate, {}, None) is None
    confirmed = {"answers": {"work_authorized_us": "Yes", "confirmed_fields": ["work_authorized_us"]}}
    assert rule_answer(Q(labels[0], "select", options), candidate, confirmed, None).value == "Yes"


def test_education_details_keep_coursework_concentration_and_activities():
    profile = heuristic_profile("""Ada Lovelace
ada@example.test
EDUCATION
Example University  Boston, MA
Master of Science in Economics | Graduate School  July 2026 - May 2027
▪  GPA: 3.8 / 4.0 | Relevant Coursework: Econometrics, Machine Learning,
  Causal Inference
Bachelor of Science in Computer Science | School of Computing  August 2022 - June 2026
Concentration: Machine Learning | Minor: Economics
▪  Relevant Coursework: Algorithms, Databases
▪  Activities/Societies: Chess Club, Robotics
EXPERIENCE
Example Club  Boston, MA | October 2024 - Present
Member
▪ Maintained the member directory.
Community Service: Food Bank, Beach Cleanup
""")
    masters, bachelors = profile.education
    assert masters.gpa == "3.8 / 4.0"
    assert masters.location == "Boston, MA"
    assert masters.details == ["Relevant Coursework: Econometrics, Machine Learning, Causal Inference"]
    assert bachelors.details == ["Concentration: Machine Learning | Minor: Economics",
                                 "Relevant Coursework: Algorithms, Databases",
                                 "Activities/Societies: Chess Club, Robotics"]
    assert profile.awards == ["Community Service: Food Bank, Beach Cleanup"]
