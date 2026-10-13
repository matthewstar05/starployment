from jobhub.answers import Q, factual_essay, pick_option, rule_answer
from jobhub.models import Education, Job, Profile, Project


def test_custom_answer_matches_common_question_aliases():
    settings = {"answers": {"field_answers": {"Email address": "ada@example.test"}}}
    answer = rule_answer(Q("What is your e-mail?", "text"), Profile(), settings, None)
    assert answer.value == "ada@example.test"
    assert answer.source == "user"


def test_custom_answer_matches_work_authorization_wording():
    settings = {"answers": {"field_answers": {
        "Are you legally authorized to work in the United States?": "Yes"
    }}}
    answer = rule_answer(Q("Work authorization", "select", ["Yes", "No"]), Profile(), settings, None)
    assert answer.value == "Yes"


def test_custom_answer_can_target_form_name_or_id():
    settings = {"answers": {"field_answers": {"candidate_phone": "555-0100"}}}
    answer = rule_answer(Q("Contact number", "text", name="candidate_phone"), Profile(), settings, None)
    assert answer.value == "555-0100"


def test_custom_answer_supports_typed_checkbox_values():
    settings = {"answers": {"field_answers": {"Subscribe to updates": False}}}
    answer = rule_answer(Q("Please subscribe to updates", "checkbox"), Profile(), settings, None)
    assert answer.value is False


def test_ambiguous_custom_aliases_stay_unanswered():
    settings = {"answers": {"field_answers": {
        "Email": "one@example.test", "E-mail address": "two@example.test"
    }}}
    assert rule_answer(Q("Your email", "text"), Profile(), settings, None) is None


def test_years_of_experience_is_answered_only_when_candidate_confirmed():
    question = Q("How many years of professional experience do you have?", "number")
    profile = Profile()
    assert rule_answer(question, profile, {"answers": {"years_experience": "4"}}, None) is None
    confirmed = {"answers": {"years_experience": "4", "confirmed_fields": ["years_experience"]}}
    answer = rule_answer(question, profile, confirmed, None)
    assert answer.value == "4"
    assert answer.source == "user"


def test_country_select_accepts_exact_us_dialing_code_option_only():
    assert pick_option(["Canada +1", "United States +1"], "United States") == "United States +1"
    assert pick_option(["United States +1", "United States of America +1"], "United States") is None


def test_school_punctuation_and_confirmed_us_state_match_unique_options():
    school = "California Polytechnic State University, San Luis Obispo"
    option = "California Polytechnic State University - San Luis Obispo"
    assert pick_option([option, "California Polytechnic State University - Pomona"], school) == option
    profile = Profile(state="CA", country="United States")
    assert rule_answer(Q("Which state do you currently reside in?", "combobox"),
                       profile, {}, None).value == "California"
    assert rule_answer(Q("State", "text"), profile, {}, None).value == "CA"


def test_graduation_month_maps_to_unambiguous_academic_term_only():
    question = Q("When is your expected graduation date?", "select", ["Spring 2027", "Fall 2027"])
    may = Profile(education=[Education(end="May 2027")])
    june = Profile(education=[Education(end="June 2027")])
    assert rule_answer(question, may, {}, None).value == "Spring 2027"
    assert rule_answer(question, june, {}, None) is None


def test_graduation_window_confirmation_uses_only_documented_education_date():
    question = Q("I confirm that my graduation date will be either Fall 2026 or Spring 2027",
                 "select", ["Yes", "No"])
    may = Profile(education=[Education(degree="M.S.", end="May 2027")])
    answer = rule_answer(question, may, {}, None)
    assert answer is not None and answer.value == "Yes"
    assert rule_answer(Q(question.label, "combobox"), may, {}, None).value == "Yes"
    assert rule_answer(question, Profile(education=[Education(end="June 2027")]), {}, None) is None
    assert rule_answer(question, Profile(education=[Education(end="2027")]), {}, None) is None
    assert rule_answer(Q("I confirm that I have read the privacy policy", "select", ["Yes", "No"]),
                       may, {}, None) is None


