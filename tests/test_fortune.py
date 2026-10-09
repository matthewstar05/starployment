"""Unit tests for jobhub.fortune (Fortune 1000 and Fortune 500 mapping)."""
from __future__ import annotations

import pytest

from jobhub.fortune import fortune_rank, is_fortune_500, is_fortune_1000


@pytest.mark.parametrize(
    ("company", "expected_rank"),
    [
        ("Walmart", 1),
        ("Exxon Mobil", 2),
        ("Chevron", 3),
        ("Berkshire Hathaway", 4),
        ("Apple", 5),
        ("Apple Inc.", 5),
        ("Apple Computer, Inc.", 5),
        ("General Motors", 6),
        ("GM", 6),
        ("Ford Motor", 9),
        ("AT&T", 12),
        ("ATT", 12),
        ("Costco", 18),
        ("Costco Wholesale", 18),
        ("IBM", 24),
        ("International Business Machines", 24),
        ("Amazon", 29),
        ("Amazon.com", 29),
        ("Amazon.com, Inc.", 29),
        ("Microsoft", 31),
        ("Microsoft Corporation", 31),
        ("Alphabet", 40),
        ("Google", 40),
        ("Google LLC", 40),
        ("Coca-Cola", 63),
        ("PepsiCo", 44),
        ("Pepsi", 44),
        ("Meta", 242),
        ("Meta Platforms", 242),
        ("Facebook", 242),
        ("Netflix", 474),
        ("Netflix, Inc.", 474),
        # Modern Fortune 500 tech additions and updates
        ("Tesla", 65),
        ("Tesla Motors", 65),
        ("Tesla Motors, Inc.", 65),
        ("Nvidia", 65),
        ("PayPal", 148),
        ("Uber", 162),
        ("Uber Technologies", 162),
        ("Adobe", 198),
        ("Block", 210),
        ("Square", 210),
        ("ServiceNow", 380),
        ("Airbnb", 450),
        ("DoorDash", 484),
        ("DoorDash USA", 484),
        ("Workday", 490),
        # Subsidiaries
        ("Twitch", 29),
        ("Zoox", 29),
        ("AWS", 29),
        ("Amazon Web Services", 29),
        ("DeepMind", 40),
        ("Google DeepMind", 40),
        ("YouTube", 40),
        ("Waymo", 40),
        ("Instagram", 242),
        ("WhatsApp", 242),
        ("LinkedIn", 31),
        ("GitHub", 31),
    ],
)
def test_fortune_500_companies(company: str, expected_rank: int):
    rank = fortune_rank(company)
    assert rank == expected_rank
    assert is_fortune_500(company) is True
    assert is_fortune_1000(company) is True


@pytest.mark.parametrize(
    ("company", "expected_rank"),
    [
        ("Palantir", 620),
        ("Palantir Technologies", 620),
        ("Snowflake", 650),
        ("Pinterest", 680),
        ("Roblox", 720),
        ("Okta", 750),
        ("Veeva", 780),
        ("Veeva Systems", 780),
        ("Datadog", 820),
        ("MongoDB", 840),
        ("Cloudflare", 860),
        ("Dropbox", 880),
        ("Robinhood", 890),
        ("Robinhood Markets", 890),
        ("Virtu Financial", 895),
        ("Williams-Sonoma", 551),
        ("Synopsys", 995),
        ("E*Trade Financial", 1000),
        ("E-Trade", 1000),
        ("Regency Energy Partners", 523),
        ("SCANA", 523),
    ],
)
def test_fortune_1000_non_500_companies(company: str, expected_rank: int):
    rank = fortune_rank(company)
    assert rank == expected_rank
    assert is_fortune_500(company) is False
    assert is_fortune_1000(company) is True


@pytest.mark.parametrize(
    "company",
    [
        ("Stripe"),
        ("Figma"),
        ("Databricks"),
        ("OpenAI"),
        ("Anthropic"),
        ("Scale AI"),
        ("Brex"),
        ("Ramp"),
        ("Plaid"),
        ("Lyft"),
        ("Reddit"),
        ("Nonexistent Unknown Company LLC"),
        (""),
        ("   "),
    ],
)
def test_non_fortune_companies(company: str):
    rank = fortune_rank(company)
    assert rank is None
    assert is_fortune_500(company) is False
    assert is_fortune_1000(company) is False
