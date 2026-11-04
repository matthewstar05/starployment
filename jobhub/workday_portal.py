"""Workday Candidate Home: what each employer's own Workday says about the applications sent there.

Many Workday employers send no email after an application, and some none until a decision.
Their Candidate Home always lists each application with its status ("Application Received",
"Under Consideration", "No Longer Under Consideration"), and whether a task (an assessment, a
questionnaire) is waiting. With ``workday.check_portal`` on, each inbox check also signs in
to the employers that have open applications, using the vault's saved sign-in, at most every
``CHECK_SECONDS`` per employer, and reads that list through the JSON endpoint the page itself
uses. Nothing there is clicked or changed.

What it shows is applied like an email: a stage only moves forward and never over one the
candidate set, and only a change from the last reading is reported. An unconfirmed attempt
(``needs_human`` past the submit boundary) that Candidate Home lists as submitted is the
employer's own record of a received application, so it becomes Applied
(``db.confirm_from_portal``). One listed as a draft stays with the candidate.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse

from . import db, governor, vault

CHECK_SECONDS = 6 * 3600          # per employer, for scheduled checks
DRAFT_SECONDS = 24 * 3600         # an employer where Workday lists only unsubmitted drafts
MANUAL_SECONDS = 15 * 60          # a check the candidate asked for skips employers read this recently
RUN_SECONDS = 20 * 60             # one check's budget; employers left over go first next time
SIGN_IN_SECONDS = 75
PAGE_SIZE = 4                     # Candidate Home's endpoint refuses larger pages
MAX_RECORDS = 200
SETTLED = ("rejected", "withdrawn", "offer")
STEPS = ("apply", "applyManually", "autofillWithResume", "useMyLastApplication", "")

_REJECTED = re.compile(r"no longer (?:under )?consider|not (?:been )?selected|not moving forward|unsuccessful|"
                       r"declin|position (?:has been )?(?:filled|closed|cancel)|requisition (?:closed|cancel)|"
                       r"job (?:closed|cancel)|not (?:under )?consider|rejected|disqualif", re.I)
_WITHDRAWN = re.compile(r"withdr[ae]wn?", re.I)
_OFFER = re.compile(r"\boffer\b|\bhired\b", re.I)
_INTERVIEW = re.compile(r"interview", re.I)
_ASSESSMENT = re.compile(r"assess|\btest\b|evaluation|questionnaire", re.I)

# The page's state: signed in (Candidate Home is showing) or which sign-in control is there.
STATE_JS = r"""
() => {
  const seen = (id) => [...document.querySelectorAll(`[data-automation-id="${id}"]`)].some(e => e.offsetParent);
  const alerts = [...document.querySelectorAll('[role=alert], [data-automation-id*=rror], [data-automation-id*=Alert]')]
    .filter(e => e.offsetParent).map(e => e.innerText).join('\n');
  return {
    home: seen('CandidateHomePage') || seen('applicationsSectionHeading'),
    email: seen('email'), password: seen('password'),
    emailButton: seen('SignInWithEmailButton'), signInLink: seen('utilityButtonSignIn'),
    captcha: !!document.querySelector('iframe[src*="captcha"], iframe[title*="challenge" i]'),
    verify: /verif(?:y|ication)[^.]{0,60}(?:email|account)/i.test(document.body.innerText || ''),
    alerts,
  };
}"""

FETCH_JS = """async ([url, token]) => {
  const response = await fetch(url, {headers: {'Accept': 'application/json', 'x-calypso-csrf-token': token}});
  return [response.status, await response.text()];
}"""


class PortalError(RuntimeError):
    """Candidate Home could not be read for one employer; the others go on."""


@dataclass
class PortalReport:
    checked: list[str] = field(default_factory=list)       # tenants read
    updates: list[dict] = field(default_factory=list)       # {application_id, company, title, stage, status}
    confirmed: list[dict] = field(default_factory=list)     # unconfirmed attempts Workday lists as submitted
    problems: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if not self.checked and not self.problems:
            return "Workday applications are up to date."
        parts = [f"Checked {len(self.checked)} Workday Candidate Home{'s' if len(self.checked) != 1 else ''}"]
        if self.confirmed:
            parts.append(f"{len(self.confirmed)} confirmed as received")
        if self.updates:
            parts.append(", ".join(f"{u['company']}: {u['stage']}" for u in self.updates[:4]))
        return " · ".join(parts) + "."


def site_of(url: str) -> tuple[str, str, str, str] | None:
    """(tenant, host, site, posting anchor) of a Workday posting address, or None.

    ``https://fmr.wd1.myworkdayjobs.com/en-US/FidelityCareers/job/Boston/Analyst_2137506`` is
    ("fmr", "fmr.wd1.myworkdayjobs.com", "FidelityCareers", "Analyst_2137506"); Workday's shared
    ``wd1.myworkdaysite.com/recruiting/fmr/FidelityCareers/job/…`` is the same employer."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").casefold()
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    shared = re.fullmatch(r"(wd\d+)\.myworkdaysite\.com", host)
    if shared and len(parts) > 2 and parts[0].casefold() == "recruiting":
        host = f"{parts[1].casefold()}.{shared.group(1)}.myworkdayjobs.com"
        parts = parts[2:]
    try:
        tenant = vault.tenant_of(host)
    except ValueError:
        return None
    if "job" not in parts:
        return None
    position = parts.index("job")
    before = [part for part in parts[:position] if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", part)]
    after = [part for part in parts[position + 1:] if part not in STEPS]
    if not before or not after:
        return None
    return tenant, host, before[-1], after[-1]


def stage_for(kind: str, record: dict) -> str | None:
    """The hiring stage a Candidate Home row shows, or None for a draft."""
    if record.get("draft"):
        return None
    status = str(record.get("status") or "")
    if _WITHDRAWN.search(status):
        return "withdrawn"
    if _OFFER.search(status):
        return "offer"
    if kind == "inactive" or _REJECTED.search(status):
        return "rejected"
    if _INTERVIEW.search(status):
        return "interview"
    if _ASSESSMENT.search(status) or record.get("hasAwaitingTasks"):
        return "assessment"
    return "applied"


def _same_posting(anchor: str, requisition: str, record: dict) -> bool:
    theirs = str(record.get("jobPostingAnchorId") or "").casefold()
    mine = anchor.casefold()
    if theirs and (mine == theirs or re.sub(r"-\d+$", "", mine) == re.sub(r"-\d+$", "", theirs)):
        return True
    req = str(record.get("jobRequisitionId") or requisition or "").strip()
    return bool(req) and re.search(rf"_{re.escape(req.casefold())}(?:-\d+)?$", mine) is not None


def match(records: list[tuple[str, dict]], apps: list[dict]) -> list[tuple[dict, str, dict]]:
    """(application, kind, record) for each tracked application Candidate Home lists.

    A posting address names its requisition, so that decides; a title is used only when one
    unmatched application and one unmatched row share it. A submitted row beats a draft."""
    pairs: list[tuple[dict, str, dict]] = []
    rows = sorted(records, key=lambda item: bool(item[1].get("draft")))   # submitted first
    taken: set[int] = set()
    for app in apps:
        located = site_of(app.get("apply_url") or "") or site_of(app.get("url") or "")
        if located is None:
            continue
        anchor = located[3]
        for index, (kind, record) in enumerate(rows):
            if index not in taken and _same_posting(anchor, "", record):
                pairs.append((app, kind, record))
                taken.add(index)
                break
    matched = {int(app["id"]) for app, _, _ in pairs}

    def title(text) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", str(text or "").casefold()))
    for app in apps:
        if int(app["id"]) in matched:
            continue
        same = [index for index, (_, record) in enumerate(rows)
                if index not in taken and title(record.get("postingTitle")) == title(app.get("title"))]
        rivals = [other for other in apps if int(other["id"]) not in matched and other is not app
                  and title(other.get("title")) == title(app.get("title"))]
        if len(same) == 1 and not rivals:
            pairs.append((app, *rows[same[0]]))
            taken.add(same[0])
    return pairs


def due(settings: dict, *, manual: bool = False, now: float | None = None) -> dict[str, dict]:
    """Employers whose Candidate Home is worth reading now: {tenant: {host, site, apps}}.

    Only employers with an application still open there (not rejected, withdrawn, or an
    offer), a saved sign-in that works, and no send running there right now, oldest check first."""
    now = time.time() if now is None else now
    wait = MANUAL_SECONDS if manual else CHECK_SECONDS
    employers: dict[str, dict] = {}
    busy: set[str] = set()
    for app in db.workday_applications():
        located = site_of(app.get("apply_url") or "") or site_of(app.get("url") or "")
        if located is None:
            continue
        tenant, host, site, _ = located
        if app["status"] in ("queued", "running"):
            busy.add(tenant)
            continue
        if app["status"] == "applied" and (app.get("stage") or "") in SETTLED:
            continue
        entry = employers.setdefault(tenant, {"host": host, "sites": {}, "apps": [], "open": False})
        entry["sites"][site] = entry["sites"].get(site, 0) + 1
        entry["apps"].append(app)
        state = db.portal_state(int(app["id"]))
        entry["open"] = entry["open"] or not (app["status"] == "needs_human" and state and state["draft"])
    out: dict[str, dict] = {}
    for tenant, entry in employers.items():
        account = vault.get_account(tenant)
        if tenant in busy or not account or account.get("status") != "active":
            continue
        last = db.portal_checked(tenant)
        if last:
            try:
                if now - datetime.fromisoformat(last).timestamp() < (wait if entry["open"] or manual
                                                                     else DRAFT_SECONDS):
                    continue
            except ValueError:
                pass
        site = max(entry["sites"], key=entry["sites"].get)
        out[tenant] = {"host": account.get("host") or entry["host"], "site": site, "apps": entry["apps"],
                       "email": account["email"], "last": last}
    return dict(sorted(out.items(), key=lambda item: item[1]["last"] or ""))


def read_candidate_home(host: str, tenant: str, site: str, email: str, password: str) -> list[tuple[str, dict]]:
    """Sign in and return every Candidate Home row as (``active`` or ``inactive``, record).

    A fresh, private browser context each time: no cookies are kept, and the sending
    browser's profile is never touched."""
    from playwright.sync_api import sync_playwright

    from .apply.engine import _browser_user_agent
    from .apply.workday import INVALID_RE
    token: dict[str, str] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 1000}, locale="en-US",
                                          user_agent=_browser_user_agent(playwright, "chromium"))
            page = context.new_page()

            def watch(request) -> None:
                found = request.headers.get("x-calypso-csrf-token")
                if found and "/wday/cxs/" in request.url:
                    token["csrf"] = found
            page.on("request", watch)
            page.goto(f"https://{host}/en-US/{site}/userHome", wait_until="domcontentloaded", timeout=60000)
            submitted = False
            deadline = time.monotonic() + SIGN_IN_SECONDS
            while True:
                if time.monotonic() > deadline:
                    raise PortalError("Candidate Home did not open after signing in")
                try:
                    state = page.evaluate(STATE_JS)
                except Exception:  # noqa: BLE001 - a navigation replaced the document: look again
                    page.wait_for_timeout(1000)
                    continue
                if state["home"] and token.get("csrf"):
                    break
                if state["captcha"]:
                    raise PortalError("Workday asked for a security check")
                if submitted and INVALID_RE.search(state["alerts"] or ""):
                    vault.set_status(tenant, "failed", "Workday did not accept the saved password")
                    raise PortalError("Workday did not accept the saved password")
                if submitted and state["verify"] and not state["home"]:
                    raise PortalError("Workday wants this account's email verified first")
                if state["emailButton"]:
                    page.locator("[data-automation-id=SignInWithEmailButton]").first.click(timeout=4000)
                elif state["email"] and state["password"] and not submitted:
                    page.locator("input[data-automation-id=email]:visible").first.fill(email)
                    page.locator("input[data-automation-id=password]:visible").first.fill(password)
                    button = page.locator("[data-automation-id=signInSubmitButton]:visible").first
                    try:
                        button.click(timeout=4000)
                    except Exception:  # noqa: BLE001 - Workday lays a click_filter over its buttons
                        button.click(timeout=4000, force=True)
                    submitted = True
                elif state["signInLink"] and not state["home"] and not submitted:
                    page.locator("[data-automation-id=utilityButtonSignIn]").first.click(timeout=4000)
                page.wait_for_timeout(1000)
            records: list[tuple[str, dict]] = []
            base = f"https://{host}/wday/cxs/{tenant}/{site}/applications"
            for kind in ("active", "inactive"):
                offset = 0
                while offset < MAX_RECORDS:
                    status, text = page.evaluate(FETCH_JS, [f"{base}?type={kind}&offset={offset}&limit={PAGE_SIZE}",
                                                            token["csrf"]])
                    if status != 200:
                        raise PortalError(f"Candidate Home answered HTTP {status}")
                    data = json.loads(text or "{}")
                    rows = [row for row in data.get("data") or [] if isinstance(row, dict)]
                    records += [(kind, row) for row in rows]
                    offset += len(rows)
                    if not rows or offset >= int(data.get("total") or 0):
                        break
            return records
        finally:
            browser.close()


