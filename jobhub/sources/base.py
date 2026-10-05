"""Shared helpers for sources: HTTP client, HTML stripping, title filters, salary parsing."""
from __future__ import annotations

import html
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx

from ..config import DEFAULT_SETTINGS


# A missing bundled board is normally an employer that changed ATS providers.
# Retry daily so a board that comes back is discovered without restarting the app.
_MISSING_BOARD_RETRY_SECONDS = 24 * 60 * 60
_missing_default_boards: dict[tuple[str | None, str], float] = {}
_missing_boards_lock = threading.Lock()


class SourceJobs(list):
    """List-compatible result with observable partial source failures.

    ``board_results`` maps each discovered board that was read to its job
    count, or None when it could not be read, so the scheduler can rotate and
    rest boards.
    """

    def __init__(self, jobs=(), warnings=(), notices=()):
        super().__init__(jobs)
        self.warnings = list(warnings)
        self.notices = list(notices)
        self.board_results: dict[str, int | None] = {}


def discovered_boards(settings: dict, source: str) -> list[str]:
    """Boards found in other listings that this scan should also read (see scheduler.scan_once)."""
    return list((settings.get("_discovered_boards") or {}).get(source) or [])


def fetch_boards(slugs: list[str], fetch_one, settings: dict, *, source: str | None = None,
                 discovered: list[str] | None = None) -> SourceJobs:
    """Read every configured board, then any discovered ones.

    Configured boards report failures as warnings. Discovered boards are best
    effort: their failures are counted in one notice and recorded in
    ``board_results`` instead of making the scan look broken.
    """
    defaults = set(DEFAULT_SETTINGS["companies"].get(source, [])) if source else set()
    unique_slugs = list(dict.fromkeys(slugs))
    configured = {slug.casefold() for slug in unique_slugs}
    extra = [slug for slug in dict.fromkeys(discovered or []) if slug.casefold() not in configured]

    def attempt(slug):
        key = (source, slug)
        if slug in defaults:
            with _missing_boards_lock:
                if _missing_default_boards.get(key, 0) > time.monotonic():
                    return [], None, None, True, False
        try:
            return fetch_one(slug, settings), None, None, False, True
        except httpx.HTTPStatusError as exc:
            if slug in defaults and exc.response.status_code == 404:
                with _missing_boards_lock:
                    _missing_default_boards[key] = time.monotonic() + _MISSING_BOARD_RETRY_SECONDS
                return [], None, slug, False, False
            return [], f"{slug}: {type(exc).__name__}: {str(exc)[:180]}", None, False, False
        except Exception as exc:
            return [], f"{slug}: {type(exc).__name__}: {str(exc)[:180]}", None, False, False

    def attempt_discovered(slug):
        try:
            return fetch_one(slug, settings)
        except Exception:  # noqa: BLE001 - recorded in board_results
            return None

    result = SourceJobs()
    missing: list[str] = []
    cached = 0
    successful = 0
    with ThreadPoolExecutor(max_workers=8) as executor:
        for jobs, warning, absent, was_cached, ok in executor.map(attempt, unique_slugs):
            result.extend(jobs)
            successful += ok
            cached += was_cached
            if warning:
                result.warnings.append(warning)
            if absent:
                missing.append(absent)
        for slug, jobs in zip(extra, executor.map(attempt_discovered, extra)):
            result.board_results[slug] = None if jobs is None else len(jobs)
            if jobs is not None:
                result.extend(jobs)
                successful += 1
    if missing:
        names = ', '.join(missing[:5])
        if len(missing) > 5:
            names += f" and {len(missing) - 5} more"
        result.notices.append(f"Bundled boards unavailable (404), retrying within 24 hours: {names}")
    if cached:
        result.notices.append(f"{cached} bundled board(s) in a 24-hour retry window")
    if extra:
        read = sum(found is not None for found in result.board_results.values())
        result.notices.append(f"Read {read} of {len(extra)} discovered board(s)")
    if (unique_slugs or extra) and not successful:
        detail = "; ".join((result.warnings + result.notices)[:3])
        if not detail:
            detail = "Bundled boards are in their 24-hour retry window"
        raise RuntimeError("All company boards failed. " + detail)
    return result

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 Starployment/0.4"


def client(timeout: float = 25.0, *, follow_redirects: bool = True) -> httpx.Client:
    return httpx.Client(timeout=timeout, headers={"User-Agent": UA, "Accept": "application/json,text/html"},
                        follow_redirects=follow_redirects)