def test_undergrad_discipline_checkbox_uses_bachelors_field_not_marketing_opt_out():
    profile = Profile(education=[Education(degree="B.S.", field="Computer Science")])
    answer = rule_answer(Q("Undergrad Discipline(s) - Computer Science", "checkbox"), profile, {}, None)
    assert answer.value is True and answer.source == "resume"
    assert rule_answer(Q("Undergrad Discipline(s) - Marketing", "checkbox"), profile, {}, None) is None


def test_unconfirmed_failure_story_and_nonnumeric_salary_stay_blank():
    profile = Profile(projects=[Project(name="Sample", bullets=["Built a working service."])])
    question = Q("Describe a project where your first approach didn't work.", "textarea")
    assert factual_essay(question, profile, None) is None
    settings = {"answers": {"salary_expectation": "Negotiable", "confirmed_fields": ["salary_expectation"]}}
    assert rule_answer(Q("What is your desired salary?", "number"), profile, settings, None) is None


def test_all_attained_education_excludes_future_degree():
    profile = Profile(education=[
        Education(school="Example University", degree="B.S.", field="Computer Science",
                  end="June 2020"),
        Education(school="Graduate School", degree="M.S.", field="Economics",
                  end="May 2099"),
    ])
    answer = rule_answer(Q("Provide All Post-Secondary Education Attained", "textarea"),
                         profile, {}, None)
    assert answer.value == "Example University; B.S. in Computer Science (June 2020)"


def test_preferred_name_fields_do_not_receive_referral_answer():
    profile = Profile(first_name="Ada", last_name="Lovelace")
    settings = {"answers": {"how_heard": "Job board", "confirmed_fields": ["how_heard"]}}
    assert rule_answer(Q("Preferred First Name", "text"), profile, settings, None).value == "Ada"
    assert rule_answer(Q("Preferred Last Name", "text"), profile, settings, None).value == "Lovelace"
    assert rule_answer(Q("How did you hear about us?", "text"), profile, settings, None).value == "Job board"
    assert rule_answer(Q("[Optional] Preferred Full Name (The name that you would like to be referred to as)",
                         "text"), profile, settings, None) is None


def test_combined_current_city_and_state_uses_both_profile_fields():
    profile = Profile(city="San Luis Obispo", state="CA", country="United States")
    answer = rule_answer(
        Q("Please list the city and state/province that you are located in today.", "text"),
        profile, {}, None,
    )
    assert answer.value == "San Luis Obispo, CA"
    assert rule_answer(Q("Preferred city and state", "text"), profile, {}, None) is None


def test_currently_located_question_uses_saved_current_city_not_target_city():
    profile = Profile(city="San Luis Obispo", state="CA", country="United States")
    for wording in ("Where are you currently located?", "Where do you currently live?"):
        answer = rule_answer(Q(wording, "text"), profile, {}, None)
        assert answer is not None and answer.value == "San Luis Obispo, CA, United States"
    autocomplete = rule_answer(
        Q("Where are you currently located?", "combobox", ["San Luis, Arizona, United States"]),
        profile, {}, None,
    )
    assert autocomplete is not None and autocomplete.value == "San Luis Obispo, CA, United States"


def test_job_country_work_authorization_needs_confirmed_fact_and_us_posting():
    question = Q("Are you authorized to work in the country where the job is located?",
                 "radio", ["Yes", "No"])
    profile = Profile(city="San Luis Obispo", state="CA", country="United States")
    confirmed = {"answers": {"work_authorized_us": "Yes", "confirmed_fields": ["work_authorized_us"]}}

    def posting(location):
        return Job(source="ashby", ats="ashby", company="Example", title="Software Engineer",
                   location=location, url="https://jobs.ashbyhq.com/example/123")

    answer = rule_answer(question, profile, confirmed, posting("San Francisco"))
    assert answer is not None and answer.value == "Yes" and answer.source == "user"
    assert rule_answer(question, profile, confirmed,
                       posting("San Francisco, CA, United States")).value == "Yes"
    assert rule_answer(question, profile, confirmed, posting("Toronto, ON")) is None
    assert rule_answer(question, profile, confirmed, posting("United States / Canada")) is None
    assert rule_answer(question, profile, confirmed, posting("")) is None
    assert rule_answer(question, profile, {}, posting("San Francisco")) is None
    assert rule_answer(Q("Are you not authorized to work in the country where the job is located?",
                         "radio", ["Yes", "No"]), profile, confirmed, posting("San Francisco")) is None