def _describe(record: dict) -> str:
    status = str(record.get("status") or "").strip() or ("Not Submitted" if record.get("draft") else "Submitted")
    return status + ("; a task is waiting for you" if record.get("hasAwaitingTasks") else "")


def _apply(tenant: str, apps: list[dict], records: list[tuple[str, dict]], report: PortalReport) -> None:
    for app, kind, record in match(records, apps):
        app_id = int(app["id"])
        stage = stage_for(kind, record)
        status = _describe(record)
        if not db.save_portal_state(app_id, tenant, status, bool(record.get("draft")),
                                    bool(record.get("hasAwaitingTasks")), stage or ""):
            continue   # unchanged since the last reading: nothing to report or redo
        label = f"{app.get('company')} · {app.get('title')}"
        if stage is None:
            continue   # a draft: nothing reached the employer, and Needs review already says so
        if app["status"] == "needs_human" and db.confirm_from_portal(app_id, status):
            report.confirmed.append({"application_id": app_id, "company": app.get("company"),
                                     "title": app.get("title"), "status": status})
            db.add_event(f"Workday confirmed {label}: its Candidate Home lists it as {status}", "success",
                         app.get("job_id"))
        try:
            changed = db.set_application_stage(app_id, stage, "workday")
        except ValueError:
            changed = False
        if changed:
            report.updates.append({"application_id": app_id, "company": app.get("company"), "title": app.get("title"),
                                   "stage": stage, "status": status})
            db.add_event(f"Workday update: {label} → {stage} (Candidate Home: {status})",
                         "success" if stage in ("assessment", "interview", "offer") else "info", app.get("job_id"))


