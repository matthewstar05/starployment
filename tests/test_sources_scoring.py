from datetime import datetime, timedelta, timezone

import pytest
import httpx

from jobhub.config import DEFAULT_SETTINGS
from jobhub.models import Job, Profile
from jobhub.scoring import score_job
from jobhub.sources import base
from jobhub.sources.base import SourceJobs, fetch_boards, parse_salary, title_relevant, title_wanted


def job(**values):
    return Job(**({'source': 'lever', 'ats': 'lever', 'company': 'Example', 'title': 'Software Engineer',
                   'location': 'San Francisco, CA', 'url': 'https://jobs.lever.co/example/id'} | values))


def test_hourly_amount_is_not_invented_annual_salary():
    assert parse_salary('Salary: $45 per hour') == (None, None)
    assert parse_salary('Salary: $120000 - $150000 per year') == (120000, 150000)
    assert parse_salary('Salary: $95k - $110k') == (95000, 110000)


def test_location_word_boundaries_and_remote_country_restriction():
    settings = DEFAULT_SETTINGS | {'preferred_locations': ['United States']}
    _, local = score_job(job(remote=True, location='Remote, United States'), Profile(), settings)
    _, foreign = score_job(job(remote=True, location='Remote, Australia'), Profile(), settings)
    assert local['location'] == 10
    assert foreign['location'] == 2
    assert foreign['auto_apply_eligible'] is False


@pytest.mark.parametrize(('preference', 'posting'), [
    ('New York', 'NYC, NY'),
    ('Los Angeles', 'LA, CA'),
    ('San Francisco', 'SF, CA'),
])
def test_target_city_aliases_match_common_posting_locations(preference, posting):
    settings = DEFAULT_SETTINGS | {'preferred_locations': [preference]}
    _, detail = score_job(job(location=posting), Profile(), settings)
    assert detail['location'] == 10


def test_discovery_does_not_masquerade_as_publication_date():
    _, unknown = score_job(job(), Profile(), DEFAULT_SETTINGS)
    _, old = score_job(job(posted_at=(datetime.now(timezone.utc)-timedelta(days=100)).isoformat()), Profile(), DEFAULT_SETTINGS)
    assert unknown['age_days'] is None
    assert unknown['freshness'] == 4
    assert old['freshness'] == 0


def test_years_old_postings_remain_browsable_but_are_not_auto_applied():
    old_date = (datetime.now(timezone.utc) - timedelta(days=181)).isoformat()
    _, detail = score_job(job(posted_at=old_date), Profile(),
                          DEFAULT_SETTINGS | {'target_titles': ['software engineer'],
                                              'preferred_locations': ['San Francisco']})
    assert not detail['auto_apply_eligible']
    assert 'Posting date is over 180 days old' in detail['review_reasons']


def test_phd_title_requires_a_doctorate_in_candidate_profile():
    settings = DEFAULT_SETTINGS | {'target_titles': ['data scientist'],
                                   'preferred_locations': ['San Francisco']}
    _, detail = score_job(job(title='Data Scientist, PhD', url='https://jobs.lever.co/example/phd'),
                          Profile(), settings)
    assert not detail['auto_apply_eligible']
    assert 'Doctorate named in title is absent from your profile' in detail['review_reasons']


def test_excluded_titles_require_review_even_if_other_attributes_match():
    settings = DEFAULT_SETTINGS | {'exclude_title_words': ['senior']}
    _, result = score_job(job(title='Senior Software Engineer'), Profile(), settings)
    assert result['auto_apply_eligible'] is False
    assert 'Senior role' in result['review_reasons']
    assert title_wanted('Software Engineer, Leadership Development', settings)


def test_numbered_midlevel_title_stays_browsable_but_needs_experience_or_explicit_target():
    settings = DEFAULT_SETTINGS | {
        'target_titles': ['software engineer'],
        'preferred_locations': ['San Francisco'],
        'years_experience': 1.0,
    }
    posting = job(title='Software Engineer II', location='San Francisco, CA')
    score, detail = score_job(posting, Profile(), settings)
    assert score >= 60
    assert detail['auto_apply_eligible'] is False
    assert 'Level 2 role requires 3+ years or an explicit level target' in detail['review_reasons']

    experienced = score_job(posting, Profile(), settings | {'years_experience': 3.0})[1]
    assert experienced['auto_apply_eligible'] is True

    explicit = score_job(posting, Profile(), settings | {'target_titles': ['software engineer II']})[1]
    assert explicit['auto_apply_eligible'] is True

    level_three = job(title='Software Engineer III', location='San Francisco, CA')
    assert score_job(level_three, Profile(), settings | {'years_experience': 3.0})[1]['auto_apply_eligible'] is False
    assert score_job(level_three, Profile(), settings | {'years_experience': 5.0})[1]['auto_apply_eligible'] is True


def test_it_support_specialization_is_browsable_but_requires_an_explicit_target_for_auto_apply():
    settings = DEFAULT_SETTINGS | {
        'target_titles': ['software engineer', 'systems engineer', 'new grad'],
        'preferred_locations': ['Los Angeles'],
    }
    posting = job(title='IT Systems Engineer - Tier 1', location='Los Angeles, CA')
    assert title_relevant(posting.title, settings)
    score, detail = score_job(posting, Profile(), settings)
    assert score >= 60 and detail['title_fit'] == 30
    assert detail['auto_apply_eligible'] is False
    assert 'IT/support specialization is not in your target titles' in detail['review_reasons']

    ordinary = job(title='Systems Engineer', location='Los Angeles, CA')
    assert score_job(ordinary, Profile(), settings)[1]['auto_apply_eligible'] is True


