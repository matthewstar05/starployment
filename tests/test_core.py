"""Fast unit tests for the LLM-free core: scoring, answer rules, option picking, salary parsing, JSON extraction."""
from jobhub.answers import Q, pick_option, rule_answer
from jobhub.config import DEFAULT_SETTINGS
from jobhub.llm import extract_json
from jobhub.models import Education, Job, Profile
from jobhub.scoring import score_job
from jobhub.sources.base import parse_salary, title_wanted

PROFILE = Profile(first_name="Ada", last_name="Lovelace", email="ada@example.com", phone="8055551234", city="San Luis Obispo",
                  state="CA", skills=["Python", "C++", "SQL", "PyTorch"], linkedin="https://linkedin.com/in/ada",
                  education=[Education(school="Cal Poly", degree="B.S. Computer Science", field="Computer Science", end="June 2026")])


def _job(**kw) -> Job:
    base = dict(source="greenhouse", ats="greenhouse", company="Acme", title="Software Engineer, New Grad",
                location="San Francisco, CA", url="https://x/1", description="We use Python and C++. 0-2 years experience.")
    return Job(**(base | kw))


def test_scoring_prefers_entry_level_and_matching_skills():
    s_new, d = score_job(_job(), PROFILE, DEFAULT_SETTINGS)
    s_senior, _ = score_job(_job(title="Senior Staff Engineer", description="10+ years required"), PROFILE, DEFAULT_SETTINGS)
    assert s_new > s_senior
    assert d["skills_fit"] > 0 and "python" in d["matched_skills"].lower()


def test_title_filter_drops_senior_and_interns_by_default():
    assert title_wanted("Software Engineer", DEFAULT_SETTINGS)
    assert not title_wanted("Senior Software Engineer", DEFAULT_SETTINGS | {"exclude_title_words": ["senior"]})
    assert not title_wanted("Software Engineering Intern", DEFAULT_SETTINGS)


def test_pick_option_yes_no_decline():
    assert pick_option(["Select...", "Yes", "No"], "no") == "No"
    assert pick_option(["Male", "Female", "Decline to self-identify"], "decline") == "Decline to self-identify"
    assert pick_option(["Bachelor's Degree", "Master's Degree"], "Bachelor's Degree") == "Bachelor's Degree"


def test_rule_answers_identity_and_eligibility():
    assert rule_answer(Q("First Name", "text"), PROFILE, DEFAULT_SETTINGS, None).value == "Ada"
    assert rule_answer(Q("Email", "text"), PROFILE, DEFAULT_SETTINGS, None).value == "ada@example.com"
    a = rule_answer(Q("Will you now or in the future require sponsorship?", "combobox", ["Yes", "No"]), PROFILE, DEFAULT_SETTINGS, None)
    assert a is None  # sponsorship is unknown until explicitly confirmed
    g = rule_answer(Q("Gender", "select", ["Select", "Male", "Female", "I don't wish to answer"]), PROFILE, DEFAULT_SETTINGS, None)
    assert g.value == "I don't wish to answer"
    assert rule_answer(Q("Degree", "combobox", ["Bachelor's Degree", "Master's Degree"]), PROFILE, DEFAULT_SETTINGS, None).value == "Bachelor's Degree"
    assert rule_answer(Q("End date year", "number"), PROFILE, DEFAULT_SETTINGS, None).value == "2026"
    assert rule_answer(Q("Describe a time you showed grit", "textarea"), PROFILE, DEFAULT_SETTINGS, None) is None  # -> LLM


def test_salary_and_json():
    assert parse_salary("Base pay range: $120,000 - $150,000 USD") == (120000, 150000)
    assert parse_salary("$95k–$110k") == (95000, 110000)
    assert extract_json('Sure!\n```json\n{"a": [1, 2]}\n```') == {"a": [1, 2]}
