"""Standing answers: the candidate's once-for-every-employer facts, and only on choice questions."""
from jobhub import config
from jobhub.answers import Q, rule_answer
from jobhub.models import Experience, Job, Profile

YES_NO = ["Yes", "No"]


def settings(**answers):
    saved = {"over_18": "Yes", "relatives_at_employer": "No", "non_compete": "No", "security_clearance": "No",
             "former_employee": "No", "application_terms_consent": "Yes", **answers}
    merged = config.merge_settings(config.DEFAULT_SETTINGS, {"answers": {
        **saved, "confirmed_fields": [key for key, value in saved.items() if value]}})
    return config.validate_settings(merged)


def ask(label, kind="radio", options=YES_NO, ats="workday", company="Bank", profile=None, **answers):
    job = Job(source="workday", ats=ats, company=company, title="Analyst", url="https://bank.wd5.myworkdayjobs.com/x")
    answer = rule_answer(Q(label=label, kind=kind, options=options), profile or Profile(), settings(**answers), job)
    return None if answer is None else answer.value


def test_standing_facts_answer_choice_questions():
    assert ask("Are you at least 18 years of age?") == "Yes"
    assert ask("Do you have any relatives or family members currently employed by Bank?") == "No"
    assert ask("Are you bound by a non-compete or non-solicitation agreement?") == "No"
    assert ask("Do you currently hold an active security clearance?") == "No"
    assert ask("Have you previously worked for Bank?", kind="select", options=["Yes", "No"]) == "No"


def test_inverted_conditional_and_free_text_wording_stays_with_the_candidate():
    assert ask("Are you under the age of 18?") is None
    assert ask("Are you willing and able to obtain a security clearance?") is None
    assert ask("Are you eligible to obtain a clearance?") is None
    assert ask("If yes, list the relatives who work here", kind="text", options=[]) is None
    assert ask("Do you have relatives employed here?", kind="text", options=[]) is None


def test_former_employee_is_no_only_when_the_employer_is_not_on_the_resume():
    resume = Profile(experience=[Experience(company="Bank of Example", title="Intern")])
    assert ask("Have you previously been employed by Bank of Example?", company="Bank of Example",
               profile=resume) is None
    assert ask("Have you ever worked for Other Co?", company="Other Co", profile=resume) == "No"
    assert ask("Have you previously worked for Bank?", former_employee="") is None


def test_unset_answers_are_never_used():
    assert ask("Are you at least 18 years of age?", over_18="") is None
    assert ask("Do you have family members working at Bank?", relatives_at_employer="") is None


def test_terms_box_is_ticked_only_on_workday_with_consent_and_never_a_certification():
    box = dict(kind="checkbox", options=[])
    assert ask("Yes, I have read and consent to the terms and conditions", **box) is True
    assert ask("I acknowledge the privacy notice and consent to the processing of my data", **box) is True
    assert ask("I certify that the information provided is true and complete", **box) is None
    assert ask("I agree to the arbitration agreement", **box) is None
    assert ask("Yes, I have read and consent to the terms and conditions", ats="greenhouse", **box) is None
    assert ask("Yes, I have read and consent to the terms and conditions", application_terms_consent="", **box) is None


def test_a_list_introduced_by_not_limited_to_is_not_a_negation():
    label = ("Have you ever been employed by Citi or any of its predecessor companies (including but not limited to "
             "Citibank, Citicorp, Banamex, Salomon Brothers, or Smith Barney) whether as an employee or via an agency "
             "or as a contractor, temporary worker or consultant?")
    assert ask(label, kind="select", company="Citi") == "No"
    assert ask("Have you not previously worked for Citi?", company="Citi") is None


def test_job_country_authorization_and_referral_follow_saved_answers():
    from jobhub.answers import Q, rule_answer
    base = settings()
    base["answers"].update(work_authorized_us="Yes", how_heard="Job board", workday_how_heard="LinkedIn")
    base["answers"]["confirmed_fields"] += ["work_authorized_us", "how_heard", "workday_how_heard"]
    job = Job(source="workday", ats="workday", company="Citi", title="Trader", location="New York, NY",
              url="https://citi.wd5.myworkdayjobs.com/2/job/New-York/Trader_1")
    def a(label):
        answer = rule_answer(Q(label=label, kind="radio", options=YES_NO), Profile(), base, job)
        return None if answer is None else answer.value
    assert a("Are you legally authorized to work in the country or jurisdiction where the position to which "
             "you are applying is located?") == "Yes"
    assert a("To the best of your knowledge, are you a referral of a current Senior Commercial Person (SCP)?") == "No"
    base["answers"]["workday_how_heard"] = "Employee referral"
    assert a("To the best of your knowledge, are you a referral of a current Senior Commercial Person (SCP)?") is None