def test_explicit_matching_it_support_target_allows_specialized_title():
    settings = DEFAULT_SETTINGS | {
        'target_titles': ['IT Systems Engineer'],
        'preferred_locations': ['Los Angeles'],
    }
    detail = score_job(job(title='IT Systems Engineer - Tier 1', location='Los Angeles, CA'),
                       Profile(), settings)[1]
    assert detail['auto_apply_eligible'] is True

    # An unrelated IT target cannot authorize the systems-engineering role.
    settings['target_titles'] = ['Systems Engineer', 'IT Support Specialist']
    detail = score_job(job(title='IT Systems Engineer - Tier 1', location='Los Angeles, CA'),
                       Profile(), settings)[1]
    assert detail['auto_apply_eligible'] is False


@pytest.mark.parametrize('title', [
    'Technical Support Engineer',
    'Application Support Engineer',
    'Help Desk Technician',
    'IT Infrastructure Analyst',
])
def test_unrequested_support_families_need_review(title):
    settings = DEFAULT_SETTINGS | {
        'target_titles': ['software engineer', 'systems engineer'],
        'preferred_locations': ['Los Angeles'],
    }
    detail = score_job(job(title=title, location='Los Angeles, CA'), Profile(), settings)[1]
    assert 'IT/support specialization is not in your target titles' in detail['review_reasons']


def test_software_engineering_on_it_products_is_not_misclassified_as_support():
    settings = DEFAULT_SETTINGS | {
        'target_titles': ['software engineer'],
        'preferred_locations': ['Los Angeles'],
    }
    detail = score_job(job(title='Software Engineer, IT Applications', location='Los Angeles, CA'),
                       Profile(), settings)[1]
    assert detail['auto_apply_eligible'] is True


def test_career_stage_target_alone_never_authorizes_unrelated_auto_application():
    settings = DEFAULT_SETTINGS | {
        'target_titles': ['new grad', 'software engineer'],
        'preferred_locations': ['Los Angeles'],
    }
    posting = job(title='New Grad Marketing Associate', location='Los Angeles, CA')
    assert title_relevant(posting.title, settings)
    detail = score_job(posting, Profile(), settings)[1]
    assert detail['auto_apply_eligible'] is False
    assert 'Weak match to your target titles' in detail['review_reasons']
    assert score_job(job(title='Software Engineer, New Grad', location='Los Angeles, CA'),
                     Profile(), settings)[1]['auto_apply_eligible'] is True


def test_partial_boards_preserve_results_and_failures():
    def fetch(slug, settings):
        if slug == 'bad': raise RuntimeError('HTTP 429')
        return [job()]
    result = fetch_boards(['good','bad','good'],fetch,{})
    assert isinstance(result,SourceJobs)
    assert len(result) == 1
    assert 'HTTP 429' in result.warnings[0]
    with pytest.raises(RuntimeError,match='All company boards failed'):
        fetch_boards(['bad'],fetch,{})


def test_missing_bundled_board_is_informational_and_retried_after_cooldown(monkeypatch):
    base._missing_default_boards.clear()
    now = [100.0]
    monkeypatch.setattr(base.time, 'monotonic', lambda: now[0])
    seen = []

    def fetch(slug, settings):
        seen.append(slug)
        if slug == 'stripe':
            request = httpx.Request('GET', 'https://boards-api.greenhouse.io/v1/boards/stripe/jobs')
            raise httpx.HTTPStatusError('Not Found', request=request, response=httpx.Response(404, request=request))
        return [job()]

    first = fetch_boards(['stripe', 'my-company'], fetch, {}, source='greenhouse')
    assert len(first) == 1 and first.warnings == []
    assert 'stripe' in first.notices[0]
    assert seen == ['stripe', 'my-company']

    second = fetch_boards(['stripe', 'my-company'], fetch, {}, source='greenhouse')
    assert len(second) == 1 and second.notices == ['1 bundled board(s) in a 24-hour retry window']
    assert seen == ['stripe', 'my-company', 'my-company']

    now[0] += base._MISSING_BOARD_RETRY_SECONDS + 1
    third = fetch_boards(['stripe', 'my-company'], fetch, {}, source='greenhouse')
    assert len(third) == 1 and 'stripe' in third.notices[0]
    assert seen.count('stripe') == 2
    base._missing_default_boards.clear()


def test_custom_missing_board_still_warns_and_transient_failure_is_not_cached():
    base._missing_default_boards.clear()
    seen = []

    def fetch(slug, settings):
        seen.append(slug)
        status = 404 if slug == 'my-company' else 429
        if slug in {'my-company', 'stripe'}:
            request = httpx.Request('GET', f'https://boards-api.greenhouse.io/v1/boards/{slug}/jobs')
            raise httpx.HTTPStatusError('failed', request=request, response=httpx.Response(status, request=request))
        return [job()]

    for _ in range(2):
        result = fetch_boards(['my-company', 'stripe', 'healthy'], fetch, {}, source='greenhouse')
        assert len(result) == 1
        assert len(result.warnings) == 2
        assert result.notices == []
    assert seen.count('my-company') == 2
    assert seen.count('stripe') == 2


def test_escaped_greenhouse_content_is_plain_text():
    from jobhub.sources.base import strip_html
    assert strip_html('&lt;p&gt;C++ &amp;amp; Python&lt;/p&gt;') == 'C++ & Python'