def test_combined_us_citizen_or_resident_answer_stays_narrow():
    profile = Profile()
    settings = {"answers": {"us_citizen_or_permanent_resident": "Yes",
                            "confirmed_fields": ["us_citizen_or_permanent_resident"]}}
    combined = Q("Are you a U.S. citizen or permanent resident?", "radio", ["Yes", "No"])
    answer = rule_answer(combined, profile, settings, None)
    assert answer is not None and answer.value == "Yes" and answer.source == "user"
    assert rule_answer(combined, profile, {"answers": {
        "us_citizen_or_permanent_resident": "Yes", "confirmed_fields": []}}, None) is None
    assert rule_answer(Q("Are you a U.S. citizen?", "radio", ["Yes", "No"]),
                       profile, settings, None) is None
    assert rule_answer(Q("Are you a U.S. citizen or permanent resident with security clearance?",
                         "radio", ["Yes", "No"]), profile, settings, None) is None


def test_conditional_follow_up_waits_for_an_explicit_answer():
    profile = Profile()
    settings = {"answers": {"how_heard": "Job board", "confirmed_fields": ["how_heard"]}}
    assert rule_answer(Q("If so, please specify the type of degree you plan to pursue.", "text"),
                       profile, settings, None) is None
    assert rule_answer(Q("How did you first hear about Five Rings?", "text"),
                       profile, settings, None).value == "Job board"


def test_social_links_and_school_subfields_use_matching_facts_only():
    profile = Profile(website="https://ada.example", education=[Education(
        school="Example University", degree="M.S.", field="Computer Science",
        start="August 2023", end="May 2027",
    )])
    assert rule_answer(Q("Twitter URL", "text"), profile, {}, None) is None
    assert rule_answer(Q("Other website", "text"), profile, {}, None).value == "https://ada.example"
    assert rule_answer(Q("School Major", "textarea"), profile, {}, None).value == "Computer Science"
    assert rule_answer(Q("School Minor", "textarea"), profile, {}, None) is None
    assert rule_answer(Q("Name of School", "text"), profile, {}, None).value == "Example University"
    assert rule_answer(Q("Start Date at Current School", "text"), profile, {}, None).value == "August 2023"


def test_office_attendance_and_specific_city_are_not_inferred_from_relocation():
    settings = {"answers": {"willing_to_relocate": "Yes", "confirmed_fields": ["willing_to_relocate"]}}
    profile = Profile()
    assert rule_answer(Q("If you are not local, are you willing to relocate?", "radio", ["Yes", "No"]),
                       profile, settings, None).value == "Yes"
    assert rule_answer(Q("Are you planning to relocate to NYC and work in our NYC office?",
                         "radio", ["Yes", "No"]), profile, settings, None) is None
    assert rule_answer(Q("Would you relocate to New York?", "radio", ["Yes", "No"]),
                       profile, settings, None) is None


def test_unknown_candidate_facts_and_legal_consent_require_saved_answers():
    profile = Profile()
    labels = [
        "Applicant Privacy Statement", "Agreement to Arbitrate",
        "I certify that all information submitted is truthful and complete",
        "Are you over the age of 18?", "Are you at least 18 years old?",
        "Are you a citizen or permanent resident of Cuba, Iran, North Korea, Syria, or Sudan?",
        "Have you ever lived in the DPRK?",
        "Are you currently bound by any non-compete, non-solicitation, or restrictive covenant agreement?",
        "Have you worked at DoorDash before?", "Have you applied to this company in the past?",
        "Have you ever been discharged or forced to resign from employment?",
    ]
    for label in labels:
        question = Q(label, "radio", ["Yes", "No"])
        assert rule_answer(question, profile, {}, None) is None
        saved = {"answers": {"field_answers": {label: "No"}}}
        assert rule_answer(question, profile, saved, None).value == "No"


