"""Location parsing, classification, and scoring for Starployment.

Rules:
1. Domestic US locations are primary.
2. The only permitted international locations are approved metropolitan hubs
   chosen as location preferences (e.g. Canada, UK, Ireland, Australia,
   Western/Northern Europe, Singapore, Tokyo, Hong Kong).
   Disallowed foreign regions (Dubai, India, etc.) receive 0 location points,
   are marked with a review reason, and are not auto-apply eligible.
3. Favorite cities (NYC, Chicago, LA, SF) and their metropolitan areas receive
   top-tier score weighting (10.0 points).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Sequence

# ----------------- Metros and Regions -----------------

FAVORITE_METROS: dict[str, set[str]] = {
    "nyc": {
        "new york", "nyc", "new york city", "manhattan", "brooklyn", "queens", "bronx",
        "staten island", "long island city", "jersey city", "hoboken",
    },
    "chicago": {
        "chicago", "chicagoland", "evanston", "naperville", "schaumburg",
    },
    "la": {
        "los angeles", "la, ca", "la ca", "greater los angeles", "santa monica",
        "culver city", "venice", "playa vista", "marina del rey", "el segundo",
        "pasadena", "burbank", "glendale", "long beach", "irvine", "costa mesa",
        "newport beach", "santa ana", "beverly hills", "west hollywood", "orange county",
    },
    "sf": {
        "san francisco", "sf", "sf, ca", "sf ca", "san francisco bay area", "bay area",
        "silicon valley", "san jose", "palo alto", "mountain view", "sunnyvale",
        "santa clara", "menlo park", "redwood city", "cupertino", "foster city",
        "san mateo", "oakland", "berkeley", "fremont", "south san francisco",
        "san bruno", "burlingame", "millbrae", "los altos", "campbell", "milpitas",
        "pleasanton", "san ramon", "walnut creek", "emeryville", "alameda",
    },
}

MAJOR_US_CITIES: set[str] = {
    "seattle", "boston", "austin", "philadelphia", "denver", "atlanta", "dallas",
    "houston", "miami", "san diego", "portland", "pittsburgh", "cincinnati",
    "charlotte", "minneapolis", "detroit", "salt lake city", "raleigh", "tampa",
    "phoenix", "nashville", "baltimore", "indianapolis", "columbus", "cleveland",
    "las vegas", "kansas city", "sacramento", "san antonio", "st. louis", "milwaukee",
    "richmond", "orlando", "tucson", "honolulu", "albuquerque", "omaha", "louisville",
    "providence", "hartford", "new haven", "boulder", "fort collins", "colorado springs",
    "ann arbor", "madison", "arlington", "alexandria", "reston", "mclean", "tysons",
}

US_STATES: set[str] = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming", "district of columbia", "puerto rico",
}

US_STATE_CODES: set[str] = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV",
    "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN",
    "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC", "PR",
}

# Candidate-approved international metropolitan hubs. This is a location
# preference filter and says nothing about work authorization.
APPROVED_INTERNATIONAL_METROS: set[str] = {
    # Canada
    "toronto", "vancouver", "montreal", "ottawa", "calgary", "waterloo", "kitchener",
    "edmonton", "victoria", "burnaby", "richmond", "mississauga", # United Kingdom
    "london", "manchester", "edinburgh", "cambridge", "oxford", "bristol", "birmingham",
    "glasgow", "leeds", "belfast", "newcastle", "cardiff", "sheffield", "nottingham",
    # Ireland
    "dublin", "cork", "galway", "limerick", # Australia and New Zealand
    "sydney", "melbourne", "brisbane", "perth", "adelaide", "canberra", "auckland", "wellington",
    "christchurch", # Western, Northern, and Central Europe
    "amsterdam", "rotterdam", "the hague", "utrecht", "eindhoven", "berlin", "munich", "frankfurt", "hamburg", "cologne", "stuttgart", "dusseldorf", "paris", "zurich", "zürich", "geneva", "lausanne", "basel", "zug", "stockholm", "gothenburg", "copenhagen", "oslo", "helsinki", "brussels", "vienna", # Developed Asia (Global finance / tech hubs with English business environments)
    "singapore", "tokyo", "hong kong",
}

# A country label only approves a metro in that metro's country. City-only
# postings remain supported when they have no explicit country context.
APPROVED_METRO_COUNTRIES: dict[str, set[str]] = {
    "canada": {"toronto", "vancouver", "montreal", "ottawa", "calgary", "waterloo", "kitchener", "edmonton", "victoria", "burnaby", "richmond", "mississauga"},
    "united kingdom": {"london", "manchester", "edinburgh", "cambridge", "oxford", "bristol", "birmingham", "glasgow", "leeds", "belfast", "newcastle", "cardiff", "sheffield", "nottingham"},
    "ireland": {"dublin", "cork", "galway", "limerick"},
    "australia": {"sydney", "melbourne", "brisbane", "perth", "adelaide", "canberra"},
    "new zealand": {"auckland", "wellington", "christchurch"},
    "netherlands": {"amsterdam", "rotterdam", "the hague", "utrecht", "eindhoven"},
    "germany": {"berlin", "munich", "frankfurt", "hamburg", "cologne", "stuttgart", "dusseldorf"},
    "france": {"paris"},
    "switzerland": {"zurich", "zürich", "geneva", "lausanne", "basel", "zug"},
    "sweden": {"stockholm", "gothenburg"},
    "denmark": {"copenhagen"},
    "norway": {"oslo"},
    "finland": {"helsinki"},
    "belgium": {"brussels"},
    "austria": {"vienna"},
    "singapore": {"singapore"},
    "japan": {"tokyo"},
    "hong kong": {"hong kong"},
}

FOREIGN_COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "canada": ("canada",),
    "united kingdom": ("united kingdom", "great britain", "uk", "england", "scotland", "wales", "northern ireland"),
    "ireland": ("ireland",), "australia": ("australia",), "new zealand": ("new zealand",),
    "netherlands": ("netherlands",), "germany": ("germany",), "france": ("france",),
    "switzerland": ("switzerland",), "sweden": ("sweden",), "norway": ("norway",),
    "finland": ("finland",), "belgium": ("belgium",), "austria": ("austria",),
    "denmark": ("denmark",), "japan": ("japan",), "singapore": ("singapore",),
    "china": ("china",), "hong kong": ("hong kong",), "india": ("india",),
    "brazil": ("brazil", "brasil"), "argentina": ("argentina",), "colombia": ("colombia",),
    "bolivia": ("bolivia",), "chile": ("chile",), "peru": ("peru",),
    "mexico": ("mexico",), "spain": ("spain",), "italy": ("italy",), "portugal": ("portugal",),
    "poland": ("poland",), "romania": ("romania",), "serbia": ("serbia",),
    "hungary": ("hungary",), "ukraine": ("ukraine",), "russia": ("russia",),
    "israel": ("israel",), "south africa": ("south africa",), "nigeria": ("nigeria",),
    "kenya": ("kenya",), "united arab emirates": ("united arab emirates", "uae"),
    "qatar": ("qatar",), "saudi arabia": ("saudi arabia",), "turkey": ("turkey",),
    "thailand": ("thailand",), "vietnam": ("vietnam",), "philippines": ("philippines",),
    "indonesia": ("indonesia",), "malaysia": ("malaysia",), "pakistan": ("pakistan",),
    "bangladesh": ("bangladesh",), "sri lanka": ("sri lanka",), "egypt": ("egypt",),
    "morocco": ("morocco",), "ghana": ("ghana",), "czechia": ("czech republic", "czechia"),
    "south korea": ("south korea",), "taiwan": ("taiwan",),
}

FOREIGN_COUNTRY_CONTEXT_RE = re.compile(
    r"\b(" + "|".join(
        (r"(?<!new )mexico" if alias == "mexico" else
         r"(?<!new )england" if alias == "england" else re.escape(alias))
        for aliases in FOREIGN_COUNTRY_ALIASES.values()
        for alias in sorted(aliases, key=len, reverse=True)
    ) + r")\b",
    re.I,
)

# Country names in a location string take precedence over ambiguous city names
# and two-letter US state codes. This list intentionally covers both approved
# and unapproved destinations, since approval requires an approved metro too.
FOREIGN_COUNTRY_RE = re.compile(
    r"\b(canada|united kingdom|great britain|uk|(?<!new )england|scotland|wales|northern ireland|"
    r"ireland|australia|new zealand|netherlands|germany|france|switzerland|sweden|"
    r"norway|finland|belgium|austria|denmark|japan|singapore|china|hong kong|india|"
    r"brazil|brasil|argentina|colombia|bolivia|chile|peru|(?<!new )mexico|spain|italy|portugal|"
    r"poland|romania|serbia|hungary|ukraine|russia|israel|south africa|nigeria|kenya|"
    r"united arab emirates|uae|qatar|saudi arabia|turkey|thailand|vietnam|philippines|"
    r"indonesia|malaysia|pakistan|bangladesh|sri lanka|egypt|morocco|ghana)\b",
    re.I,
)

DISALLOWED_PATTERNS: list[re.Pattern] = [
    # India and Indian tech centers
    re.compile(r"\b(india|bharat|bengaluru|bangalore|hyderabad|pune|mumbai|bombay|gurugram|gurgaon|noida|chennai|madras|delhi|new delhi|kolkata|ahmedabad|mohali|jaipur|kochi|indore|coimbatore)\b", re.I),
    # UAE and Middle East
    re.compile(r"\b(dubai|abu dhabi|uae|united arab emirates|sharjah|qatar|doha|saudi arabia|riyadh|jeddah|kuwait|bahrain|egypt|cairo|jordan|amman|lebanon|beirut|iraq)\b", re.I),
    # Arabic / mojibake for Dubai
    re.compile(r"[\u0600-\u06FF]|Ø¯Ø¨Ù", re.I),
    # Mainland China
    re.compile(r"\b(china|beijing|shanghai|shenzhen|guangzhou|dalian|hangzhou|chengdu|wuhan)\b", re.I),
    # Non-approved South & Southeast Asia
    re.compile(r"\b(pakistan|karachi|lahore|bangladesh|dhaka|sri lanka|philippines|manila|vietnam|hanoi|saigon|thailand|bangkok|indonesia|jakarta|malaysia|kuala lumpur)\b", re.I),
    # Latin America (negative lookbehind ensures 'New Mexico' is not matched)
    re.compile(r"\b(brazil|brasil|são paulo|sao paulo|sao paolo|argentina|buenos aires|colombia|bogota|chile|santiago|peru|lima|costa rica|uruguay|venezuela)\b", re.I),
    re.compile(r"(?<!new\s)\bmexico\b|mexico city", re.I),
    # Eastern Europe, Central Asia, Russia (negative lookbehind ensures 'King of Prussia' is not matched)
    re.compile(r"(?<!p)\brussia\b|\b(moscow|belarus|ukraine|kyiv|poland|warsaw|krakow|romania|bucharest|serbia|belgrade|hungary|budapest|kyrgyzstan|bishkek|kazakhstan|lithuania|vilnius|croatia|zagreb|israel|tel aviv)\b", re.I),
    # Africa
    re.compile(r"\b(nigeria|lagos|kenya|nairobi|south africa|johannesburg|cape town|ghana|accra)\b", re.I),
]

US_COUNTRY_RE = re.compile(
    r"\b(united states|usa|u\.s\.a?|u\.s\b|remote in usa|remote - us|us remote|us-based|us only|remote, us|us)\b",
    re.I,
)


@dataclass(slots=True, frozen=True)
class LocationResult:
    is_favorite: bool
    is_preferred: bool
    is_us: bool
    is_approved_intl: bool
    is_disallowed: bool
    is_remote: bool
    score: float
    review_reason: str | None = None


def _is_bare_or_word(term: str, text: str) -> bool:
    return bool(re.search(rf"(?<![a-zA-Z0-9]){re.escape(term)}(?![a-zA-Z0-9])", text, re.I))


def _foreign_country_contexts(text: str) -> set[str]:
    contexts = set()
    for country, aliases in FOREIGN_COUNTRY_ALIASES.items():
        for alias in aliases:
            if _is_bare_or_word(alias, text):
                if alias in {"mexico", "england"} and re.search(rf"\bnew\s+{re.escape(alias)}\b", text, re.I):
                    continue
                contexts.add(country)
                break
    if _has_foreign_country_marker(text) and re.search(r"\bgeorgia\b", text, re.I):
        contexts.add("georgia")
    return contexts


def _has_foreign_country_marker(text: str) -> bool:
    if FOREIGN_COUNTRY_RE.search(text) or FOREIGN_COUNTRY_CONTEXT_RE.search(text):
        return True
    # Georgia is both a US state and a country. Require positive US context
    # before interpreting the state name as domestic.
    if re.search(r"\bgeorgia\b", text, re.I):
        if re.search(r"\b(tbilisi|batumi|caucasus|country|republic)\b", text, re.I):
            return True
        if text.strip().casefold() == "georgia":
            return True
        if re.search(r",\s*georgia\s*$", text, re.I):
            city_part = re.sub(r",\s*georgia\s*$", "", text, flags=re.I).casefold()
            georgia_us_cities = {
                "atlanta", "savannah", "augusta", "athens", "columbus", "macon",
                "roswell", "alpharetta", "marietta", "sandy springs",
            }
            if not any(_is_bare_or_word(city, city_part) for city in georgia_us_cities):
                return True
    return False


METRO_KEY_ALIASES: dict[str, str] = {
    "nyc": "nyc",
    "new york": "nyc",
    "new york city": "nyc",
    "manhattan": "nyc",
    "brooklyn": "nyc",
    "chicago": "chicago",
    "chicagoland": "chicago",
    "la": "la",
    "los angeles": "la",
    "sf": "sf",
    "san francisco": "sf",
    "bay area": "sf",
    "silicon valley": "sf",
}


def is_favorite_metro(loc: str, favorite_keys: Sequence[str] | None = None) -> bool:
    """Check if location matches one of the top favorite metros (NYC, Chicago, LA, SF)."""
    text = loc.casefold().strip()
    if not text:
        return False
    if _has_foreign_country_marker(text):
        return False
    # Quick acronym checks
    if re.search(r"\b(nyc|sf|la)\b", text):
        return True
    active_keys = favorite_keys or ("nyc", "chicago", "la", "sf")
    for key in active_keys:
        k_clean = key.casefold().strip()
        canonical = METRO_KEY_ALIASES.get(k_clean, k_clean)
        terms = FAVORITE_METROS.get(canonical, {k_clean})
        for t in terms:
            if _is_bare_or_word(t, text):
                return True
    return False


def is_us_location(loc: str) -> bool:
    """Check if a location string points to a domestic United States location."""
    raw = loc.strip()
    text = raw.casefold()
    if not text:
        return False
    # Explicit US country markers
    if US_COUNTRY_RE.search(text):
        return True
    # Country context prevents ambiguous city names and state-code fragments
    # from making a foreign posting look domestic.
    if _has_foreign_country_marker(text):
        return False
    # Acronyms
    if re.search(r"\b(sf|nyc|la)\b", text):
        return True
    # Full US State names
    if any(re.search(rf"\b{re.escape(st)}\b", text) for st in US_STATES):
        return True
    # 2-letter postal abbreviations
    if re.search(r"\b(" + "|".join(US_STATE_CODES) + r")\b", raw):
        # Prevent India matching IN
        if re.search(r"\bIN\b", raw) and any(p.search(raw) for p in DISALLOWED_PATTERNS):
            return False
        return True
    # Major US Cities
    if any(re.search(rf"\b{re.escape(c)}\b", text) for c in MAJOR_US_CITIES):
        return True
    # Favorite metros
    if is_favorite_metro(loc):
        return True
    return False


def is_approved_international(loc: str) -> bool:
    """Check if location matches an approved international metropolitan hub."""
    text = loc.casefold().strip()
    if not text:
        return False
    # A US state/country context takes precedence over a hub with the same name
    # (for example, Paris, Texas).
    if is_us_location(loc):
        return False
    matched_cities = {
        city for city in APPROVED_INTERNATIONAL_METROS
        if re.search(rf"\b{re.escape(city)}\b", text)
    }
    if not matched_cities:
        return False
    country_contexts = _foreign_country_contexts(text)
    if not country_contexts:
        return True
    return any(
        country in country_contexts and bool(cities.intersection(matched_cities))
        for country, cities in APPROVED_METRO_COUNTRIES.items()
    )


def is_disallowed_location(loc: str) -> bool:
    """Check if location explicitly matches a disallowed foreign region."""
    raw = loc.strip()
    if not raw:
        return False
    # If it has a clear US indicator (like Charlotte, NC or Indianapolis, IN), it's not solely disallowed
    if is_us_location(loc) and not re.search(r"remote\s*-\s*india", raw, re.I):
        return False
    if is_approved_international(loc):
        return False
    return _has_foreign_country_marker(raw) or any(p.search(raw) for p in DISALLOWED_PATTERNS)


def is_location_acceptable(loc: str | None) -> bool:
    return _location_acceptable(loc or "")


@lru_cache(maxsize=16384)
def _location_acceptable(loc: str) -> bool:
    """Determine whether a posting location is acceptable for ingestion and auto-apply.

    Acceptable if:
    - Empty, unspecified, or general remote
    - Located in the United States
    - Located in an approved international metropolitan city
    - For multi-location postings: contains at least ONE US or approved international location.
    """
    if not loc or not loc.strip():
        return True
    segments = [s.strip() for s in re.split(r"[;|•\n]+", loc) if s.strip()]
    if not segments:
        return True

    # Check if ANY segment is US or approved international
    has_us = any(is_us_location(s) for s in segments)
    has_approved_intl = any(is_approved_international(s) for s in segments)
    if has_us or has_approved_intl:
        return True

    # If all segments are disallowed
    if all(is_disallowed_location(s) for s in segments):
        return False

    # Only unspecified work modes are neutral; a country restriction stays a restriction.
    text = loc.casefold().strip()
    if re.fullmatch(r"remote|remoto|hybrid|distributed|in-office|multiple locations|n/a|worldwide|anywhere|global", text):
        return True

    # If it is not US and not approved international, but is foreign
    if any(p.search(loc) for p in DISALLOWED_PATTERNS):
        return False

    # Any other foreign location not matching approved metros
    return False


def classify_location(
    loc: str | None,
    preferred_locations: Sequence[str] | None = None,
    *,
    is_remote: bool = False,
    remote_ok: bool = True,
    favorite_keys: Sequence[str] | None = None,
) -> LocationResult:
    # Cached: postings share a few thousand location strings, and every scan
    # re-scores the whole library. Callers treat the result as read-only.
    return _classify_location(loc or "", tuple(preferred_locations or ()), bool(is_remote), bool(remote_ok),
                              tuple(favorite_keys) if favorite_keys else None)


@lru_cache(maxsize=16384)
def _classify_location(
    loc: str,
    preferred_locations: tuple[str, ...],
    is_remote: bool,
    remote_ok: bool,
    favorite_keys: tuple[str, ...] | None,
) -> LocationResult:
    """Evaluate and score a posting location against user preferences.

    Scoring Tiers (out of 10.0 points):
    - Favorite Metros (NYC, Chicago, LA, SF): 10.0 points
    - Other Preferred Locations in settings: 8.0 points
    - General US Remote (when remote_ok): 7.0 points
    - Other Domestic US Locations: 5.0 points
    - Unspecified / blank location: 4.0 points
    - Approved International Metros:
        - If explicitly preferred: 7.5 points
        - Otherwise: 2.0 points
    - Disallowed Foreign Locations: 0.0 points (ineligible)
    """
    text = (loc or "").strip()
    text_cf = text.casefold()
    prefs = [p.casefold().strip() for p in (preferred_locations or []) if p.strip()]

    # Empty or blank location
    if not text:
        if is_remote and remote_ok:
            return LocationResult(
                is_favorite=False, is_preferred=False, is_us=True,
                is_approved_intl=False, is_disallowed=False, is_remote=True,
                score=7.0, review_reason=None,
            )
        return LocationResult(
            is_favorite=False, is_preferred=False, is_us=True,
            is_approved_intl=False, is_disallowed=False, is_remote=False,
            score=4.0, review_reason=None,
        )

    if not is_location_acceptable(text):
        return LocationResult(
            is_favorite=False, is_preferred=False, is_us=False,
            is_approved_intl=False, is_disallowed=True, is_remote=is_remote,
            score=0.0, review_reason="Location outside US and approved metropolitan hubs",
        )

    segments = [s.strip() for s in re.split(r"[;|•\n]+", text) if s.strip()]
    if not segments:
        segments = [text]

    # Check favorite cities
    has_favorite = any(is_favorite_metro(s, favorite_keys) for s in segments)
    if has_favorite:
        return LocationResult(
            is_favorite=True, is_preferred=True, is_us=True,
            is_approved_intl=False, is_disallowed=False, is_remote=is_remote,
            score=10.0, review_reason=None,
        )

    # Check preferred locations match
    has_preferred = False
    for p in prefs:
        if p in ("remote", "united states", "usa", "us"):
            continue
        if any(_is_bare_or_word(p, s.casefold()) for s in segments):
            has_preferred = True
            break

    if has_preferred:
        return LocationResult(
            is_favorite=False, is_preferred=True, is_us=any(is_us_location(s) for s in segments),
            is_approved_intl=any(is_approved_international(s) for s in segments),
            is_disallowed=False, is_remote=is_remote,
            score=8.0, review_reason=None,
        )

    # Check remote
    is_remote_loc = (
        is_remote
        or bool(re.search(r"\b(remote|remoto|worldwide|anywhere|global)\b", text_cf))
    )
    has_us = any(is_us_location(s) for s in segments)
    has_approved_intl = any(is_approved_international(s) for s in segments)

    # Check for disallowed foreign location
    all_disallowed = all(is_disallowed_location(s) for s in segments)
    if all_disallowed or (not has_us and not has_approved_intl and not is_remote_loc and is_disallowed_location(text)):
        return LocationResult(
            is_favorite=False, is_preferred=False, is_us=False,
            is_approved_intl=False, is_disallowed=True, is_remote=False,
            score=0.0, review_reason="Location outside US and approved metropolitan hubs",
        )

    # If job is remote and candidate accepts remote
    if is_remote_loc and remote_ok:
        # Check if remote in a disallowed region (e.g. Remote - India)
        if any(p.search(text) for p in DISALLOWED_PATTERNS) and not has_us:
            return LocationResult(
                is_favorite=False, is_preferred=False, is_us=False,
                is_approved_intl=False, is_disallowed=True, is_remote=True,
                score=0.0, review_reason="Location outside US and approved metropolitan hubs",
            )
        # If remote is explicitly restricted to an approved foreign location (e.g. Remote, Australia / Remote, UK)
        if has_approved_intl and not has_us:
            if has_preferred:
                return LocationResult(
                    is_favorite=False, is_preferred=True, is_us=False,
                    is_approved_intl=True, is_disallowed=False, is_remote=True,
                    score=8.0, review_reason=None,
                )
            return LocationResult(
                is_favorite=False, is_preferred=False, is_us=False,
                is_approved_intl=True, is_disallowed=False, is_remote=True,
                score=2.0, review_reason="Location outside your preferences",
            )
        # Domestic US remote or worldwide
        score = 10.0 if any(u in prefs for u in ("united states", "usa", "us")) else 7.0
        return LocationResult(
            is_favorite=False, is_preferred=any(u in prefs for u in ("united states", "usa", "us")),
            is_us=True, is_approved_intl=False, is_disallowed=False, is_remote=True,
            score=score, review_reason=None,
        )

    # General domestic US location (not in candidate's preferred list)
    if has_us:
        return LocationResult(
            is_favorite=False, is_preferred=False, is_us=True,
            is_approved_intl=has_approved_intl, is_disallowed=False, is_remote=is_remote,
            score=5.0, review_reason="Location outside your preferences",
        )

    # Approved international metropolitan hub (not in candidate's preferred list)
    if has_approved_intl:
        return LocationResult(
            is_favorite=False, is_preferred=False, is_us=False,
            is_approved_intl=True, is_disallowed=False, is_remote=is_remote,
            score=2.0, review_reason="Location outside your preferences",
        )

    # Any other foreign location not matching approved metros
    return LocationResult(
        is_favorite=False, is_preferred=False, is_us=False,
        is_approved_intl=False, is_disallowed=True, is_remote=False,
        score=0.0, review_reason="Location outside US and approved metropolitan hubs",
    )
