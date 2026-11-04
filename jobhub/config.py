"""Paths, environment, and default settings.

Everything user-tunable lives in the `settings` table (see db.py) and is seeded
from DEFAULT_SETTINGS. Environment variables only cover secrets and runtime knobs.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

APP_NAME = "Starployment"
# Installed builds before the rename kept candidate data under this name.
LEGACY_APP_NAME = "JobHub"


def _platform_data_base() -> tuple[Path, str, str]:
    """Return the per-user data base directory and the new/legacy folder names."""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support", APP_NAME, LEGACY_APP_NAME
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local).expanduser() if local else home / "AppData" / "Local"
        if not base.is_absolute():
            base = home / "AppData" / "Local"
        return base, APP_NAME, LEGACY_APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else home / ".local" / "share"
    if not base.is_absolute():
        base = home / ".local" / "share"
    return base, APP_NAME.lower(), LEGACY_APP_NAME.lower()


def _data_dir() -> Path:
    """Keep source checkouts compatible and give installed apps private user data.

    The app was renamed from JobHub to Starployment. An existing JobHub data
    folder keeps being used in place (never moved or copied), so an upgrade
    cannot lose or duplicate candidate data.
    """
    override = os.environ.get("STARPLOYMENT_DATA_DIR") or os.environ.get("JOBHUB_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if not getattr(sys, "frozen", False):
        return ROOT / "data"
    base, name, legacy = _platform_data_base()
    current, previous = base / name, base / legacy
    if not current.exists() and previous.is_dir() and (previous / "jobhub.db").exists():
        return previous
    return current


def _validate_data_root(root: Path) -> Path:
    root = Path(root).expanduser()
    if root.is_symlink():
        raise ValueError(f"Refusing to use a symlink as the Starployment data root: {root}")
    root = root.resolve()
    home = Path.home().resolve()
    if root == root.parent or root == home or home.is_relative_to(root):
        raise ValueError(f"Refusing to use an unsafe Starployment data root: {root}")
    if root.is_dir():
        children = list(root.iterdir())
        managed = {"resumes", "tailored", "screenshots", "browser_profile", "jobhub.db",
                   "jobhub.db-wal", "jobhub.db-shm", ".env", ".checkout-import.json", "agent_scratch"}
        if children and not any(item.name in managed for item in children):
            raise ValueError(f"Refusing to use an unrecognized Starployment data directory: {root}")
    return root


def secure_data_tree(root: Path | None = None) -> None:
    """Make the app-owned private data tree accessible only to its owner."""
    requested_root = Path(root or DATA).expanduser()
    if requested_root.is_symlink():
        raise ValueError(f"Refusing to secure a symlink as the Starployment data root: {requested_root}")
    root = _validate_data_root(requested_root)
    if not root.exists():
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
    managed_dirs = {"resumes", "tailored", "screenshots", "browser_profile", "agent_scratch"}
    managed_files = {"jobhub.db", "jobhub.db-wal", "jobhub.db-shm", ".env", ".checkout-import.json", "secrets.json"}
    children = list(root.iterdir())
    if children and not any(item.name in managed_dirs | managed_files for item in children):
        raise ValueError(f"Refusing to change permissions on an unrecognized data directory: {root}")
    paths = [root]
    # Only touch app-owned paths. User overrides may point inside a directory
    # that also contains unrelated files, so never recursively chmod the root.
    for child in children:
        if child.name in managed_files:
            paths.append(child)
        elif child.name in managed_dirs and child.is_dir() and not child.is_symlink():
            paths.append(child)
            paths.extend(child.rglob("*"))
    for path in sorted(paths, key=lambda item: len(item.parts)):
        if path.is_symlink():
            continue
        if os.name == "nt":
            from .platform_support import windows_private_permissions
            windows_private_permissions(path)
        elif path.is_dir():
            path.chmod(0o700)
        elif path.is_file():
            path.chmod(0o600)


def secure_file(path: Path) -> None:
    """Restrict a newly written user artifact to its owner."""
    if os.name == "nt":
        from .platform_support import windows_private_permissions
        windows_private_permissions(Path(path))
    else:
        Path(path).chmod(0o600)


DATA = _data_dir()
DATA = _validate_data_root(DATA)
RESUMES = DATA / "resumes"
TAILORED = DATA / "tailored"
SCREENSHOTS = DATA / "screenshots"
BROWSER_PROFILE = DATA / "browser_profile"
RESUME_LIBRARY = RESUMES / "library"
AGENT_SCRATCH = DATA / "agent_scratch"
AGENT_HOME = DATA / "agent_home"       # agy runs here: the sign-in only, no MCP servers
DB_PATH = DATA / "jobhub.db"
WEB = ROOT / "web"

for _p in (DATA, RESUMES, TAILORED, SCREENSHOTS, BROWSER_PROFILE, RESUME_LIBRARY, AGENT_SCRATCH):
    _p.mkdir(parents=True, mode=0o700, exist_ok=True)
secure_data_tree(DATA)

load_dotenv(DATA / ".env")
if not getattr(sys, "frozen", False):
    load_dotenv(ROOT / ".env")


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


PORT = int(env("JOBHUB_PORT", "8765"))
HEADLESS = env("JOBHUB_HEADLESS", "1") not in ("0", "false", "no")
LLM_BACKEND = env("JOBHUB_LLM_BACKEND", "auto")  # auto | anthropic | claude_cli | codex_cli | none
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = env("JOBHUB_ANTHROPIC_MODEL", "claude-sonnet-5-5")
CLAUDE_CLI_MODEL = env("JOBHUB_CLAUDE_CLI_MODEL", "sonnet")
CODEX_MODEL = env("JOBHUB_CODEX_MODEL", "gpt-5.5")  # codex CLI default model may be unsupported on ChatGPT plans

# Company slugs for the free, public ATS JSON APIs. A bad slug is harmless (404 -> skipped).
GREENHOUSE_COMPANIES = [
    "stripe", "airbnb", "coinbase", "databricks", "dropbox", "figma", "discord", "robinhood",
    "cloudflare", "doordash", "lyft", "pinterest", "reddit", "twitch", "gitlab", "hashicorp",
    "brex", "plaid", "scaleai", "anthropic", "duolingo", "samsara", "flexport", "benchling",
    "asana", "instacart", "affirm", "nuro", "appliedintuition", "andurilindustries", "chime",
    "carta", "gusto", "rippling", "verkada", "mongodb", "elastic", "datadog", "snowflake",
    "twilio", "okta", "zscaler", "nextdoor", "roblox", "unity", "epicgames", "riotgames",
    "twosigma", "hudsonrivertrading", "janestreet", "imc", "drw", "optiver", "akunacapital",
    "citadel", "point72", "squarepoint", "sig", "virtu", "belvederetrading",
]
LEVER_COMPANIES = [
    "palantir", "zoox", "cruise", "mistral", "figureai", "shield-ai", "lucidmotors", "kraken",
    "netlify", "plaid", "veeva", "anyscale", "wealthsimple", "wealthfront", "quora",
    "tower-research-capital", "five-rings", "aquatic", "cubist", "jumptrading",
]
ASHBY_COMPANIES = [
    "openai", "ramp", "notion", "linear", "vercel", "replit", "anysphere", "cognition",
    "perplexity", "supabase", "modal", "elevenlabs", "harvey", "sierra", "glean", "hex",
    "mercury", "deel", "runway", "together-ai", "character", "wispr", "lovable", "browser-use",
]

# Boards added to the bundled catalog in version 2. Each one was checked
# against its public API when added. Existing installs receive these once
# (see db._upgrade_settings), so a board the candidate removes stays removed.
CATALOG_VERSION = 4
CATALOG_ADDITIONS = {
    "greenhouse": [
        "airtable", "cockroachlabs", "coursera", "faire", "fivetran", "gemini", "intercom", "lattice",
        "mixpanel", "peloton", "squarespace", "toast", "tripadvisor", "udemy", "waymo", "webflow", "yext",
        "zoominfo", "pagerduty", "newrelic", "launchdarkly", "grafanalabs", "fastly", "checkr", "sofi",
        "betterment", "justworks", "ripple", "khanacademy", "spacex", "planetlabs", "astranis", "xai",
        "wizinc", "flatironhealth", "oscar", "roku", "oura", "clearstreet", "flowtraders", "chicagotrading",
        "gravitonresearchcapital", "jumptrading", "drweng", "sigmacomputing", "sambanovasystems", "coreweave",
        "figure", "kodiak", "mercury", "mozilla", "wikimedia", "lucidmotors", "attentive", "klaviyo", "braze",
        "contentful", "calendly", "monzo", "scopely", "crunchyroll", "vercel", "netlify", "humeai",
        # Version 3: finance, fintech, FP&A software, insurance, and consulting boards, so
        # finance, accounting, and business-analysis roles are found as readily as software ones.
        "aqr", "bridgewater89", "pdtpartners", "marshallwace", "schonfeld", "worldquant", "genevatrading",
        "gsacapital", "towerresearchcapital", "mangroup", "engineersgate", "williamblair",
        "lincolninternational", "generalatlantic", "charlesriverassociates", "addepar1", "iex", "alphasense",
        "upstart", "pagaya", "oportun", "alloy", "adyen", "billcom", "fireblocks", "alpaca", "earnin", "blend",
        "wise", "sezzle", "payoneer", "zuora", "highradius", "anaplan", "datarails", "a16z", "iconiq",
        "exoduspoint", "optiverus", "bitgo", "consensys", "thoughtworks", "amwins", "costar", "coalition",
        "ethoslife", "pieinsurance", "counterpart", "insurify", "federato",
        # Version 4: wealth and asset management (advisory platforms, RIAs, private markets).
        "altruist", "artisanpartners", "focusfinancialpartners", "stepstone", "vestwell", "humaninterest",
        "public",
    ],
    "lever": ["spotify", "outreach", "gopuff", "ro", "binance", "offchainlabs",
              "anchorage", "finix", "dlocal", "nium",
              "fundrise"],
    "ashby": [
        "cohere", "posthog", "render", "railway", "neon", "clickhouse", "airbyte", "prefect", "astronomer",
        "weaviate", "pinecone", "langchain", "llamaindex", "baseten", "lambda", "crusoe", "cerebras", "etched",
        "snowflake", "confluent", "vanta", "drata", "plaid", "zip", "attio", "pylon", "decagon", "abridge",
        "suno", "pika", "krea", "1password", "temporal", "sentry", "cribl", "mintlify", "resend", "omni",
        "cursor", "warp", "saronic",
        "voleon", "lemonade", "openly", "kin", "bestow", "dave", "nubank", "nerdwallet", "moderntreasury",
        "column", "circle", "acorns", "rho", "cube", "airwallex", "pleo", "ramp",
        "savvy", "range", "titan", "compound", "artafinance", "frec", "wealthsimple",
    ],
}
# Workday boards, written tenant.wdN/site (see sources/workday.py). Each was
# checked against Workday's public job search when added. Banks, insurers, and
# asset managers that publish nowhere else.
WORKDAY_COMPANIES = [
    "citi.wd5/2", "ms.wd5/external", "blackrock.wd1/blackrock_professional", "statestreet.wd1/global",
    "troweprice.wd5/troweprice", "travelers.wd5/external", "spgi.wd5/spgi_careers", "aig.wd1/early_careers",
    "aig.wd1/aig", "barclays.wd3/external_career_site_barclays", "vanguard.wd5/vanguard_external",
    "capitalone.wd12/capital_one", "fmr.wd1/fidelitycareers", "fmr.wd1/targeted", "lseg.wd3/graduate_careers",
    "td.wd3/td_bank_careers", "clearwateranalytics.wd1/clearwater_analytics_careers", "visa.wd5/visa",
    "equifax.wd5/ur_external", "cigna.wd5/cignacareers", "nvidia.wd5/nvidiaexternalcareersite",
]
GREENHOUSE_COMPANIES = list(dict.fromkeys(GREENHOUSE_COMPANIES + CATALOG_ADDITIONS["greenhouse"]))
LEVER_COMPANIES = list(dict.fromkeys(LEVER_COMPANIES + CATALOG_ADDITIONS["lever"]))
ASHBY_COMPANIES = list(dict.fromkeys(ASHBY_COMPANIES + CATALOG_ADDITIONS["ashby"]))

# Priority ("FAANG+") employers. Their applications are starred and are never
# sent automatically: they are prepared like any other, then wait for the
# candidate to refine and approve each one. Editable in Settings.
PRIORITY_COMPANIES = [
    "Google", "Alphabet", "DeepMind", "Meta", "Facebook", "Apple", "Amazon", "Netflix", "Microsoft",
    "Nvidia", "OpenAI", "Anthropic", "Tesla", "SpaceX", "Stripe", "Airbnb", "Uber", "Databricks",
    "Palantir", "LinkedIn", "Jane Street", "Citadel", "Two Sigma", "Hudson River Trading", "Jump Trading",
    "D. E. Shaw", "Optiver", "IMC", "Susquehanna", "Five Rings",
]


# Starting title lists by role family, offered in Settings → What to look for → Add a
# role family. Adding one appends its titles to target_titles; nothing else changes.
ROLE_PRESETS: dict[str, list[str]] = {
    "Software": ["software engineer", "software developer", "backend engineer", "frontend engineer", "full stack",
                 "platform engineer", "infrastructure engineer", "site reliability engineer", "mobile engineer",
                 "new grad", "entry level"],
    "Data & ML": ["data scientist", "data analyst", "data engineer", "analytics engineer", "machine learning engineer",
                  "ai engineer", "applied scientist", "decision scientist", "business intelligence analyst"],
    "Quant & trading": ["quantitative developer", "quantitative researcher", "quantitative analyst",
                        "quantitative trader", "trading analyst", "quantitative strategist", "algorithmic trading"],
    "Finance & FP&A": ["fp&a", "fp&a analyst", "financial analyst", "finance analyst", "financial planning",
                       "strategic finance", "corporate finance analyst", "business finance", "budget analyst",
                       "revenue analyst", "treasury analyst", "investment analyst", "investment banking analyst",
                       "equity research", "credit analyst", "valuation analyst", "corporate development analyst",
                       "finance rotational", "finance development program", "investment operations"],
    "Wealth & asset management": ["wealth management", "wealth management analyst", "wealth management associate",
                                  "private wealth", "private wealth management", "private banking analyst",
                                  "wealth advisor associate", "associate wealth advisor", "wealth planning",
                                  "financial planning associate", "paraplanner", "asset management analyst",
                                  "asset management associate", "investment management analyst",
                                  "portfolio associate", "portfolio operations", "trust analyst",
                                  "investment consultant analyst"],
    "Accounting": ["accountant", "staff accountant", "accounting analyst", "accounting associate",
                   "revenue accountant", "financial reporting", "technical accounting", "fund accountant",
                   "fund accounting", "tax analyst", "tax associate", "audit associate", "internal audit",
                   "assurance associate", "sox analyst"],
    "Business & strategy": ["business analyst", "business systems analyst", "business operations",
                            "operations analyst", "strategy analyst", "strategy & operations", "strategy and operations",
                            "bizops", "commercial analyst", "pricing analyst", "revenue operations analyst",
                            "deal desk analyst", "product analyst", "associate product manager",
                            "associate consultant", "consulting analyst", "management consultant",
                            "supply chain analyst", "procurement analyst"],
    "Risk & actuarial": ["risk analyst", "credit risk", "market risk", "model validation", "actuarial",
                         "underwriting analyst", "insurance analyst", "compliance analyst", "fraud analyst"],
    "Economics & research": ["economist", "economic analyst", "economic consultant", "economic research",
                             "research analyst", "research associate", "policy analyst", "econometrician",
                             "statistician"],
}


DEFAULT_SETTINGS: dict = {
    # --- desktop appearance ---
    "appearance": "System",       # System | Light | Dark
    "font": "Claude",             # Claude (Anthropic Sans/Serif from the installed Claude app) | System
    # --- autonomy ---
    "setup_complete": False,     # first-run desktop setup completed by the candidate
    "jarvis_mode": "off",          # off | new (recent postings) | all (autonomous AI Autofill) | all_plus (ALL+, overnight)
    "auto_apply": False,           # when True the hourly loop applies to top jobs on its own
    "dry_run": False,              # fill every form but never click Submit
    "scan_interval_min": 60,
    "max_apply_per_run": 3,
    "min_score_to_apply": 60,
    # Automatic runs (auto_apply and JARVIS) stop sending to an employer once it has
    # this many attempts in the last ``days`` days, and alternate role families
    # (software, data, quant, finance, accounting...) so one kind of role or one
    # large employer cannot take every overnight send.
    "company_cap": {"count": 3, "days": 30},
    "mix_role_families": True,
    # Delete untouched postings that are no longer hiring: gone from a board that was
    # read successfully, or not found by any scan for ``unseen_days`` days.
    "prune": {"enabled": True, "unseen_days": 21},
    # --- sending: applications sent side by side, each in its own background browser ---
    "parallel_sends": 4,
    "parallel_prepare": 8,         # forms read and Luna/AI Autofill drafts run at once while preparing
    "parallel_agent_calls": 4,     # agent calls one AI Autofill splits its questions across
    "show_browser": False,         # True opens a visible browser window while sending
    # --- priority employers: starred, prepared, never sent automatically ---
    "priority_companies": PRIORITY_COMPANIES,
    # --- targeting ---
    "target_titles": [],
    "exclude_title_words": [],
    "include_internships": False,
    "preferred_locations": [],
    "favorite_locations": ["New York", "Chicago", "Los Angeles", "San Francisco"],
    "remote_ok": True,
    "min_salary": 0,
    "years_experience": 0,
    # --- sources ---
    "sources": {"greenhouse": True, "lever": True, "ashby": True, "workday": True, "simplify": True,
                "remoteok": True, "jobspy": True},
    "jobspy": {
        # ZipRecruiter refuses JobSpy (HTTP 403), so it is not searched by default.
        "sites": ["indeed", "linkedin", "google"],
        "search_terms": [],
        "location": "",
        "country_indeed": "usa",
        "results_wanted": 50,
        "hours_old": 72,
    },
    "companies": {"greenhouse": GREENHOUSE_COMPANIES, "lever": LEVER_COMPANIES,
                  "ashby": ASHBY_COMPANIES, "workday": WORKDAY_COMPANIES},
    # Also scan the public boards of companies found in other listings.
    "discover_boards": True,
    "catalog_version": CATALOG_VERSION,
    # --- resume ---
    # tailored: a one-page PDF generated from the resume builder for each role
    # library / bank: the most relevant untouched resume selected from the uploaded resume bank
    # original: always the main resume PDF
    "resume_strategy": "tailored",
    # Generated resumes are always one page; ``one_page`` stays for older saved settings.
    "resume_builder": {"one_page": True, "luna_select": False, "max_skills": 28},
    # --- Luna, the optional drafting assistant (needs a connected agent) ---
    "agent": {"backend": "none", "model": ""},
    "luna": {"draft_answers": True, "remember_approved": True, "autofill_on_prepare": True},
    # --- the Overview "Apply" plan ---
    # posted_within_hours: 0 means any posting date; order: "match" or "newest".
    "apply_plan": {"count": 100, "min_score": 60, "one_per_company": True,
                   "posted_within_hours": 0, "order": "match", "exclude_starred": True},
    # --- optional inbox tracking for replies (uses the mailbox below) ---
    # archive_rejections: take matched rejection emails out of the Inbox (never deletes; Gmail adds a label).
    "email_tracking": {"enabled": False, "lookback_days": 30, "use_luna": True, "archive_rejections": False},
    # --- canned answers (edit in the UI; used when a form asks) ---
    "answers": {
        "work_authorized_us": "",
        "requires_sponsorship": "",
        "us_citizen_or_permanent_resident": "",
        "salary_expectation": "",
        "available_start": "",
        "how_heard": "",
        "willing_to_relocate": "",
        "preferred_name": "",
        "pronouns": "",
        "years_experience": "",
        # Standing answers for every employer (Profile → Answers → Standing answers; answers.py).
        "over_18": "",
        "relatives_at_employer": "",
        "non_compete": "",
        "security_clearance": "",
        "former_employee": "",           # "No": no, unless the employer is in the resume's work history
        "application_terms_consent": "",  # "Yes": tick Workday's application-terms box
        "accuracy_certification": "",    # "Yes": tick Workday's "the information provided is accurate" box
        "workday_how_heard": "",         # a specific source for Workday's "How did you hear" picker, e.g. LinkedIn
        "phone_type": "",                # Mobile | Home | Business
        "native_language": "",           # e.g. English: Workday's Languages block, at the top proficiency
        "pay_range_target": "",          # e.g. 100000: a required pay-range list picks the range containing it
        "cover_letter_mode": "auto",     # auto | none
        "confirmed_fields": [],          # values explicitly saved by the candidate
        "field_answers": {},             # exact question label -> candidate answer
    },
    # --- optional Greenhouse email-code completion ---
    # The IMAP password is stored separately in the native OS credential vault.
    "email_verification": {
        "enabled": False,
        "imap_host": "",
        "imap_port": 993,
        "imap_username": "",
        "sender_address": "",
    },
    # --- optional agent harness fallback for forms the engine can't finish ---
    "harness": {"enabled": False, "backend": "codex_cli"},
    # --- Workday (apply/workday.py, vault.py) ---
    # create_accounts: create each employer's Workday account with a generated
    # password kept in the local vault, and sign in with it.
    # send: Approve & send, Send ready, and automatic runs fill and submit
    # Workday applications, signing in with the vault's saved password.
    # check_portal: read each employer's Workday Candidate Home with the saved sign-in to
    # track the applications sent there (many employers send no email until a decision).
    "workday": {"create_accounts": False, "send": False, "check_portal": True},
    # --- where saved passwords and API keys live ---
    # False keeps them in an owner-only file in the data folder, so a rebuilt
    # app never waits on a Keychain prompt. True uses the OS credential vault.
    "keychain_protection": False,
}


def merge_settings(base: dict, patch: dict) -> dict:
    """Merge nested preferences without losing unrelated saved values."""
    import copy
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if key != "field_answers" and isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_settings(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def validate_settings(settings: dict) -> dict:
    """Validate user settings at the boundary before scheduler/browser use."""
    from typing import Literal
    from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

    class StrictModel(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class Answers(StrictModel):
        work_authorized_us: Literal["", "Yes", "No"]
        requires_sponsorship: Literal["", "Yes", "No"]
        us_citizen_or_permanent_resident: Literal["", "Yes", "No"]
        willing_to_relocate: Literal["", "Yes", "No"]
        salary_expectation: StrictStr
        available_start: StrictStr
        how_heard: StrictStr
        preferred_name: StrictStr
        pronouns: StrictStr
        years_experience: StrictStr
        over_18: Literal["", "Yes", "No"] = ""
        relatives_at_employer: Literal["", "Yes", "No"] = ""
        non_compete: Literal["", "Yes", "No"] = ""
        security_clearance: Literal["", "Yes", "No"] = ""
        former_employee: Literal["", "No"] = ""
        application_terms_consent: Literal["", "Yes"] = ""
        accuracy_certification: Literal["", "Yes"] = ""
        workday_how_heard: StrictStr = ""
        phone_type: StrictStr = ""
        native_language: StrictStr = ""
        pay_range_target: StrictStr = ""
        cover_letter_mode: Literal["auto", "none"]
        confirmed_fields: list[StrictStr]
        field_answers: dict[StrictStr, StrictStr | StrictBool]

        @field_validator("confirmed_fields")
        @classmethod
        def confirm_known_fields(cls, value):
            allowed = set(DEFAULT_SETTINGS["answers"]) - {"confirmed_fields", "field_answers", "cover_letter_mode"}
            if not set(value) <= allowed:
                raise ValueError("Unknown confirmed answer field")
            return list(dict.fromkeys(value))

    class JobSpy(StrictModel):
        sites: list[Literal["indeed", "linkedin", "zip_recruiter", "google", "glassdoor"]]
        search_terms: list[StrictStr] = Field(max_length=20)
        location: StrictStr
        country_indeed: StrictStr
        results_wanted: StrictInt = Field(ge=1, le=100)
        hours_old: StrictInt = Field(ge=1, le=8760)

    class Harness(StrictModel):
        enabled: StrictBool
        backend: Literal["codex_cli", "claude_cli"]

    class Workday(StrictModel):
        create_accounts: StrictBool
        send: StrictBool
        check_portal: StrictBool = True

    class CompanyCap(StrictModel):
        count: StrictInt = Field(ge=0, le=100)     # 0 turns the cap off
        days: StrictInt = Field(ge=1, le=365)

    class Prune(StrictModel):
        enabled: StrictBool
        unseen_days: StrictInt = Field(ge=3, le=365)

    class ResumeBuilder(StrictModel):
        one_page: StrictBool
        luna_select: StrictBool
        max_skills: StrictInt = Field(ge=5, le=80)

    class Agent(StrictModel):
        backend: Literal["none", "antigravity_cli", "claude_cli", "codex_cli", "anthropic_api", "openai_api"]
        model: StrictStr = Field(max_length=80)

        @field_validator("model")
        @classmethod
        def simple_model_name(cls, value):
            import re
            value = value.strip()
            if value and not re.fullmatch(r"[A-Za-z0-9._:/-]{1,80}", value):
                raise ValueError("Model names may contain letters, numbers, dots, dashes, colons, and slashes")
            return value

    class Luna(StrictModel):
        draft_answers: StrictBool
        remember_approved: StrictBool
        autofill_on_prepare: StrictBool

    class ApplyPlan(StrictModel):
        count: StrictInt = Field(ge=1, le=500)
        min_score: float = Field(ge=0, le=100, allow_inf_nan=False)
        one_per_company: StrictBool
        exclude_starred: StrictBool
        posted_within_hours: StrictInt = Field(ge=0, le=8760)
        order: Literal["match", "newest"]

    class EmailTracking(StrictModel):
        enabled: StrictBool
        lookback_days: StrictInt = Field(ge=1, le=365)
        use_luna: StrictBool
        archive_rejections: StrictBool = False

    class EmailVerification(StrictModel):
        enabled: StrictBool
        imap_host: StrictStr = Field(max_length=253)
        imap_port: StrictInt = Field(ge=1, le=65535)
        imap_username: StrictStr = Field(max_length=320)
        sender_address: StrictStr = Field(max_length=320)

        @field_validator("imap_host", "imap_username", "sender_address")
        @classmethod
        def no_control_characters(cls, value):
            if any(ord(char) < 32 for char in value):
                raise ValueError("Control characters are not allowed")
            return value.strip()

        @model_validator(mode="after")
        def configured_when_enabled(self):
            import re
            if self.imap_host and not re.fullmatch(r"[A-Za-z0-9.-]+", self.imap_host):
                raise ValueError("The IMAP server must be a host name such as imap.gmail.com")
            if self.sender_address and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", self.sender_address):
                raise ValueError("The trusted sender must be one email address")
            # A blank sender matches ATS and employer senders automatically (see EmailVerificationInbox).
            if self.enabled and not (re.fullmatch(r"[A-Za-z0-9.-]+", self.imap_host) and self.imap_username):
                raise ValueError("Enabled email verification needs an IMAP host and username")
            return self

    class Settings(StrictModel):
        appearance: Literal["System", "Light", "Dark"]
        font: Literal["Claude", "System"]
        setup_complete: StrictBool
        jarvis_mode: Literal["off", "new", "all", "all_plus"] = "off"
        auto_apply: StrictBool
        dry_run: StrictBool
        scan_interval_min: StrictInt = Field(ge=5, le=10080)
        max_apply_per_run: StrictInt = Field(ge=1, le=25)
        company_cap: CompanyCap
        mix_role_families: StrictBool
        prune: Prune
        parallel_sends: StrictInt = Field(ge=1, le=24)
        parallel_prepare: StrictInt = Field(ge=1, le=32)
        parallel_agent_calls: StrictInt = Field(ge=1, le=16)
        show_browser: StrictBool
        min_score_to_apply: float = Field(ge=0, le=100, allow_inf_nan=False, strict=True)
        priority_companies: list[StrictStr] = Field(max_length=300)
        target_titles: list[StrictStr] = Field(max_length=300)
        exclude_title_words: list[StrictStr] = Field(max_length=100)
        include_internships: StrictBool
        preferred_locations: list[StrictStr] = Field(max_length=100)
        favorite_locations: list[StrictStr] = Field(default_factory=lambda: ["New York", "Chicago", "Los Angeles", "San Francisco"], max_length=100)
        remote_ok: StrictBool
        min_salary: StrictInt = Field(ge=0, le=10000000)
        years_experience: float = Field(ge=0, le=80, allow_inf_nan=False, strict=True)
        sources: dict[Literal["greenhouse", "lever", "ashby", "workday", "simplify", "remoteok", "jobspy"], StrictBool]
        jobspy: JobSpy
        companies: dict[Literal["greenhouse", "lever", "ashby", "workday"], list[StrictStr]]
        discover_boards: StrictBool
        catalog_version: StrictInt = Field(ge=1)
        resume_strategy: Literal["original", "tailored", "library", "bank"]
        resume_builder: ResumeBuilder
        agent: Agent
        luna: Luna
        apply_plan: ApplyPlan
        email_tracking: EmailTracking
        answers: Answers
        email_verification: EmailVerification
        keychain_protection: StrictBool
        harness: Harness
        workday: Workday

        @field_validator("priority_companies")
        @classmethod
        def clean_companies(cls, value):
            names = []
            for name in value:
                name = " ".join(str(name).split())
                if any(ord(char) < 32 for char in name) or len(name) > 100:
                    raise ValueError("Priority companies must be short names")
                if name and name.casefold() not in {n.casefold() for n in names}:
                    names.append(name)
            return names

        @field_validator("companies")
        @classmethod
        def valid_slugs(cls, value):
            import re
            for ats, slugs in value.items():
                # Workday boards are tenant.wdN/site (sources/workday.py); the others are simple slugs.
                pattern = (r"[a-zA-Z0-9][a-zA-Z0-9-]{0,62}\.wd\d{1,3}/[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}"
                           if ats == "workday" else r"[a-zA-Z0-9_-]{1,100}")
                if len(slugs) > 500 or any(not re.fullmatch(pattern, slug) or ".." in slug for slug in slugs):
                    raise ValueError("Company boards must be simple slugs (Workday: company.wdN/site), "
                                     "at most 500 per source")
            return value

    # Nested models are local, so resolve annotations against this function's namespace.
    Settings.model_rebuild(_types_namespace=locals())
    return Settings.model_validate(settings).model_dump()