def test_graduation_timing_and_degree_status():
    profile = Profile(education=[
        Education(school="Example University", degree="M.S.", field="Computer Science",
                  start="August 2025", end="May 2027")
    ])
    q_year = Q("Year you expect to complete your current degree", "select", ["2026", "2027", "2028"])
    ans_year = rule_answer(q_year, profile, {}, None)
    assert ans_year is not None and ans_year.value == "2027"

    q_month = Q("Month you expect to complete your current degree", "select", ["April", "May", "June"])
    ans_month = rule_answer(q_month, profile, {}, None)
    assert ans_month is not None and ans_month.value == "May"

    q_affil = Q("What is your current affiliation with your University?", "select",
                ["Completing Bachelors Degree", "Completing Masters Degree", "Completing PhD"])
    ans_affil = rule_answer(q_affil, profile, {}, None)
    assert ans_affil is not None and ans_affil.value == "Completing Masters Degree"

    q_highest = Q("Highest level of education", "select",
                  ["High School", "College Degree", "Master's Degree", "Doctorate"])
    ans_highest = rule_answer(q_highest, profile, {}, None)
    assert ans_highest is not None and ans_highest.value == "Master's Degree"


def test_notice_period_and_referral_source():
    profile = Profile()
    q_notice = Q("Notice period", "select", ["Immediate", "2 weeks", "1 month", "2 months"])
    ans_notice = rule_answer(q_notice, profile, {}, None)
    assert ans_notice is None

    q_source = Q("How did you hear about this opportunity?", "select",
                 ["LinkedIn", "Handshake", "Campus Event", "Other"])
    ans_source = rule_answer(q_source, profile, {}, None)
    assert ans_source is None


def test_state_and_country_dropdown_matching():
    profile = Profile(city="Thousand Oaks", state="CA", country="United States")
    q_state = Q("State", "select", ["California", "New York", "Texas"])
    ans_state = rule_answer(q_state, profile, {}, None)
    assert ans_state is not None and ans_state.value == "California"

    q_country = Q("Country", "select", ["United States", "Canada", "United Kingdom"])
    ans_country = rule_answer(q_country, profile, {}, None)
    assert ans_country is not None and ans_country.value == "United States"



def test_saved_headline_summary_and_localized_contact_fields_are_extractive():
    profile = Profile(first_name="Ada", last_name="Example", headline="Data engineer", summary="Built Python services")
    assert rule_answer(Q("Headline", "text"), profile, {}, None).value == profile.headline
    assert rule_answer(Q("Summary", "textarea"), profile, {}, None).value == profile.summary
    assert rule_answer(Q("Prénom", "text", name="firstname"), profile, {}, None).value == "Ada"
    assert rule_answer(Q("Nom de famille", "text", name="lastname"), profile, {}, None).value == "Example"
    assert rule_answer(Q("Summary", "textarea"), Profile(), {}, None) is None
def test_choose_not_to_disclose_is_an_answer_not_a_placeholder():
    from jobhub.answers import pick_option
    assert pick_option(["Choose...", "Male", "Female", "Choose not to disclose"], "decline") == "Choose not to disclose"


def test_phone_country_code_requires_matching_saved_number_and_country():
    from jobhub.answers import Q, rule_answer, pick_option
    from jobhub.models import Profile
    q = Q("Phone country code", "combobox", ["+1 CA - Canada", "+1 US - United States"])
    candidate = Profile(phone="415-555-0134", country="United States")
    assert rule_answer(q, candidate, {}, None).value == "+1 US - United States"
    assert pick_option(q.options, "+1 US") == "+1 US - United States"
    candidate.phone = "+44 20 7946 0958"
    assert rule_answer(q, candidate, {}, None) is None
