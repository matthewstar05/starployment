"""Tests for location classification, filtering, and scoring."""
from __future__ import annotations

import pytest

from jobhub.config import DEFAULT_SETTINGS
from jobhub.locations import (
    is_approved_international,
    is_disallowed_location,
    is_favorite_metro,
    is_location_acceptable,
    is_us_location,
)
from jobhub.models import Job, Profile
from jobhub.scoring import score_job


def _make_job(location: str, remote: bool = False, **kwargs) -> Job:
    return Job(
        source="test",
        company="Acme Corp",
        title="Software Engineer",
        location=location,
        remote=remote,
        url=f"https://example.com/jobs/{abs(hash(location)) % 1000000}",
        description="Python software engineer entry level",
        **kwargs,
    )


@pytest.mark.parametrize("location", [
    "New York, NY",
    "NYC",
    "New York City",
    "Manhattan, NY",
    "Brooklyn, NY",
    "Jersey City, NJ",
    "Chicago, IL",
    "Chicagoland",
    "Evanston, IL",
    "Los Angeles, CA",
    "LA, CA",
    "LA",
    "Santa Monica, CA",
    "Culver City, CA",
    "Irvine, CA",
    "Costa Mesa, California, United States",
    "San Francisco, CA",
    "SF",
    "SF, CA",
    "Bay Area",
    "Silicon Valley",
    "San Jose, CA",
    "Palo Alto, CA",
    "Mountain View, CA",
    "Sunnyvale, CA",
    "Santa Clara, CA",
    "Menlo Park, CA",
])
def test_favorite_city_locations_receive_top_score(location):
    settings = DEFAULT_SETTINGS | {
        "target_titles": ["Software Engineer"],
        "preferred_locations": ["New York", "Chicago", "Seattle", "Los Angeles", "San Francisco"],
    }
    score, details = score_job(_make_job(location), Profile(), settings)
    assert details["location"] == 10.0
    assert "Location outside your preferences" not in details["review_reasons"]
    assert "Location outside US and approved metropolitan hubs" not in details["review_reasons"]
    assert details["auto_apply_eligible"] is True
    assert is_location_acceptable(location) is True


def test_preferred_non_favorite_city_receives_second_tier_score():
    settings = DEFAULT_SETTINGS | {
        "target_titles": ["Software Engineer"],
        "preferred_locations": ["Seattle", "Austin"],
    }
    # Seattle is preferred, but not one of the 4 favorite cities
    score, details = score_job(_make_job("Seattle, WA"), Profile(), settings)
    assert details["location"] == 8.0
    assert "Location outside your preferences" not in details["review_reasons"]
    assert details["auto_apply_eligible"] is True

    # Austin is preferred, not favorite
    score, details = score_job(_make_job("Austin, TX"), Profile(), settings)
    assert details["location"] == 8.0
    assert "Location outside your preferences" not in details["review_reasons"]


def test_non_preferred_domestic_us_location_receives_neutral_score():
    settings = DEFAULT_SETTINGS | {
        "preferred_locations": ["New York", "Chicago"],
    }
    score, details = score_job(_make_job("Denver, CO"), Profile(), settings)
    assert details["location"] == 5.0
    assert "Location outside your preferences" in details["review_reasons"]
    assert is_location_acceptable("Denver, CO") is True


def test_united_states_in_places_prefers_every_us_location():
    settings = DEFAULT_SETTINGS | {"preferred_locations": ["New York", "United States"]}
    for location in ("Denver, CO", "Hawthorne, CA", "United States", "Boston, Massachusetts, United States"):
        score, details = score_job(_make_job(location), Profile(), settings)
        assert details["location"] == 8.0, location
        assert "Location outside your preferences" not in details["review_reasons"]
    score, details = score_job(_make_job("New York, NY"), Profile(), settings)
    assert details["location"] == 10.0
    score, details = score_job(_make_job("London, UK"), Profile(), settings)
    assert "Location outside your preferences" in details["review_reasons"]


