import sys
from types import SimpleNamespace

from jobhub.sources import jobspy_source


class EmptyResults:
    def __len__(self):
        return 0


def test_jobspy_defaults_queries_saved_titles_near_preferred_location(monkeypatch):
    calls = []

    def scrape_jobs(**kwargs):
        calls.append(kwargs)
        return EmptyResults()

    monkeypatch.setitem(sys.modules, "jobspy", SimpleNamespace(scrape_jobs=scrape_jobs))
    settings = {
        "target_titles": ["Data Analyst", "Software Engineer", "data analyst"],
        "preferred_locations": ["Los Angeles, CA", "San Francisco, CA"],
        "jobspy": {"search_terms": [], "location": ""},
    }

    assert jobspy_source.fetch(settings) == []
    assert [(call["search_term"], call["location"]) for call in calls] == [
        ("Data Analyst", "Los Angeles, CA"), ("Data Analyst", "San Francisco, CA"),
        ("Software Engineer", "Los Angeles, CA"), ("Software Engineer", "San Francisco, CA")]
    assert all(f"in {call['location']}" in call["google_search_term"] for call in calls)


def test_jobspy_explicit_queries_and_location_override_fallbacks(monkeypatch):
    calls = []

    def scrape_jobs(**kwargs):
        calls.append(kwargs)
        return EmptyResults()

    monkeypatch.setitem(sys.modules, "jobspy", SimpleNamespace(scrape_jobs=scrape_jobs))
    settings = {
        "target_titles": ["Ignored Target"],
        "preferred_locations": ["Los Angeles, CA"],
        "jobspy": {"search_terms": ["Platform Engineer"], "location": "San Diego, CA"},
    }

    assert jobspy_source.fetch(settings) == []
    assert len(calls) == 1
    assert calls[0]["search_term"] == "Platform Engineer"
    assert calls[0]["location"] == "San Diego, CA"
    assert "in San Diego, CA" in calls[0]["google_search_term"]


def test_jobspy_uses_nationwide_fallback_without_local_preference():
    assert jobspy_source._search_locations({}, {"preferred_locations": ["Remote", "United States"]}) == ["United States"]


def test_jobspy_rotates_bounded_searches_across_all_target_cities():
    settings = {"target_titles": ["Software Engineer", "Data Engineer", "Data Analyst"],
                "preferred_locations": ["NYC", "Chicago", "Seattle", "Los Angeles", "San Francisco"],
                "jobspy": {"search_terms": [], "location": ""}}
    first = jobspy_source._search_plan(settings["jobspy"], settings, hour=0)
    second = jobspy_source._search_plan(settings["jobspy"], settings, hour=1)
    assert len(first) == len(second) == jobspy_source.MAX_SEARCHES_PER_SCAN
    assert len(set(first + second)) == 15
    assert {location for _, location in first + second} == set(settings["preferred_locations"])


def test_explicit_location_stays_single_location_in_rotating_plan():
    settings = {"target_titles": [f"Role {index}" for index in range(12)],
                "preferred_locations": ["NYC", "Chicago"],
                "jobspy": {"search_terms": [], "location": "Boston"}}
    plan = jobspy_source._search_plan(settings["jobspy"], settings, hour=0)
    assert len(plan) == jobspy_source.MAX_SEARCHES_PER_SCAN
    assert {location for _, location in plan} == {"Boston"}
