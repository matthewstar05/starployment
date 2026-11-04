"""Workday applications: sign in (or create the account), fill each step, move on.

Workday puts every application behind an account on the employer's own
tenant. With Settings → Workday → Manage Workday sign-ins on
(``settings.workday.create_accounts``), Starployment creates that account with
a generated password kept in the local vault (``vault.py``), signs in with the
saved one later, and saves the password the candidate types when they sign in
themselves. It never solves a CAPTCHA or security check.

Finish in browser (``assist``) fills each step as it appears in a visible
window; the candidate presses Save and Continue and Submit. With
``settings.workday.send`` on, Approve & send, Send ready, and automatic runs
come here too: a step is saved (Save and Continue) only when every required
question on it has an answer from saved facts, the review page's Submit is
pressed, and any open required question stops the attempt (``needs_human``).
Practice runs sign in with a saved account and fill the first step, but save
nothing and create no account.

Workday marks its controls with ``data-automation-id`` attributes, which are
stable across tenants; the selectors below rely on them.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import date

from playwright.sync_api import Page

from .. import db, vault
from ..answers import Q, pick_option, rule_answer
from ..models import ApplyResult
from .engine import ASSIST_SECONDS, FormEngine

# Workday's confirmation: a dialog or heading after Submit.
SUCCESS_RE = re.compile(r"application (?:has been |was )?(?:successfully )?submitted|thank you for applying|"
                        r"you(?:'ve| have) (?:successfully )?applied|congratulations", re.I)
# Controls Starployment must never press: the candidate moves between pages and submits.
FORBIDDEN_RE = re.compile(r"submit|save and continue|^next$|continue|sign in|create account|delete|withdraw", re.I)
# Account pages: Workday's own messages after Create Account or Sign In.
ALREADY_RE = re.compile(r"already (?:in use|exists|registered|been used)|account (?:with this email )?already", re.I)
INVALID_RE = re.compile(r"(?:wrong|invalid|incorrect) (?:email|user ?name|password|credentials)|could not sign|"
                        r"sign.?in (?:failed|unsuccessful)|account (?:is |has been )?locked", re.I)
VERIFY_RE = re.compile(r"verif(?:y|ication)[^.]{0,60}(?:email|account)|(?:email|account)[^.]{0,40}verif|"
                       r"activate your account", re.I)
# A posting Workday no longer shows: its "page doesn't exist" card or a closed-job notice.
CLOSED_RE = re.compile(r"page you are looking for (?:doesn.t|does not) exist|job (?:posting )?(?:you are looking for )?"
                       r"(?:is )?no longer (?:available|accepting|open|exists)|no longer accepting applications|"
                       r"(?:position|job) has been (?:filled|closed)|this job (?:is not|isn.t) available", re.I)
SIGN_IN_STEP_RE = re.compile(r"create account|sign.?in", re.I)
READY_SECONDS = 45
SETTLE_POLLS = 40          # up to ~40 s for a step to finish loading before it is read         # how long a posting page may take to show its Apply button or a sign-in
NEXT_RE = re.compile(r"^(?:save and continue|next|continue)$", re.I)
SUBMIT_RE = re.compile(r"^submit$", re.I)
WORKDAY_HOSTS = ("myworkdayjobs.com", "myworkday.com", "myworkdaysite.com")
SEND_SECONDS = 8 * 60      # an unattended send, including a verification email
# Account sign-ups that may wait for a verification email take turns: with sends side by side,
# two fresh Workday emails at once could otherwise cross (each link opened for the wrong account).
_SIGNUP_LOCK = threading.Lock()
SIGNUP_WAIT_SECONDS = 6 * 60
VERIFY_SECONDS = 4 * 60     # Workday's activation emails can take a few minutes
# Workday labels that mean a standard question under another name.
ALIASES = {"country phone code": "Phone country code", "phone extension": "Phone extension",
           "address line 1": "Address Line 1", "postal code": "Postal Code"}

STATE_JS = r"""
() => {
  const visible = e => { if (!e) return false; const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const q = s => [...document.querySelectorAll(s)].find(visible);
  const active = q('[data-automation-id=progressBarActiveStep]');
  const heading = q('[data-automation-id=applyFlowPage] h2, [data-automation-id=applyFlowPage] h3');
  const dialogs = [...document.querySelectorAll('[role=dialog],[role=alertdialog],[data-automation-id=applyFlowPage] h2,h1')]
    .filter(visible).map(e => e.innerText).join('\n').slice(0, 4000);
  return {
    apply: !!q('[data-automation-id=adventureButton]'),
    start: ['useMyLastApplication', 'autofillWithResume', 'applyManually']
      .filter(id => q(`[data-automation-id=${id}]`)),
    signIn: !!(q('[data-automation-id=signInContent]') || q('[data-automation-id=createAccountLink]') ||
               q('input[data-automation-id=password]') || q('input[type=password]')),
    step: ((active && active.innerText) || (heading && heading.innerText) || '').replace(/\s+/g, ' ').trim(),
    // Workday hides its resume input behind a "Select files" button: any file input on the step counts.
    upload: !!document.querySelector('[data-automation-id=applyFlowPage] input[type=file]'),
    resumeStep: /experience|resume|cv\b|document/i.test(((active && active.innerText) || '')),
    createForm: !!q('input[data-automation-id=verifyPassword]'),
    passwordForm: !!(q('input[data-automation-id=password]') || q('input[type=password]')),
    emailButton: !!q('[data-automation-id=SignInWithEmailButton]'),
    captcha: !!([...document.querySelectorAll('iframe')].find(f =>
      /hcaptcha|recaptcha|turnstile|arkose|funcaptcha/i.test(f.src) && !/size=invisible/.test(f.src) && visible(f))),
    alerts: [...document.querySelectorAll('[data-automation-id=errorMessage],[data-automation-id=alertMessage],[role=alert]')]
      .filter(visible).map(e => e.innerText).join('\n').slice(0, 800),
    next: ((b => b ? b.innerText : '')(q('[data-automation-id=bottom-navigation-next-button],[data-automation-id=pageFooterNextButton]'))).trim(),
    // Workday draws a step's heading and buttons first, then loads its fields behind a spinner
    // with Save and Continue greyed out; nothing on the step is read until that settles.
    busy: !!(q('[data-automation-id*=oading]') || q('[data-automation-id*=pinner]') || q('[aria-busy=true]') ||
             q('[role=progressbar]:not(ol):not(li)') ||
             (b => b && (b.disabled || b.getAttribute('aria-disabled') === 'true'))(
               q('[data-automation-id=bottom-navigation-next-button],[data-automation-id=pageFooterNextButton]'))),
    fields: document.querySelectorAll('[data-automation-id=applyFlowPage] [data-automation-id^=formField-]').length,
    dialogs,
  };
}
"""

FIELDS_JS = r"""
() => {
  const visible = e => { if (!e) return false; const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const clean = t => (t || '').replace(/\s+/g, ' ').replace(/[*✱]/g, '').trim().slice(0, 400);
  const root = document.querySelector('[data-automation-id=applyFlowPage]') || document.body;
  const out = [];
  // Which Work Experience block a field sits in: a "workExperience-N" container, else the nearest
  // section headed "Work Experience N", else block 1 when the page has a single Job Title field.
  const titles = [...root.querySelectorAll('[data-automation-id^=formField-]')].filter(b =>
    /^job title/i.test(clean((b.querySelector('label, legend') || {}).innerText)));
  const experienceGroup = box => {
    for (let e = box.parentElement; e && e !== root; e = e.parentElement) {
      const id = e.getAttribute('data-automation-id') || '';
      const named = id.match(/^workExperience-(\d+)$/);
      if (named) return 'workExperience-' + named[1];
      const head = [...e.children].find(c => /^(H[2-5]|LEGEND)$/.test(c.tagName) ||
        (c.children[0] && /^(H[2-5]|LEGEND)$/.test(c.children[0].tagName)));
      const text = head ? head.innerText : '';
      const numbered = text.match(/work experience\s*(\d+)/i);
      if (numbered) return 'workExperience-' + numbered[1];
      if (/^education\b|^language|^website|^skill/i.test(text)) return '';
    }
    if (titles.length !== 1) return '';
    // The smallest container holding both this field and the one Job Title field; never one
    // that also holds Education (whose From/To are school years).
    let shared = titles[0].parentElement;
    while (shared && shared !== root && !shared.contains(box)) shared = shared.parentElement;
    if (!shared || shared === root) return '';
    const education = [...shared.querySelectorAll('[data-automation-id^=formField-]')].some(b =>
      /school|university|degree|field of study/i.test((b.getAttribute('data-automation-id') || '') + ' ' +
        ((b.querySelector('label, legend') || {}).innerText || '')));
    return education ? '' : 'workExperience-1';
  };
  for (const box of root.querySelectorAll('[data-automation-id^=formField-]')) {
    if (!visible(box) || box.parentElement.closest('[data-automation-id^=formField-]')) continue;
    if (!box.dataset.jhwd) box.dataset.jhwd = String(window.__jhwd = (window.__jhwd || 0) + 1);
    const labelEl = box.querySelector('label, legend');
    const raw = labelEl ? labelEl.innerText : (box.getAttribute('aria-label') || '');
    const rec = {id: box.dataset.jhwd, automation: box.getAttribute('data-automation-id').slice(10),
                 label: clean(raw), required: /\*/.test(raw) || !!box.querySelector('[aria-required=true],[required]'),
                 kind: '', value: '', empty: true, options: [],
                 group: experienceGroup(box), ancestors: ''};
    rec.ancestors = [...(function*(e){ while ((e = e.parentElement) && e !== root) {
      const a = e.getAttribute('data-automation-id') || e.getAttribute('role') || ''; if (a) yield a; } })(box)]
      .slice(0, 5).join(' < ');
    const dropdown = box.querySelector('button[aria-haspopup=listbox]');
    const prompt = box.querySelector('[data-automation-id=multiselectInputContainer] input, input[data-uxi-widget-type=selectinput]');
    const date = box.querySelector('[data-automation-id=dateSectionMonth-input], [data-automation-id=dateSectionYear-input]');
    const radios = [...box.querySelectorAll('input[type=radio]')];
    const boxes = [...box.querySelectorAll('input[type=checkbox]')];
    const text = box.querySelector('textarea, input:not([type=radio]):not([type=checkbox]):not([type=file]):not([type=hidden])');
    if (date) {
      rec.kind = 'date';
      rec.empty = ![...box.querySelectorAll('input')].some(i => i.value && !/^(MM|DD|YYYY)$/.test(i.value));
    } else if (dropdown) {
      rec.kind = 'dropdown';
      rec.value = clean(dropdown.innerText);
      rec.empty = !rec.value || /^select one$/i.test(rec.value);
    } else if (prompt) {
      rec.kind = 'prompt';
      const chosen = [...box.querySelectorAll('[data-automation-id=selectedItem]')].map(e => clean(e.innerText));
      rec.value = chosen.join('; ');
      rec.empty = !chosen.length;
    } else if (radios.length) {
      rec.kind = 'radio';
      rec.options = radios.map(r => clean(r.labels?.[0]?.innerText || r.getAttribute('aria-label') || r.value));
      rec.empty = !radios.some(r => r.checked);
    } else if (boxes.length > 1) {
      rec.kind = 'checkboxes';
      rec.options = boxes.map(r => clean(r.labels?.[0]?.innerText || r.getAttribute('aria-label') || ''));
      rec.empty = !boxes.some(r => r.checked);
    } else if (boxes.length === 1) {
      rec.kind = 'checkbox';
      rec.empty = !boxes[0].checked;
      if (!rec.label) rec.label = clean(boxes[0].labels?.[0]?.innerText || '');
    } else if (text) {
      rec.kind = text.tagName === 'TEXTAREA' ? 'textarea' : 'text';
      rec.value = text.value || '';
      rec.empty = !rec.value.trim();
    } else continue;
    out.push(rec);
  }
  return out;
}
"""

BANNER_JS = """(message) => {
  let bar = document.getElementById('starployment-handoff');
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'starployment-handoff';
    bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;padding:10px 16px;'
      + 'font:600 14px -apple-system,system-ui,sans-serif;color:#1d1a12;background:#f3d27a;'
      + 'box-shadow:0 2px 8px rgba(0,0,0,.2);pointer-events:none';
    document.body.appendChild(bar);
  }
  bar.textContent = message;
}"""


class WorkdayEngine(FormEngine):
    """Fills each Workday step as it appears; the candidate signs in, moves on, and submits."""

    def _box(self, page: Page, field: dict):
        return page.locator(f'[data-jhwd="{field["id"]}"]')

    def _press(self, locator) -> None:
        """Click a control that only opens or chooses something, never one that sends or navigates."""
        text = (locator.inner_text(timeout=2000) or "").strip()
        if FORBIDDEN_RE.search(text):
            raise RuntimeError(f"Refused to press {text[:40]!r}: the candidate presses it")
        locator.click(timeout=5000)

    def _banner(self, page: Page, message: str) -> None:
        try:
            page.evaluate(BANNER_JS, message)
        except Exception:  # noqa: BLE001 - a navigation replaced the document
            pass

    def _confirmation_signals(self, page: Page) -> set[str]:
        try:
            text = page.evaluate(STATE_JS)["dialogs"]
        except Exception:  # noqa: BLE001
            return set()
        return {m.group(0).casefold() for m in SUCCESS_RE.finditer(text)}

    def _answer(self, field: dict, options: list[str]) -> object:
        label = ALIASES.get(field["label"].casefold(), field["label"])
        kind = {"dropdown": "select", "prompt": "select", "checkboxes": "select"}.get(field["kind"], field["kind"])
        answer = rule_answer(Q(label=label, kind=kind, options=options, required=field["required"],
                               name=field["automation"]), self.profile, self.settings, self.job)
        if answer is None and field["label"].casefold() == "country phone code":
            country = rule_answer(Q(label="Country", kind="select", options=options), self.profile, self.settings,
                                  self.job)
            return country.value if country else None
        return None if answer is None else answer.value

    def _open_options(self, page: Page, field: dict) -> list[str]:
        self._press(self._box(page, field).locator("button[aria-haspopup=listbox]").first)
        page.wait_for_timeout(400)
        return [o.strip() for o in page.locator("[role=listbox] [role=option]:visible").all_inner_texts() if o.strip()]

    def _choose(self, page: Page, text: str) -> bool:
        for option in page.locator("[role=listbox] [role=option]:visible, [data-automation-id=promptOption]:visible").all():
            if (option.inner_text() or "").strip() == text:
                self._press(option)
                return True
        return False

    # ------------------------------------------------------------- work experience
    def _experience_entry(self, field: dict):
        """The resume entry a Work Experience block stands for (block N is the Nth listed role)."""
        match = re.match(r"workExperience-(\d+)", field.get("group") or "")
        if not match:
            return None
        entries = [e for e in (self.profile.experience or []) if not e.hidden and (e.title or e.company)]
        index = int(match.group(1)) - 1
        return entries[index] if 0 <= index < len(entries) else None

    @staticmethod
    def _month_year(text: str) -> tuple[int, int] | None:
        months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
        found = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{4})", text or "") or re.search(r"(\d{1,2})/(\d{4})", text or "")
        if not found:
            return None
        head = found.group(1)
        month = int(head) if head.isdigit() else (months.index(head[:3].casefold()) + 1
                                                  if head[:3].casefold() in months else 0)
        return (month, int(found.group(2))) if 1 <= month <= 12 else None

    def _type_date(self, page: Page, box, parts: list[tuple[str, str]]) -> bool:
        """Type a Workday date one section at a time. A display layer covers the inputs, so each is
        focused directly; Workday moves on to the next section by itself, which a fresh focus allows."""
        typed = False
        for part, value in parts:
            section = box.locator(f"[data-automation-id=dateSection{part}-input]")
            if not section.count():
                continue
            target = section.first
            try:
                target.click(timeout=2500)
            except Exception:  # noqa: BLE001 - covered by Workday's display layer
                target.focus()
            page.keyboard.type(value, delay=60)
            typed = True
        page.wait_for_timeout(300)
        return typed and bool(box.locator("[data-automation-id^=dateSection][data-automation-id$=-input]").evaluate_all(
            "els => els.some(e => e.value && !/^(MM|DD|YYYY)$/.test(e.value))"))

    def _fill_experience(self, page: Page, field: dict, entry) -> bool | None:
        """Fill one Work Experience field from the candidate's own resume entry (None: not one of them)."""
        label = field["label"].casefold().strip()
        current = bool(re.match(r"present|current|now", (entry.end or "").strip(), re.I))
        box = self._box(page, field)
        text = {"job title": entry.title, "company": entry.company, "location": entry.location,
                "role description": "\n".join(f"• {b}" for b in entry.bullets[:6])}.get(label)
        if text is not None:
            if not text.strip() or field["kind"] not in ("text", "textarea"):
                return False
            box.locator("textarea, input:not([type=radio]):not([type=checkbox]):not([type=file])").first.fill(text)
            return True
        if label.startswith("i currently work here"):
            if current:
                box.locator("input[type=checkbox]").first.check(force=True)
                return True
            return False
        if label in ("from", "to", "start date", "end date") and field["kind"] == "date":
            if label in ("to", "end date") and current:
                return False
            parts = self._month_year(entry.start if label in ("from", "start date") else entry.end)
            if not parts:
                return False
            return self._type_date(page, box, [("Month", f"{parts[0]:02d}"), ("Year", str(parts[1]))])
        return None

    LANGUAGE_SKILLS = re.compile(r"^(?:listening|oral|speaking|oral/speaking|reading|writing|overall|comprehension|"
                                 r"fluency|proficiency)$", re.I)
    TOP_LEVELS = (r"native", r"fluent|bilingual", r"expert|advanced|full professional", r"proficient")

    def _language_answer(self, field: dict, options: list[str]) -> str | None:
        """Workday's Languages block, from the candidate's saved native language (Profile → Answers)."""
        answers = self.settings.get("answers") or {}
        language = str(answers.get("native_language") or "").strip()
        if not language or "native_language" not in (answers.get("confirmed_fields") or []):
            return None
        label = field["label"].strip()
        if re.fullmatch(r"language", label, re.I):
            return language
        if self.LANGUAGE_SKILLS.match(label):
            for level in self.TOP_LEVELS:
                found = [o for o in options if re.search(level, o, re.I)]
                if found:
                    return found[0]
        return None

    def _luna_answer(self, field: dict) -> str | None:
        """An open text question while JARVIS ALL or ALL+ is on: Luna writes it from the resume, as AI
        Autofill does on other forms (the candidate's standing permission). Never a sensitive
        question (eligibility, salary, legal, demographic: luna.SENSITIVE_RE), never a practice
        run, and the answer stays with this application only."""
        from .. import jarvis
        from ..luna import SENSITIVE_RE, autofill_answers
        if self.assist or self.dry_run or not jarvis.sweeping(self.settings):
            return None
        if not field["label"] or SENSITIVE_RE.search(field["label"]):
            return None
        if not field["required"] and field["kind"] != "textarea":
            return None
        question = {"id": f"workday-{field['id']}", "label": field["label"],
                    "kind": "textarea" if field["kind"] == "textarea" else "text",
                    "required": bool(field["required"]), "options": []}
        try:
            filled = autofill_answers(self.job, self.profile, self.settings, [question])
        except Exception as exc:  # noqa: BLE001 - the question stays open for the candidate
            self._log(f"Luna could not answer {field['label'][:50]}: {str(exc)[:120]}")
            return None
        answer = str((filled.get(question["id"]) or {}).get("answer") or "").strip()
        if answer:
            self._log(f"Luna answered {field['label'][:60]} (AI Autofill, this application only)")
        return answer or None

    def _fill_field(self, page: Page, field: dict) -> bool:
        box = self._box(page, field)
        kind = field["kind"]
        if kind in ("dropdown", "prompt") and (re.fullmatch(r"language", field["label"].strip(), re.I)
                                               or self.LANGUAGE_SKILLS.match(field["label"].strip())):
            options = self._open_options(page, field) if kind == "dropdown" else []
            value = self._language_answer(field, options)
            if kind == "dropdown":
                if value and self._choose(page, value):
                    return True
                if value and pick_option(options, value) and self._choose(page, pick_option(options, value)):
                    return True
                page.keyboard.press("Escape")
                return False
            if value:
                search = box.locator("input").first
                search.click()
                search.press_sequentially(value, delay=30)
                search.press("Enter")
                page.wait_for_timeout(1000)
                options = list(dict.fromkeys(o.strip() for o in page.locator(
                    "[data-automation-id=promptOption]:visible").all_inner_texts() if o.strip()))
                chosen = pick_option(options, value) or next((o for o in options if o.casefold() == value.casefold()), None)
                if chosen and self._choose(page, chosen):
                    return True
                page.keyboard.press("Escape")
            return False
        entry = self._experience_entry(field)
        if entry is not None:
            done = self._fill_experience(page, field, entry)
            if done is not None:
                return done
        if kind == "date":
            # Only the self-identification form's signature date is a date Starployment knows: today.
            if not re.search(r"^date$|today", field["label"], re.I):
                return False
            today = date.today()
            return self._type_date(page, box, [("Month", f"{today.month:02d}"), ("Day", f"{today.day:02d}"),
                                               ("Year", str(today.year))])
        if kind == "dropdown":
            options = self._open_options(page, field)
            value = self._answer(field, options)
            if value is None and field["label"]:
                # A saved answer that is not one of this list's options word for word ("LinkedIn"
                # where the list says "LinkedIn.com"): ask without options, then find the one option
                # that names it.
                free = self._answer(field, [])
                if isinstance(free, str) and free.strip():
                    wanted = free.strip().casefold()
                    naming = sorted((o for o in options if wanted in o.casefold()), key=len)
                    if naming and (len(naming) == 1 or len(naming[0]) < len(naming[1])):
                        value = naming[0]
                    else:
                        self._log(f"{field['label'][:50]}: no option for {free!r} among {options[:12]}")
            chosen = pick_option(options, str(value)) if isinstance(value, str) else None
            if chosen and self._choose(page, chosen):
                return True
            if field["required"]:
                self._log(f"{field['label'][:50]}: no saved answer among {[o for o in options if o != 'Select One'][:10]}")
            page.keyboard.press("Escape")
            return False
        if kind == "prompt":
            value = self._answer(field, [])
            if not isinstance(value, str) or not value.strip():
                return False
            # Other pickers' chosen items are promptOptions too ("United States of America (+1)").
            taken = set(page.evaluate("""() => [...document.querySelectorAll('[data-automation-id=selectedItem]')]
                .map(e => (e.innerText || '').trim())"""))
            search = box.locator("input").first
            search.click()
            search.press_sequentially(value, delay=30)
            search.press("Enter")
            page.wait_for_timeout(1200)
            options = list(dict.fromkeys(o.strip() for o in page.locator("[data-automation-id=promptOption]:visible")
                                         .all_inner_texts() if o.strip() and o.strip() not in taken))
            chosen = pick_option(options, value) or (options[0] if len(options) == 1 and
                                                      options[0].casefold() == value.casefold() else None)
            if chosen is None:
                # A nested list ("Job Boards › LinkedIn Job Board"): the one option naming the answer,
                # preferring the shortest ("LinkedIn" over "LinkedIn Recruiter Job Board").
                wanted = value.casefold()
                naming = sorted((o for o in options if wanted in o.casefold()), key=len)
                if naming and (len(naming) == 1 or len(naming[0]) < len(naming[1])):
                    chosen = naming[0]
            if chosen is None and re.search(r"field of study|major|discipline", field["label"], re.I) \
                    and len(value.split()) > 1:
                # A fixed catalog without the exact major ("Quantitative Economics"): the option that is
                # exactly its broader field ("Economics"), never a guess at a different subject.
                broad = value.split()[-1]
                search.fill("")
                search.press_sequentially(broad, delay=30)
                search.press("Enter")
                page.wait_for_timeout(1200)
                options = list(dict.fromkeys(o.strip() for o in page.locator("[data-automation-id=promptOption]:visible")
                                             .all_inner_texts() if o.strip() and o.strip() not in taken))
                chosen = next((o for o in options if o.casefold() == broad.casefold()), None)
            if chosen is None:
                self._log(f"{field['label'][:50]}: no option for {value!r} among {options[:12]}")
            if chosen and self._choose(page, chosen):
                return True
            page.keyboard.press("Escape")
            return False
        value = self._answer(field, field.get("options") or [])
        if (value is None or value == "") and kind in ("text", "textarea"):
            value = self._luna_answer(field)
        if value is None or value == "":
            return False
        if kind in ("text", "textarea"):
            box.locator("textarea, input:not([type=radio]):not([type=checkbox]):not([type=file])").first.fill(str(value))
            return True
        if kind == "radio":
            chosen = pick_option(field["options"], str(value))
            if chosen is None:
                return False
            # Workday styles its radios over a hidden input that ignores direct checks: click its
            # label (what a person clicks), then confirm the choice took.
            radio = box.locator("input[type=radio]").nth(field["options"].index(chosen))
            radio_id = radio.get_attribute("id") or ""
            label = box.locator(f'label[for="{radio_id}"]') if radio_id else box.locator("label", has_text=chosen)
            for attempt in (lambda: label.first.click(timeout=4000),
                            lambda: radio.click(force=True, timeout=4000),
                            lambda: radio.check(force=True, timeout=4000)):
                try:
                    attempt()
                except Exception:  # noqa: BLE001 - try the next way of pressing it
                    continue
                page.wait_for_timeout(250)
                if radio.is_checked():
                    return True
            return False
        if kind == "checkboxes":
            chosen = pick_option(field["options"], str(value))
            if chosen is None:
                return False
            box.locator("input[type=checkbox]").nth(field["options"].index(chosen)).check()
            return True
        if kind == "checkbox" and value is True:
            # rule_answer never accepts an attestation or consent; only ordinary saved yes/no boxes.
            box.locator("input[type=checkbox]").first.check()
            return True
        return False

    def _answer_known(self, field: dict) -> bool:
        """Whether saved answers could answer this field at all (False: no point retrying it)."""
        try:
            return self._answer(field, field.get("options") or []) is not None or field["kind"] in ("dropdown", "prompt")
        except Exception:  # noqa: BLE001
            return True

    def _fill_step(self, page: Page) -> tuple[int, list[str]]:
        """Fill every empty field on this step once. Returns (filled, open required labels)."""
        filled = 0
        for _ in range(3):   # an answer may reveal follow-up questions
            fields = [f for f in page.evaluate(FIELDS_JS) if f["empty"] and f["id"] not in self._tried]
            if not fields:
                break
            for field in fields:
                # A control that failed (still loading, a click that did not land) gets one more pass.
                failures = self._failures.get(field["id"], 0)
                began = time.monotonic()
                try:
                    if self._fill_field(page, field):
                        filled += 1
                        self.answered[field["label"]] = "filled"
                        self._tried.add(field["id"])
                    elif failures >= 1 or self._answer_known(field) is False:
                        self._tried.add(field["id"])
                    else:
                        self._failures[field["id"]] = failures + 1
                except Exception as exc:  # noqa: BLE001 - one stubborn control never stops the rest
                    self._failures[field["id"]] = failures + 1
                    if failures >= 1:
                        self._tried.add(field["id"])
                    self._log(f"Could not fill {field['label'][:60]}: {str(exc).splitlines()[0][:120]}")
                finally:
                    spent = time.monotonic() - began
                    if spent > 5:
                        self._log(f"Slow: {field['label'][:40]} ({field['kind']}) took {spent:.0f}s")
                    try:
                        page.keyboard.press("Escape")
                    except Exception:  # noqa: BLE001
                        pass
            page.wait_for_timeout(300)
        remaining = [f for f in page.evaluate(FIELDS_JS) if f["empty"] and f["required"]]
        for f in remaining[:4]:
            self._log(f"Open: {f['label'][:40]} ({f['kind']}, block {f.get('group') or '-'}; in {f.get('ancestors', '')[:120]})")
        return filled, [f["label"] for f in remaining]

    def _upload_resume(self, page: Page) -> bool:
        if page.locator("[data-automation-id=file-upload-successful]:visible, "
                        "[data-automation-id=file-upload-item]:visible").count():
            return False
        inputs = page.locator("input[type=file]")
        if not inputs.count():
            return False
        inputs.first.set_input_files(self.resume_path)
        for _ in range(20):   # wait for Workday to accept the file
            page.wait_for_timeout(500)
            if self._resume_visible(page):
                break
        self._resume_attached = self._resume_visible(page)
        self._log("Attached the resume" if self._resume_attached else "Workday did not show the attached resume")
        return self._resume_attached

    def _resume_visible(self, page: Page) -> bool:
        return bool(page.locator("[data-automation-id=file-upload-successful]:visible, "
                                 "[data-automation-id=file-upload-item]:visible").count())

    # ------------------------------------------------------------- accounts
    def _tenant(self) -> str | None:
        try:
            return vault.tenant_of(self.adapter.apply_url or self.job.apply_url)
        except ValueError:
            return None

    def _manage_accounts(self) -> bool:
        return bool((self.settings.get("workday") or {}).get("create_accounts")) and self._tenant() is not None

    def _click_control(self, page: Page, automation_id: str) -> bool:
        """Press one of Workday's own account or navigation buttons by its automation id."""
        control = page.locator(f"[data-automation-id={automation_id}]:visible").first
        if not control.count():
            return False
        try:
            control.click(timeout=4000)
        except Exception:  # noqa: BLE001 - Workday lays a click_filter over its buttons
            control.click(timeout=4000, force=True)
        return True

    def _set_input(self, page: Page, automation_id: str, value: str) -> bool:
        box = page.locator(f"input[data-automation-id={automation_id}]:visible").first
        if not box.count():
            return False
        box.fill(value)
        return True

    def _capture(self, page: Page) -> None:
        """Remember what the candidate typed to sign in, to save once Workday lets them in."""
        try:
            password = page.locator("input[data-automation-id=password]:visible, input[type=password]:visible").first
            email = page.locator("input[data-automation-id=email]:visible, input[type=email]:visible").first
            value = password.input_value(timeout=500) if password.count() else ""
            address = email.input_value(timeout=500) if email.count() else ""
        except Exception:  # noqa: BLE001 - the page moved on
            return
        if value:
            self._typed = (address.strip() or self.profile.email or "", value)

    def _release_signup(self) -> None:
        if getattr(self, "_signup_held", False):
            self._signup_held = False
            _SIGNUP_LOCK.release()

    def _account_done(self) -> None:
        """Past the sign-in: mark the saved account working, or save the one the candidate used."""
        self._release_signup()
        if self._account_closed or not self._manage_accounts():
            return
        self._account_closed = True
        tenant = self._tenant()
        try:
            if self._typed and self._typed[0] and self._typed[1] != vault.password_for(tenant):
                vault.save_account(tenant, self._typed[0], self._typed[1], company=self.job.company,
                                   host=self._host(), status="active", note="Saved from your sign-in")
                self._log("Saved your Workday sign-in in Starployment's vault")
            elif self._used_vault and vault.get_account(tenant):
                vault.set_status(tenant, "active", used=True)
        except Exception as exc:  # noqa: BLE001 - the application goes on without the vault
            self._log(f"Could not update the saved Workday sign-in: {str(exc)[:120]}")
        self._typed = None

    def _host(self) -> str:
        from urllib.parse import urlparse
        return urlparse(self.adapter.apply_url or self.job.apply_url).hostname or ""

    def _inbox(self):
        """The candidate's mailbox (Settings → Connections → Email), for Workday's verification email."""
        options = self.settings.get("email_verification") or {}
        if not options.get("enabled") or not self.profile.email:
            return None
        from .email_verification import EmailVerificationInbox
        try:
            # Workday mails from the employer's own address, not the Greenhouse sender saved there.
            return EmailVerificationInbox({**options, "sender_address": ""}, self.profile.email)
        except Exception:  # noqa: BLE001 - no mailbox password saved
            return None

    def _verify_email(self, ctx) -> bool:
        inbox = self._inbox()
        if inbox is None:
            return False
        baseline = self._mail_baseline
        if baseline is None:
            # The snapshot before Create Account failed (a mailbox that did not answer in time).
            # Look from a little before the mailbox's newest message, and only at mail since the
            # sign-up began; only this employer's own activation link is accepted below.
            try:
                from .email_verification import Baseline
                now = inbox.baseline()
                baseline = Baseline(now.uidvalidity, max(0, now.max_uid - 10),
                                    getattr(self, "_signup_started", now.started_at))
            except Exception as exc:  # noqa: BLE001
                self._log(f"Could not open the mailbox for Workday's verification email: {str(exc)[:120]}")
                return False
        self._log("Waiting for Workday's verification email")
        deadline = time.monotonic() + VERIFY_SECONDS
        problem = ""
        while time.monotonic() < deadline:
            try:
                # Workday links each account's activation to the employer's own address, and its
                # emails can arrive late and out of order: only this employer's link counts.
                link = inbox.wait_for_link(baseline.max_uid, baseline.started_at,
                                           allowed_hosts=[self._host()] if self._host() else WORKDAY_HOSTS,
                                           timeout_sec=30, baseline_uidvalidity=baseline.uidvalidity)
            except Exception as exc:  # noqa: BLE001 - a slow mailbox read: keep waiting until the deadline
                if str(exc) != problem:
                    problem = str(exc)
                    self._log(f"Could not read the mailbox yet: {problem[:120]}")
                time.sleep(5)
                continue
            if link:
                tab = ctx.new_page()
                try:
                    tab.goto(link, wait_until="domcontentloaded", timeout=60000)
                    tab.wait_for_timeout(3000)
                finally:
                    tab.close()
                self._log("Opened Workday's verification link")
                return True
        return False

    def _sign_in(self, ctx, page: Page, state: dict) -> str | None:
        """Workday's sign-in and create-account pages. Returns why to stop, or None to keep going."""
        if not self._manage_accounts():
            if not self.assist:
                return ("Workday asks you to sign in. Turn on Settings → Workday → Manage Workday sign-ins, "
                        "or choose Finish in browser.")
            email = page.locator("input[data-automation-id=email]:visible, input[type=email]:visible").first
            if not self._email_offered and email.count() and not email.input_value():
                email.fill(self.profile.email or "")
                self._email_offered = True
            self._wait_note(page)
            return None
        tenant = self._tenant()
        record = vault.get_account(tenant)
        notice = f"{state['alerts']}\n{state['dialogs']}"
        self._capture(page)
        if state["captcha"]:
            self._log("Workday shows a security check")
            if not self.assist:
                return ("Workday asked for a security check before signing in. Choose Finish in browser and "
                        "complete it yourself.")
            self._banner(page, "Complete the security check yourself. Starployment continues once you are past it.")
            return None
        if state["emailButton"]:
            self._click_control(page, "SignInWithEmailButton")
            return None
        if record and record["status"] == "creating" and ALREADY_RE.search(notice):
            vault.set_status(tenant, "exists", "An account with this email already existed")
            self._log("Workday says an account with this email already exists")
            record = vault.get_account(tenant)
        if record and VERIFY_RE.search(notice) and not self._verify_tried:
            self._verify_tried = True
            vault.set_status(tenant, "needs_verification")
            verified = self._verify_email(ctx)
            self._release_signup()   # this account's email has been read (or given up on)
            if not verified:
                if not self.assist:
                    return (f"Workday sent a verification email to {record['email']}. Open its link, then send "
                            "this application again; Starployment signs in with the saved password.")
                self._banner(page, "Open the verification email Workday sent you, then come back. "
                                   "Starployment signs in with the saved password.")
            return None
        if self._account_tries >= 4 or (record and record["status"] in ("exists", "failed")):
            # The saved password is not this account's: the candidate signs in once and it is saved.
            if not self.assist:
                return (f"Starployment could not sign in to {self.job.company}'s Workday with a saved password. "
                        "Choose Finish in browser and sign in once (or reset the password there); "
                        "Starployment saves it for next time.")
            if state["createForm"]:
                self._click_control(page, "signInLink")
            self._wait_note(page, "Sign in yourself (or reset the password). Starployment saves the password "
                                  "you use and fills the application once you are in.")
            return None
        if state["createForm"]:
            password = vault.password_for(tenant) if record else None
            if record and record["status"] not in ("creating", "needs_verification"):
                self._click_control(page, "signInLink")   # a working account: sign in instead
                return None
            if self.dry_run:
                return "practice"
            email = (record or {}).get("email") or (self.profile.email or "").strip()
            if not email:
                return "Add your email in Profile so Starployment can create the Workday account."
            if not password:
                password = vault.generate_password()
                # Saved before Workday sees it, so a crash never loses an account's password.
                vault.save_account(tenant, email, password, company=self.job.company, host=self._host(),
                                   status="creating")
                self._log(f"Creating a Workday account for {email}; the password is saved in Starployment's vault")
            self._set_input(page, "email", email)
            self._set_input(page, "password", password)
            self._set_input(page, "verifyPassword", password)
            agree = page.locator("input[data-automation-id=createAccountCheckbox]:visible").first
            if agree.count() and not agree.is_checked():
                agree.check()   # Workday's account terms; accepted by turning on Manage Workday sign-ins
            inbox = self._inbox()
            if inbox is not None:
                if not self._signup_held:
                    if not _SIGNUP_LOCK.acquire(timeout=SIGNUP_WAIT_SECONDS):
                        return ("Another Workday account was still waiting for its verification email. "
                                "Send this application again in a few minutes.")
                    self._signup_held = True
                from datetime import datetime, timedelta, timezone
                self._signup_started = datetime.now(timezone.utc) - timedelta(seconds=30)
                self._mail_baseline = None
                for _ in range(2):   # a mailbox that is slow to answer gets a second try
                    try:
                        self._mail_baseline = inbox.baseline()
                        break
                    except Exception:  # noqa: BLE001
                        continue
            self._click_control(page, "createAccountSubmitButton")
            self._account_tries += 1
            self._used_vault = True
            page.wait_for_timeout(2500)
            return None
        if state["passwordForm"]:
            password = vault.password_for(tenant) if record else None
            if not password:
                if self.dry_run:
                    return "practice"
                if self._click_control(page, "createAccountLink"):
                    return None
                if not self.assist:
                    return "Workday asks you to sign in and offers no way to create an account here."
                self._wait_note(page)
                return None
            if self._signin_tries and INVALID_RE.search(notice):
                if record["status"] == "creating" and self._click_control(page, "createAccountLink"):
                    return None   # the account was never made: create it with the saved password
                vault.set_status(tenant, "failed", "Workday did not accept the saved password")
                self._log("Workday did not accept the saved password")
                return None
            self._set_input(page, "email", record["email"])
            self._set_input(page, "password", password)
            self._click_control(page, "signInSubmitButton")
            self._signin_tries += 1
            self._account_tries += 1
            self._used_vault = True
            self._log("Signed in with the saved Workday password")
            page.wait_for_timeout(2500)
            return None
        self._wait_note(page)
        return None

    def _wait_note(self, page: Page, message: str = "") -> None:
        if not self._signed_in_note:
            self._signed_in_note = True
            self._log("Waiting for you to sign in to Workday")
        self._banner(page, message or f"Sign in to {self.job.company}'s Workday account, or create one. "
                                      "Starployment fills the application once you are in.")

    # ------------------------------------------------------------- steps
    def _advance(self, page: Page, step: str) -> bool:
        """Press Save and Continue (or Submit) and wait for Workday to move on."""
        try:
            before = page.evaluate(STATE_JS)["alerts"].strip()
        except Exception:  # noqa: BLE001
            before = ""
        # Tenants name the footer button differently; both send the step on.
        if not self._click_control(page, "bottom-navigation-next-button"):
            self._click_control(page, "pageFooterNextButton")
        for tick in range(60):
            page.wait_for_timeout(500)
            try:
                now = page.evaluate(STATE_JS)
            except Exception:  # noqa: BLE001 - mid-navigation
                continue
            if now["step"] != step or SUCCESS_RE.search(now["dialogs"]):
                return True
            # Only an error that appeared after the click (a banner left from earlier is not one),
            # and only once Workday has finished checking the page.
            alerts = now["alerts"].strip()
            if alerts and alerts != before and not now["busy"] and tick >= 4:
                return False
        return False

    def _wait_until_open(self, page: Page) -> str:
        """'' once the page offers Apply, a start choice, a sign-in, or a step; else 'closed' or 'none'."""
        deadline = time.monotonic() + READY_SECONDS
        while time.monotonic() < deadline:
            try:
                state = page.evaluate(STATE_JS)
                if state["apply"] or state["start"] or state["signIn"] or state["createForm"] or state["step"]:
                    return ""
                if CLOSED_RE.search(page.inner_text("body", timeout=3000)[:6000]):
                    return "closed"
            except Exception:  # noqa: BLE001 - still loading or navigating
                pass
            page.wait_for_timeout(1000)
        return "none"

    def _run_page(self, ctx, page: Page, url: str) -> ApplyResult:
        unattended = not self.assist
        if unattended and not (self.settings.get("workday") or {}).get("send"):
            return self._result("needs_human", "Workday applications need you signed in to the employer's Workday "
                                               "account. Choose Finish in browser.")
        self._typed: tuple[str, str] | None = None
        self._account_closed = self._used_vault = self._verify_tried = False
        self._email_offered = self._signed_in_note = False
        self._account_tries = self._signin_tries = 0
        self._mail_baseline = None
        self._signup_held = False
        self._resume_attached = False
        self._failures: dict[str, int] = {}
        try:
            return self._walk(ctx, page, url)
        finally:
            self._release_signup()

    def _walk(self, ctx, page: Page, url: str) -> ApplyResult:
        unattended = not self.assist
        response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if response and response.status >= 400:
            self._shot(page, "unavailable")
            return self._result("needs_human", f"The posting returned HTTP {response.status}; it may be closed.")
        page.wait_for_timeout(1500)
        # Nothing has been sent yet: make sure this is a live application before the submit
        # boundary, so a closed posting is not left as an attempt that can never be retried.
        opened = self._wait_until_open(page)
        if opened:
            self._shot(page, "closed" if opened == "closed" else "unrecognized")
            return self._result("failed", "The employer has closed this posting." if opened == "closed" else
                                "This Workday page showed no application to start; nothing was sent.")
        # The resume upload and every saved page reach the employer: the submit boundary
        # comes before Starployment touches the form. A practice run saves nothing.
        if not self.dry_run:
            db.mark_submission_attempted(self.job.id)
            self.submission_attempted = True
        baseline = self._confirmation_signals(page)
        self.root = page.main_frame
        seconds = ASSIST_SECONDS if self.assist else SEND_SECONDS
        deadline = time.monotonic() + seconds
        started = submitted = False
        steps: dict[str, int] = {}
        sign_in_waits = settle_waits = 0
        settled_step = waiting_for = ""
        try:
            page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        while time.monotonic() < deadline:
            try:
                page = next((p for p in reversed(ctx.pages) if not p.is_closed()), page)
                if page.is_closed():
                    break
                if self._confirmation_signals(page) - baseline:
                    self._account_done()
                    self._shot(page, "success")
                    return self._result("applied", "You submitted it in the browser, and Workday confirmed it."
                                        if self.assist else "Submitted on Workday, and Workday confirmed it.")
                state = page.evaluate(STATE_JS)
                if state["step"] != waiting_for:
                    waiting_for, settle_waits = state["step"], 0   # each step gets its own loading wait
                if state["apply"] and not started:
                    started = True
                    self._log("Open the application")
                    self._press(page.locator("[data-automation-id=adventureButton]").first)
                elif state["start"]:
                    # Apply manually: Starployment fills every page from saved answers. "Use my last
                    # application" only helps after an earlier application at this employer, and
                    # otherwise leaves an error banner; Workday's own resume reading can add entries.
                    order = ("applyManually", "useMyLastApplication", "autofillWithResume")
                    choices = [c for c in order if c in state["start"]
                               and not (self.dry_run and c == "autofillWithResume")]
                    choice = (choices or state["start"])[0]
                    self._log(f"Start: {choice}")
                    self._press(page.locator(f"[data-automation-id={choice}]").first)
                elif state["signIn"] or state["createForm"]:
                    stop = self._sign_in(ctx, page, state)
                    if stop == "practice":
                        return self._result("dry_run", "Practice run: this Workday application needs an account, "
                                                       "and practice runs create none. Nothing was saved.")
                    if stop:
                        self._shot(page, "sign-in")
                        return self._result("needs_human", stop)
                elif state["step"] and SIGN_IN_STEP_RE.search(state["step"]):
                    # Workday draws its progress bar ("Create Account/Sign In") before the
                    # account form itself: wait for the form; this step is never filled.
                    sign_in_waits += 1
                    if sign_in_waits > 30:
                        self._shot(page, "sign-in")
                        return self._result("needs_human", "Workday's sign-in step never showed a form "
                                                           "Starployment recognizes. Choose Finish in browser.")
                elif state["step"] and (state["busy"] or not state["next"]) and settle_waits < SETTLE_POLLS:
                    settle_waits += 1     # still loading this step: look again in a moment
                elif state["step"]:
                    self._account_done()
                    step = state["step"]
                    if step != settled_step:
                        settled_step = step
                        page.wait_for_timeout(800)   # let the last fields render
                        continue
                    if re.search(r"\breview\b", step, re.I):
                        if self.assist:
                            self._banner(page, "Review your application, then press Submit yourself. "
                                               "Keep this window open until Workday confirms it.")
                        elif not submitted:
                            if not SUBMIT_RE.match(state["next"]):
                                self._shot(page, "review")
                                return self._result("needs_human", "Workday's review page has no Submit button "
                                                                   "Starployment recognizes. Choose Finish in browser.")
                            self._shot(page, "review")
                            submitted = True
                            self._log("Pressed Submit")
                            if not self._advance(page, step):
                                self._shot(page, "not-submitted")
                                return self._result("needs_human", "Workday did not accept Submit: "
                                                    + (state["alerts"] or "no confirmation appeared")[:200]
                                                    + ". Choose Finish in browser to check it.")
                    elif steps.get(step, 0) < 2:
                        steps[step] = steps.get(step, 0) + 1
                        if state["upload"] and not self.dry_run and self._upload_resume(page):
                            page.wait_for_timeout(2500)   # Workday reads the resume into the next steps
                        if (unattended and not self.dry_run and state["resumeStep"] and not self._resume_attached
                                and not self._resume_visible(page)):
                            # Never send an application without the resume.
                            self._shot(page, "no-resume")
                            return self._result("needs_human", f"Workday's {step} page took no resume upload "
                                                               "Starployment recognizes. Choose Finish in browser "
                                                               "to attach it.")
                        filled, open_required = self._fill_step(page)
                        self._log(f"{step}: filled {filled}" + (f"; yours to answer: {'; '.join(open_required[:6])}"
                                                                 if open_required else ""))
                        self.unanswered = list(dict.fromkeys(self.unanswered + open_required))
                        if self.assist:
                            self._banner(page, (f"Starployment filled this page. Answer: {'; '.join(open_required[:3])}"
                                                + (" and more" if len(open_required) > 3 else "") + ". "
                                                if open_required else "Starployment filled this page. ")
                                         + "Check it, then press Save and Continue.")
                        elif self.dry_run:
                            blocking = (" A real send would stop at: " + "; ".join(open_required[:8]) + "."
                                        if open_required else "")
                            return self._result("dry_run", f"Practice run: signed in and filled {filled} fields on "
                                                           f"Workday's {step} page; nothing was saved.{blocking}")
                        elif open_required:
                            self._shot(page, "needs-answers")
                            return self._result("needs_human", "Workday asks what Starployment has no saved answer "
                                                f"for: {'; '.join(open_required[:6])}. Choose Finish in browser "
                                                "to answer them.")
                        elif not NEXT_RE.match(state["next"]):
                            self._shot(page, "no-next")
                            return self._result("needs_human", f"Workday's {step} page has no Save and Continue "
                                                               "button Starployment recognizes. Choose Finish in browser.")
                        else:
                            self._log(f"{step}: Save and Continue")
                            if not self._advance(page, step):
                                self._shot(page, "not-saved")
                                alerts = page.evaluate(STATE_JS)["alerts"]
                                return self._result("needs_human", f"Workday kept the {step} page open: "
                                                    + (alerts or "it did not move on")[:240]
                                                    + ". Choose Finish in browser to fix it.")
                    elif unattended:
                        self._shot(page, "stuck")
                        return self._result("needs_human", f"Workday kept returning to the {step} page. "
                                                           "Choose Finish in browser to finish it.")
                page.wait_for_timeout(1000)
            except Exception as exc:  # noqa: BLE001 - a navigation detached the page; look again
                if page.is_closed():
                    break
                self._log(f"Waiting: {str(exc).splitlines()[0][:120]}")
                time.sleep(1)
        else:
            self._shot(page, "unconfirmed")
            if unattended and not submitted:
                return self._result("needs_human", f"Workday did not reach its review page within {seconds // 60} "
                                                   "minutes; nothing was submitted. Choose Finish in browser.")
            return self._result("needs_human", f"No Workday confirmation appeared within {seconds // 60} "
                                               "minutes. If it was submitted, check your email, then Mark as sent.")
        return self._result("needs_human", "The browser was closed before Workday confirmed the application. "
                                           "If you submitted it, check your email, then Mark as sent.")