def test_country_in_which_the_job_is_located_and_communication_language():
    from jobhub.answers import Q, rule_answer
    base = settings()
    base["answers"].update(work_authorized_us="Yes", native_language="English")
    base["answers"]["confirmed_fields"] += ["work_authorized_us", "native_language"]
    job = Job(source="workday", ats="workday", company="RBC", title="Analyst", location="New York, NY",
              url="https://rbc.wd3.myworkdayjobs.com/x")
    def a(label, options=YES_NO):
        answer = rule_answer(Q(label=label, kind="select", options=options), Profile(), base, job)
        return None if answer is None else answer.value
    assert a("Are you legally eligible to work in the country in which this job is located?") == "Yes"
    assert a("Please indicate your language preference for receiving communications:", ["English", "French"]) == "English"


def test_offer_named_firms_and_listed_location_follow_saved_answers():
    resume = Profile(experience=[Experience(company="Theta Tau", title="Director")])
    assert ask("Please be advised that if you provide inaccurate answers, any offer may be withdrawn. Have you ever "
               "accepted an offer to work at Fidelity in any capacity?", kind="select", company="Fidelity",
               profile=resume) == "No"
    assert ask("Do you currently, or have you ever, worked for either PricewaterhouseCoopers (PwC) or Deloitte, or "
               "any of their affiliates?", profile=resume) == "No"
    worked = Profile(experience=[Experience(company="Deloitte Consulting", title="Analyst")])
    assert ask("Do you currently, or have you ever, worked for either PricewaterhouseCoopers (PwC) or Deloitte?",
               profile=worked) is None
    assert ask("Are you interested in and able to work at the location(s) listed on this opening?",
               willing_to_relocate="Yes") == "Yes"
    assert ask("Are you interested in and able to work at the location(s) listed on this opening?") is None


def test_accuracy_box_needs_its_own_consent_and_never_covers_a_signature_or_background_check():
    box = dict(kind="checkbox", options=[])
    assert ask("I acknowledge that the information provided is accurate.", **box) is None   # not yet given
    assert ask("I acknowledge that the information provided is accurate.", accuracy_certification="Yes", **box) is True
    assert ask("I certify that my answers are true and complete", accuracy_certification="Yes", **box) is True
    assert ask("I certify the information is accurate and consent to a background check",
               accuracy_certification="Yes", **box) is None
    assert ask("By checking this box I provide my electronic signature", accuracy_certification="Yes", **box) is None
    assert ask("I acknowledge that the information provided is accurate.", ats="greenhouse",
               accuracy_certification="Yes", **box) is None


def test_pay_range_lists_and_years_without_of():
    from jobhub.answers import Q, rule_answer
    base = settings()
    base["answers"].update(pay_range_target="100000", years_experience="2")
    base["answers"]["confirmed_fields"] += ["pay_range_target", "years_experience"]
    job = Job(source="workday", ats="workday", company="Fidelity", title="Analyst", url="https://fmr.wd1.myworkdayjobs.com/x")
    ranges = ["Select One", "$100,000-$124,999", "$125,000-$149,999", "$500,000+"]
    pick = rule_answer(Q(label="What are your total annual compensation expectations?", kind="select", options=ranges),
                       Profile(), base, job)
    assert pick.value == "$100,000-$124,999"
    high = ["$150,000-$174,999", "$175,000-$199,999"]
    assert rule_answer(Q(label="Desired salary?", kind="select", options=high), Profile(), base, job) is None
    years = rule_answer(Q(label="How many years relevant experience do you have, specific to this role?", kind="text"),
                        Profile(), base, job)
    assert years.value == "2"


def test_phone_extension_is_never_the_phone_number():
    from jobhub.answers import Q, rule_answer
    profile = Profile(phone="805-555-0100")
    job = Job(source="workday", ats="workday", company="Bank", title="Analyst", url="https://bank.wd5.myworkdayjobs.com/x")
    for label in ("Phone Extension", "Phone extension", "Ext."):
        assert rule_answer(Q(label=label, kind="text"), profile, settings(), job) is None
    assert rule_answer(Q(label="Phone Number", kind="text"), profile, settings(), job) is not None


def test_worked_with_us_before_is_the_former_employer_question():
    assert ask("Have you worked with us before?", company="CIBC") == "No"