def check(settings: dict | None = None, *, manual: bool = False, reader=None, stop=None) -> PortalReport:
    """Read each due employer's Candidate Home and apply what it shows.

    ``reader`` replaces ``read_candidate_home`` in tests; ``stop()`` ends the run early (the
    app is quitting). One employer's failure never ends the run."""
    settings = settings or db.get_settings()
    report = PortalReport()
    if not (settings.get("workday") or {}).get("check_portal", True):
        return report
    reader = reader or read_candidate_home
    started = time.monotonic()
    for tenant, entry in due(settings, manual=manual).items():
        if (stop and stop()) or time.monotonic() - started > RUN_SECONDS:
            break
        if not manual and report.checked and governor.busy():
            break   # the Mac is busy (sends, a scan): the rest wait for the next check
        password = vault.password_for(tenant)
        if not password:
            continue
        try:
            records = reader(entry["host"], tenant, entry["site"], entry["email"], password)
        except Exception as error:  # noqa: BLE001 - one employer's Workday never ends the check
            message = str(error).splitlines()[0][:160] if str(error) else type(error).__name__
            db.record_portal_check(tenant, f"error: {message}")
            report.problems.append(f"{tenant}: {message}")
            continue
        db.record_portal_check(tenant, f"ok: {len(records)} application(s)")
        report.checked.append(tenant)
        _apply(tenant, entry["apps"], records, report)
    if report.problems:
        db.add_event("Workday check could not read " + "; ".join(report.problems[:5])
                     + (f" and {len(report.problems) - 5} more" if len(report.problems) > 5 else ""), "warn")
    return report


def last_checked(hours: int = 24) -> int:
    """How many employers' Candidate Homes were read in the last ``hours``."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    with db.reader() as c:
        return int(c.execute("SELECT COUNT(*) FROM workday_portal_checks WHERE checked_at >= ? "
                             "AND outcome LIKE 'ok:%'", (since,)).fetchone()[0])