@pytest.mark.parametrize("location", [
    "London, UK",
    "London, England, United Kingdom",
    "Toronto, ON, Canada",
    "Vancouver, BC, Canada",
    "Montreal, Canada",
    "Dublin, Ireland",
    "Amsterdam, Netherlands",
    "Berlin, Germany",
    "Munich, Germany",
    "Paris, France",
    "Zurich, Switzerland",
    "Zürich, CH",
    "Stockholm, Sweden",
    "Sydney, Australia",
    "Melbourne, Australia",
    "Singapore",
    "Tokyo, Japan",
    "Hong Kong",
])
def test_approved_international_metros_are_acceptable(location):
    assert is_approved_international(location) is True
    assert is_location_acceptable(location) is True

    settings = DEFAULT_SETTINGS | {
        "preferred_locations": ["New York", "Chicago", "Seattle"],
    }
    score, details = score_job(_make_job(location), Profile(), settings)
    # When not in preferred list, gets acceptable foreign score (2.0)
    assert details["location"] == 2.0
    assert "Location outside your preferences" in details["review_reasons"]
    assert "Location outside US and approved metropolitan hubs" not in details["review_reasons"]


def test_approved_international_metro_explicitly_preferred():
    settings = DEFAULT_SETTINGS | {
        "preferred_locations": ["London", "Toronto"],
    }
    score, details = score_job(_make_job("London, UK"), Profile(), settings)
    assert details["location"] == 8.0
    assert "Location outside your preferences" not in details["review_reasons"]


@pytest.mark.parametrize("location", [
    "Bengaluru, India",
    "Bangalore, India",
    "Mumbai, India",
    "Hyderabad, India",
    "India - Hyderabad",
    "Gurugram",
    "Gurgaon, India",
    "Noida, India",
    "Pune, India",
    "Dubai, United Arab Emirates",
    "Dubai, UAE",
    "Abu Dhabi, United Arab Emirates",
    "Remote - India",
    "São Paulo, Brazil",
    "Buenos Aires, Argentina",
    "Mexico City",
    "Warsaw, Poland",
    "Bucharest, Romania",
    "Bishkek, Kyrgyzstan",
    "Beijing, China",
    "Shanghai, China",
    "Kuala Lumpur, Malaysia",
    "Lagos, Nigeria",
])
def test_disallowed_foreign_locations_are_rejected_and_ineligible(location):
    assert is_disallowed_location(location) is True
    assert is_location_acceptable(location) is False

    settings = DEFAULT_SETTINGS | {
        "preferred_locations": ["New York", "Chicago", "Los Angeles", "San Francisco"],
    }
    score, details = score_job(_make_job(location), Profile(), settings)
    assert details["location"] == 0.0
    assert "Location outside US and approved metropolitan hubs" in details["review_reasons"]
    assert details["auto_apply_eligible"] is False


def test_multi_location_with_eligible_us_and_disallowed_options():
    # If job has both an eligible location and an ineligible foreign office,
    # the candidate can choose the US or approved location
    mixed_us = "Charlotte, NC; Cologne, Germany; Bengaluru, Karnataka, India"
    assert is_location_acceptable(mixed_us) is True

    settings = DEFAULT_SETTINGS | {
        "preferred_locations": ["New York", "Chicago", "San Francisco"],
    }
    score, details = score_job(_make_job(mixed_us), Profile(), settings)
    # Charlotte, NC is US
    assert details["location"] == 5.0
    assert "Location outside US and approved metropolitan hubs" not in details["review_reasons"]


def test_multi_location_with_favorite_city():
    multi_fav = "San Francisco, CA; Bengaluru, India; London, UK"
    assert is_location_acceptable(multi_fav) is True

    settings = DEFAULT_SETTINGS | {
        "target_titles": ["Software Engineer"],
        "preferred_locations": ["San Francisco"],
    }
    score, details = score_job(_make_job(multi_fav), Profile(), settings)
    assert details["location"] == 10.0
    assert details["auto_apply_eligible"] is True


def test_multi_location_with_approved_international():
    multi_intl = "Dubai, London, Paris, Hong Kong, Sydney, Toronto, Tokyo"
    assert is_location_acceptable(multi_intl) is True

    settings = DEFAULT_SETTINGS | {
        "preferred_locations": ["San Francisco"],
    }
    score, details = score_job(_make_job(multi_intl), Profile(), settings)
    assert details["location"] == 2.0
    assert "Location outside US and approved metropolitan hubs" not in details["review_reasons"]