# Greenhouse postings listed on a company's own careers page ("seatgeek.com/jobs/123?gh_jid=123")
# belong to a Greenhouse board the link does not name. Given only the job id, Greenhouse's
# application embed redirects to "?for=<board>", which makes the posting preparable like
# any board posting. Answers are cached for the life of the process ("" = no such job).
_GREENHOUSE_BOARD_OF: dict[str, str] = {}
_BOARD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _greenhouse_board_for(job_id: str) -> str | None:
    if job_id in _GREENHOUSE_BOARD_OF:
        return _GREENHOUSE_BOARD_OF[job_id]
    try:
        with client(15.0) as http:
            response = http.get(f"https://boards.greenhouse.io/embed/job_app?token={job_id}")
    except httpx.HTTPError:
        return None   # try again in a later scan
    from urllib.parse import parse_qs, urlsplit

    board = (parse_qs(urlsplit(str(response.url)).query).get("for") or [""])[0] if response.status_code == 200 else ""
    _GREENHOUSE_BOARD_OF[job_id] = board if _BOARD_NAME.match(board) else ""
    return _GREENHOUSE_BOARD_OF[job_id]


def resolve_hosted_greenhouse(jobs: list, workers: int = 8) -> int:
    """Point careers-page links that carry ``gh_jid`` at their Greenhouse board. Returns how many."""
    from urllib.parse import parse_qs, urlsplit

    pending: dict[str, list] = {}
    for job in jobs:
        if job.ats in ("greenhouse", "lever", "ashby"):
            continue
        for url in (job.apply_url, job.url):
            job_id = (parse_qs(urlsplit(url or "").query).get("gh_jid") or [""])[0]
            if job_id.isdigit():
                pending.setdefault(job_id, []).append(job)
                break
    if not pending:
        return 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        boards = dict(zip(pending, pool.map(_greenhouse_board_for, pending)))
    resolved = 0
    for job_id, board in boards.items():
        if not board:
            continue
        for job in pending[job_id]:
            job.ats = "greenhouse"
            job.apply_url = f"https://job-boards.greenhouse.io/{board}/jobs/{job_id}"
            job.raw = {**(job.raw or {}), "slug": board, "gh_id": job_id}
            resolved += 1
    return resolved


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    # Greenhouse's content can contain escaped HTML, including twice-escaped
    # entities. Decode before removing tags so descriptions stay readable.
    for _ in range(2):
        decoded = html.unescape(s)
        if decoded == s:
            break
        s = decoded
    s = re.sub(r"<(br|/p|/div|/li|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def title_wanted(title: str, settings: dict) -> bool:
    """Cheap pre-filter: drop obviously senior/management titles and (optionally) internships."""
    t = title.lower()
    for w in settings.get("exclude_title_words", []):
        if w.strip() and re.search(r"(?<!\w)" + re.escape(w.lower().strip()) + r"(?!\w)", t):
            return False
    if not settings.get("include_internships") and re.search(r"\bintern(ship)?\b|co-op", t):
        return False
    return True


def looks_technical(title: str) -> bool:
    if re.search(r"sales|account executive|recruit|marketing|customer success|solutions consultant|legal|"
                 r"counsel|finance manager|accountant|designer|writer|support specialist", title, re.I):
        return False
    return bool(re.search(r"engineer|developer|software|\bswe\b|quant|scientist|programmer|researcher|"
                          r"\bml\b|machine learning|data (analyst|engineer)|technologist", title, re.I))


def title_relevant(title: str, settings: dict) -> bool:
    """Search for the candidate's own roles, whatever their profession.

    A target list is required by desktop setup. Broad job boards and company
    feeds may expose every department, so discovery filters before scoring.
    """
    if not title_wanted(title, settings):
        return False
    targets = [t.strip().casefold() for t in settings.get("target_titles", []) if t.strip()]
    if not targets:
        return False
    candidate = title.casefold()
    words = set(re.findall(r"[\w+#.]+", candidate))
    for target in targets:
        if target in candidate:
            return True
        sought = set(re.findall(r"[\w+#.]+", target)) - {"new", "grad", "junior", "senior", "entry", "level"}
        if sought and len(sought & words) / len(sought) >= 0.6:
            return True
    return False


SAL_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d{2,7})(?:\.\d+)?\s*(k)?(?!\d)", re.I)


def parse_salary(text: str | None) -> tuple[int | None, int | None]:
    """Find a plausible annual USD range in free text. Returns (min, max) or (None, None)."""
    if not text:
        return None, None
    vals: list[int] = []
    for m in SAL_RE.finditer(text[:20000]):
        whole, k = m.group(1), m.group(2)
        n = int(whole.replace(",", "")) * (1000 if k else 1)
        # Bare $45 can be an hourly rate or expense; never call it $45,000.
        nearby = text[max(0, m.start() - 45):m.end() + 45].lower()
        if re.search(r"funding|raised|sign.on|bonus|equity|stock", nearby):
            continue
        if 30000 <= n <= 1000000:
            vals.append(n)
    if not vals:
        return None, None
    return min(vals), max(vals)


def iso(dt: datetime | str | int | float | None) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        if dt > 1e12:
            dt = dt / 1000
        return datetime.fromtimestamp(dt, tz=timezone.utc).isoformat(timespec="seconds")
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    return str(dt)