def test_indiana_and_indianapolis_not_confused_with_india():
    assert is_us_location("Indianapolis, IN") is True
    assert is_disallowed_location("Indianapolis, IN") is False
    assert is_location_acceptable("Indianapolis, IN") is True

    assert is_us_location("Bloomington, Indiana") is True
    assert is_disallowed_location("Bloomington, Indiana") is False
    assert is_location_acceptable("Bloomington, Indiana") is True


def test_new_mexico_not_confused_with_mexico():
    assert is_us_location("Albuquerque, New Mexico") is True
    assert is_disallowed_location("Albuquerque, New Mexico") is False
    assert is_location_acceptable("Albuquerque, New Mexico") is True


def test_king_of_prussia_not_confused_with_russia():
    assert is_us_location("King of Prussia, PA") is True
    assert is_disallowed_location("King of Prussia, PA") is False
    assert is_location_acceptable("King of Prussia, PA") is True


@pytest.mark.parametrize("location", [
    "La Paz, Bolivia",
    "Venice, Italy",
    "Tbilisi, Georgia",
    "Georgia country",
])
def test_foreign_context_prevents_us_city_state_and_favorite_matches(location):
    assert is_us_location(location) is False
    assert is_favorite_metro(location) is False
    assert is_approved_international(location) is False
    assert is_disallowed_location(location) is True
    assert is_location_acceptable(location) is False

    _, details = score_job(_make_job(location), Profile(), DEFAULT_SETTINGS)
    assert details["location"] == 0.0
    assert details["auto_apply_eligible"] is False


@pytest.mark.parametrize("location", ["Richmond, Canada", "Cambridge, UK"])
def test_approved_foreign_hub_context_is_not_mistaken_for_us(location):
    assert is_us_location(location) is False
    assert is_approved_international(location) is True
    assert is_disallowed_location(location) is False
    assert is_location_acceptable(location) is True


@pytest.mark.parametrize("location", [
    "Paris, India",
    "London, Italy",
    "Toronto, India",
    "Tokyo, Germany",
])
def test_approved_city_name_does_not_override_mismatched_country(location):
    assert is_approved_international(location) is False
    assert is_disallowed_location(location) is True
    assert is_location_acceptable(location) is False


@pytest.mark.parametrize("location", [
    "Paris, France",
    "London, UK",
    "Amsterdam, Netherlands",
    "Tokyo, Japan",
])
def test_approved_city_country_pairs_remain_acceptable(location):
    assert is_approved_international(location) is True
    assert is_location_acceptable(location) is True


def test_us_alternative_keeps_multilocation_posting_acceptable():
    assert is_location_acceptable("Paris, India; Chicago, IL") is True


def test_ambiguous_us_city_names_yield_to_country_context():
    assert is_us_location("Richmond, UK") is False
    assert is_approved_international("Richmond, UK") is False
    assert is_location_acceptable("Richmond, UK") is False
    # "New England" is a US region, not the country label England.
    assert is_us_location("Boston, New England") is True


def test_ambiguous_favorite_venice_requires_us_context():
    assert is_favorite_metro("Venice, Italy") is False
    assert is_favorite_metro("Venice, CA") is True
    assert is_us_location("Venice, CA") is True


def test_georgia_state_requires_positive_us_context():
    assert is_us_location("Georgia") is False
    assert is_us_location("Atlanta, Georgia") is True
    assert is_us_location("Atlanta, GA") is True


@pytest.mark.parametrize("location", [
    "Canada", "Remote, UK", "Japan", "France", "Rural Australia", "Remote - Spain",
    "Remote - Poland", "Spain (Hybrid)",
])
def test_country_names_and_remote_labels_do_not_allow_unapproved_locations(location):
    assert is_location_acceptable(location) is False
    _, details = score_job(_make_job(location, remote=True), Profile(),
                           DEFAULT_SETTINGS | {"preferred_locations": [location]})
    assert details["location"] == 0.0
    assert details["auto_apply_eligible"] is False
