"""Fill supported ATS forms from known candidate facts and verify submission.

Each attempt uses the local, dedicated browser profile. Unknown required answers,
visible challenges, invalid fields, and ambiguous submission outcomes stop the
attempt with a screenshot. No CAPTCHA bypass or guessed factual answers.
"""
from __future__ import annotations

import itertools
import json
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime
from contextlib import contextmanager
from contextlib import nullcontext as _nullcontext
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Page, sync_playwright

from .. import config, db
from ..answers import (FORMAT_NAMES, Answer, Q, US_STATES, availability_date, fit_answer, form_date, llm_answers,
                       pick_option, rule_answer)
from ..models import ApplyResult, Job, Profile
from .adapters import Adapter
from .email_verification import Baseline, EmailVerificationInbox, VerificationToken, redact_token

# Field identifiers remain attached to their elements across scans. This matters
# for conditional forms, duplicate labels, and radio groups after the first pass.
SCAN_JS = r"""
() => {
  const visible = e => {
    if (!e) return false;
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const clean = t => (t || '').replace(/\s+/g, ' ').replace(/[*✱]/g, '').trim().slice(0, 400);
  const idFor = e => {
    if (!e.dataset.jh) e.dataset.jh = String(window.__jobhubFieldId = (window.__jobhubFieldId || 0) + 1);
    return e.dataset.jh;
  };
  const customTitle = e => {
    const field = e.closest('[data-testid=field]');
    if (!field?.querySelector('[data-testid^="customQuestions."]')) return null;
    return field.previousElementSibling?.querySelector('p') || null;
  };
  const ownLabel = e => {
    if (e.closest('[data-testid="phone_number-code"]')) return 'Phone country code';
    if (e.getAttribute('aria-labelledby')) return e.getAttribute('aria-labelledby').split(' ').map(
      id => document.getElementById(id)?.innerText || '').join(' ');
    const associated = e.id ? document.getElementById(e.id + '-label') : null;
    if (associated) return associated.innerText;
    const field = e.closest('[data-testid=field]');
    const title = field?.querySelector('[id$="-label"]');
    if (title && !['checkbox','radio'].includes(e.type) && !['checkbox','radio'].includes(e.getAttribute('role')))
      return title.innerText;
    const custom = customTitle(e);
    if (custom && !['checkbox','radio'].includes(e.type) && !['checkbox','radio'].includes(e.getAttribute('role')))
      return custom.innerText;
    const labels = [...(e.labels || [])];
    if (labels.length) return labels.map(l => l.innerText || l.textContent).join(' ');
    return e.getAttribute('aria-label') || '';
  };
  const placeholder = t => !t || (!/choose not to (?:answer|say|disclose)/i.test(t) &&
    /^(start typing|select|choose|pick|type|search|enter)\b/i.test(t.trim()));
  const fieldTitle = e => e.closest('.ashby-application-form-field-entry,.ashby-application-form-input-radio-group,.ashby-application-form-input-checkbox-group')
    ?.querySelector('.ashby-application-form-question-title');
  const leverTitle = e => e.closest('.application-question')?.querySelector('.application-label');
  const labelFor = e => {
    let text = ownLabel(e);
    if (!['checkbox','radio'].includes(e.type) && leverTitle(e)) text = leverTitle(e).innerText;
    if (placeholder(text)) {
      let parent = e.parentElement;
      for (let i = 0; i < 5 && parent && parent !== document.body; i++, parent = parent.parentElement) {
        const label = [...parent.querySelectorAll('label,legend,[class*=label i],[class*=question i],h3,h4')]
          .find(l => !l.contains(e) && !placeholder(l.innerText) && clean(l.innerText).length > 1);
        if (label) { text = label.innerText; break; }
      }
    }
    return text || e.getAttribute('placeholder') || e.name || e.id || '';
  };
  const required = (e, label) => !!(e.required || e.getAttribute('aria-required') === 'true' ||
      /[*✱]|\(required\)/i.test(label || '') ||
      /(?:^|\s)_?required(?:_|\s|$)/i.test(fieldTitle(e)?.className || '') ||
      e.closest('[role=group][aria-required=true],[role=radiogroup][aria-required=true],fieldset[aria-required=true]') ||
      /[*✱]/.test(e.closest('[data-testid=field]')?.querySelector('[id$="-label"]')?.parentElement?.innerText || ''));
  const seen = new Set(), out = [];
  // Scope to an application form when the document also contains newsletter/search forms.
  const upload = [...document.querySelectorAll('input[type=file]')].find(e => /resume|cv/i.test(labelFor(e)));
  const scope = upload?.closest('form') || document;
  // Ashby represents a yes/no question with buttons and an invisible checkbox.
  // The unchecked native input does NOT mean the applicant has answered "No".
  for (const group of scope.querySelectorAll('.ashby-application-form-input-yesno')) {
    if (!visible(group)) continue;
    const buttons = [...group.querySelectorAll('button[data-option]')].filter(visible);
    if (!buttons.length) continue;
    const input = group.querySelector('input');
    if (input) seen.add(input);
    const title = fieldTitle(group), label = title?.innerText || '';
    out.push({idx: idFor(group), kind: 'radio', type: 'custom', tag: 'div', id: '', name: input?.name || '',
      label: clean(label) || 'Unlabeled yes/no question', required: required(group, label),
      options: buttons.map(b => clean(b.innerText)), optionIdx: buttons.map(idFor), buttonOptions: true,
      value: '', selectedOption: clean(buttons.find(b => b.getAttribute('aria-pressed') === 'true')?.innerText),
      empty: !buttons.some(b => b.getAttribute('aria-pressed') === 'true'), invalid: false, validation: ''});
  }
  const checked = e => e.checked === true || e.getAttribute('aria-checked') === 'true';
  // Accessible custom radio groups expose their state through ARIA, not native inputs.
  for (const group of scope.querySelectorAll('[role=radiogroup]')) {
    const radios = [...group.querySelectorAll('[role=radio]')].filter(visible);
    if (!visible(group) || !radios.length) continue;
    const label = customTitle(group)?.innerText || document.getElementById(group.id + '-label')?.innerText || labelFor(group);
    radios.forEach(e => { seen.add(e); e.querySelectorAll('input').forEach(n => seen.add(n)); });
    out.push({idx: idFor(group), kind: 'radio', type: 'custom', tag: group.tagName.toLowerCase(),
      id: group.id || '', name: group.getAttribute('data-testid') || '', label: clean(label),
      required: required(group, label) || radios.some(e => required(e, label)),
      options: radios.map(e => clean(e.getAttribute('aria-label') || e.innerText || ownLabel(e))),
      optionIdx: radios.map(idFor), ariaOptions: true, value: '',
      selectedOption: clean(radios.find(checked)?.getAttribute('aria-label') || radios.find(checked)?.innerText),
      empty: !radios.some(checked), invalid: group.getAttribute('aria-invalid') === 'true', validation: ''});
  }
  for (const e of scope.querySelectorAll('input,select,textarea,[role=combobox],[role=checkbox]')) {
    const type = (e.type || (e.getAttribute('role') === 'checkbox' ? 'checkbox' : '')).toLowerCase();
    if (e.disabled || e.getAttribute('aria-disabled') === 'true' ||
        ['hidden','submit','button','reset'].includes(type) && e.getAttribute('role') !== 'combobox') continue;
    if (seen.has(e) || (e.tagName === 'INPUT' && e.closest('[role=checkbox]'))) continue;
    // select2 and jQuery Chosen draw a stand-in for a real <select>, which is what gets filled.
    if (e.closest('.select2-container,.chosen-container')) continue;
    const chosenWidget = e.tagName === 'SELECT' &&
      (e.nextElementSibling?.classList?.contains('chosen-container') ? e.nextElementSibling
        : e.id ? document.getElementById(e.id + '_chosen') : null);
    // Custom radios/checkboxes often hide the native control behind a visible label.
    const labeledVisible = [...(e.labels || [])].some(visible) ||
      (e.getAttribute('aria-labelledby') || '').split(' ').some(id => visible(document.getElementById(id)));
    if (type !== 'file' && !visible(e) && !(chosenWidget && visible(chosenWidget)) &&
        !(['radio','checkbox'].includes(type) && labeledVisible)) continue;
    let rawLabel = e.getAttribute('role') === 'checkbox' ? (ownLabel(e) || e.innerText) : labelFor(e);
    // Ashby's repeatable education block labels its fields "Start Date", "School"...
    if (e.closest('.ashby-application-form-input-education-entry') && !/education/i.test(rawLabel)) {
      rawLabel = 'Education ' + rawLabel;
    }
    const rec = {idx: idFor(e), tag: e.tagName.toLowerCase(), type, id: e.id || '', name: e.name || '',
      required: required(e, rawLabel), label: clean(rawLabel), kind: '', options: [],
      value: e.value || '', empty: !(e.value || '').trim(), invalid: e.willValidate ? !e.validity.valid : false,
      validation: e.validationMessage || '', ariaInvalid: e.getAttribute('aria-invalid') === 'true'};
    if (type === 'file') { rec.kind = 'file'; rec.empty = !e.files?.length; rec.fileName = e.files?.[0]?.name || ''; }
    else if (type === 'radio') {
      const groupScope = e.closest('form') || document;
      const all = e.name ? [...groupScope.querySelectorAll('input[type=radio]')].filter(r => r.name === e.name && !r.disabled) : [e];
      all.forEach(r => seen.add(r));
      let container = e.closest('fieldset,[role=radiogroup],[role=group]') || e.parentElement;
      while (container && container !== scope && all.some(r => !container.contains(r))) container = container.parentElement;
      const optionLabels = all.map(r => clean(ownLabel(r) || r.value));
      const legend = container?.querySelector('legend');
      let question = legend?.innerText || fieldTitle(e)?.innerText || leverTitle(e)?.innerText || (container ? ownLabel(container) : '');
      if (!question && container) question = (container.innerText || '').split('\n').map(clean)
        .find(t => t && !optionLabels.includes(t)) || '';
      // Lever's question text is a sibling of the options list, outside its
      // nearest common ancestor. Walk upward without crossing the form.
      for (let parent = container?.parentElement, level = 0; !question && parent && parent !== scope && level < 3;
           parent = parent.parentElement, level++) {
        const text = [...parent.children].filter(c => !c.contains(e)).map(c => c.innerText || '').join('\n');
        question = text.split('\n').map(clean).find(t => t && !optionLabels.includes(t)) || '';
      }
      // Ashby puts a yes/no "text me about this application" choice inside the phone field.
      const texting = e.closest('.ashby-application-form-field-entry')
        ?.querySelector('[class*=texting-consent-description]');
      if (texting && container?.className && /consent/i.test(String(container.className))) {
        question = 'Receive text message updates about this application';
      }
      rec.kind = 'radio'; rec.label = clean(question) || e.name || rec.label;
      rec.options = optionLabels; rec.optionIdx = all.map(idFor);
      rec.required = all.some(r => required(r, question)); rec.empty = !all.some(r => r.checked);
      rec.selectedOption = optionLabels[all.findIndex(r => r.checked)] || '';
      rec.invalid = all.some(r => r.willValidate && !r.validity.valid);
    }
    else if (type === 'checkbox') {
      rec.kind = 'checkbox'; rec.empty = !checked(e); rec.value = checked(e);
      const group = e.closest('fieldset[aria-required],.ashby-application-form-input-checkbox-group,' +
                              '.ashby-application-form-field-entry,.application-question,[role=group]');
      const members = group ? [...group.querySelectorAll('[role=checkbox],input[type=checkbox]')].filter(m => m.getAttribute('role') === 'checkbox' || !m.closest('[role=checkbox]')) : [];
      const title = customTitle(e) || group?.querySelector('legend,.ashby-application-form-question-title,.application-label') ||
        (group?.id ? document.getElementById(group.id + '-label') : null);
      // A box inside a block of other inputs (Ashby's Education History: school, degree, dates,
      // and "Still student?") is one part of that block, not a question of its own: its
      // required mark belongs to the block, and unticked is a real answer.
      const composite = !!group?.querySelector(
        'input:not([type=checkbox]):not([type=radio]):not([type=hidden]):not([type=file]),select,textarea');
      if (composite && members.length === 1) rec.required = !!(e.required || e.getAttribute('aria-required') === 'true');
      // One box under its own question ("Please review the privacy policy" / "Acknowledge") is
      // labelled like a group option, matching how the question was prepared.
      else if (members.length > 1 || (members.length === 1 && title && clean(title.innerText) &&
                                      clean(title.innerText) !== rec.label)) {
        rec.groupLabel = clean(title?.innerText) || 'Checkbox choices';
        rec.groupEmpty = !members.some(checked);
        rec.required = rec.required || required(group, title?.innerText);
        rec.groupMembers = members.map(idFor);
        rec.label = rec.groupLabel + ' - ' + rec.label;
        if (rec.required) rec.invalid = rec.groupEmpty;
      }
    }
    else if (e.tagName === 'SELECT') {
      rec.kind = 'select'; rec.options = [...e.options].filter(o => !o.disabled && o.value !== '').map(o => clean(o.text));
      rec.selectedOption = clean(e.selectedOptions?.[0]?.text);
    }
    else if (e.tagName === 'TEXTAREA') rec.kind = 'textarea';
    else if (e.getAttribute('role') === 'combobox' || e.getAttribute('aria-autocomplete') ||
             /select__input|combobox|autocomplete|location-input/i.test(String(e.className)) ||
             e.closest('[class*=select__control]') || e.parentElement?.querySelector(':scope > .dropdown-container .dropdown-results')) {
      rec.kind = 'combobox';
      const box = e.closest('[class*=select__control]') || e.parentElement;
      const chosen = box?.querySelector('[class*=singleValue],[class*=single-value],[class*=multiValue]');
      if (chosen?.textContent.trim()) { rec.value = chosen.textContent.trim(); rec.empty = false; }
      if (['BUTTON','DIV','SPAN'].includes(e.tagName)) { rec.value = clean(e.innerText); rec.empty = placeholder(rec.value); }
    }
    else if (['text','email','tel','url','number','search','date',''].includes(type)) {
      rec.kind = type === 'number' ? 'number' : 'text';
      // Date pickers (react-datepicker on Ashby) only accept a date typed key by key.
      rec.nativeDate = type === 'date';
      rec.datePicker = rec.nativeDate || /input-date|datepicker/i.test(String(e.className)) ||
        !!e.closest('.react-datepicker-wrapper,.react-datepicker__input-container');
    }
    else continue;
    if (!rec.label) rec.label = rec.kind === 'file' ? 'Attachment' : 'Unlabeled field';
    out.push(rec);
  }
  return out;
}
"""

# Cookie and consent banners float over the page. On a career site that embeds the form in
# an iframe, a banner covering a dropdown swallows the click even though the browser reports
# it as delivered. Hiding the banner gives no consent (no choice is clicked or sent); the
# site treats an unanswered banner as "no" to optional cookies.
HIDE_BANNERS_JS = r"""
() => {
  const consent = /cookie|consent|privacy (?:preferences|choices|settings)|we use (?:cookies|technologies)|gdpr/i;
  let hidden = 0;
  for (const e of document.querySelectorAll('body *')) {
    const s = getComputedStyle(e);
    if ((s.position !== 'fixed' && s.position !== 'sticky') || s.display === 'none' || e.offsetHeight < 40) continue;
    if (e.querySelector('form, iframe, input[type=file], textarea')) continue;   // never the application itself
    const text = (e.innerText || '').slice(0, 3000);
    const marked = /onetrust|evidon|cookie|consent|truste|cybot|osano|didomi|usercentrics|qc-cmp/i
      .test(e.id + ' ' + e.className);
    if (marked || consent.test(text)) { e.style.setProperty('display', 'none', 'important'); hidden++; }
  }
  return hidden;
}
"""

CAPTCHA_JS = r"""
() => {
  const visible = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 35 && r.height > 35 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const frames = [...document.querySelectorAll('iframe')].filter(f =>
    /hcaptcha|recaptcha|turnstile|arkose|funcaptcha/i.test(f.src) && !/size=invisible/.test(f.src) && visible(f));
  const text = /verify (?:you are|you're) (?:a )?human|complete the captcha|captcha (?:is )?required|security check/i
    .test(document.body.innerText.slice(0, 30000));
  return {widgets: frames.length, challenge: frames.length > 0, text};
}
"""

ERROR_JS = r"""
() => {
  const visible = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden'; };
  const out = new Set();
  document.querySelectorAll('[aria-invalid=true],.field-error,.error,[class*=error i],[role=alert]').forEach(e => {
    if (!visible(e)) return;
    const text = (e.innerText || '').trim();
    if (text && text.length < 300) out.add(text.replace(/\s+/g,' '));
    if (e.getAttribute('aria-invalid') === 'true') {
      const label = [...(e.labels || [])].map(l => l.innerText).join(' ') || e.getAttribute('aria-label');
      if (label) out.add('Invalid: ' + label.trim().slice(0,120));
    }
  });
  return [...out].slice(0,20);
}
"""


# Bot filters (reCAPTCHA v3 on Ashby, and similar) score a session on browser
# fingerprints and on how the page is used. An instantly filled form submitted
# seconds after load, from a browser that reports navigator.webdriver and a
# "HeadlessChrome" user agent, is flagged as spam. The engine therefore runs
# Chromium without automation markers and paces itself like a person. Tests
# disable pacing with JOBHUB_HUMAN_PACING=0 (it changes timing, never answers).
STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]
STEALTH_SCRIPT = """
Object.defineProperty(Navigator.prototype, 'webdriver', {get: () => undefined});
if (!window.chrome) { window.chrome = {runtime: {}}; }
"""
MIN_PAGE_SECONDS = 18          # time on the form before Submit, like a quick human
# Searchable catalogs where a value missing from the list is truthfully "Other".
CATALOG_RE = re.compile(r"\b(?:school|university|college|institution|degree|discipline|major|field of study)\b", re.I)
SPAM_RE = re.compile(r"flagged as (?:possible )?spam|spam|suspicious activity|automated|are you a robot|"
                     r"recaptcha|verify you are human|bot detected", re.I)


def human_pacing() -> bool:
    return os.environ.get("JOBHUB_HUMAN_PACING", "1").strip().lower() not in {"0", "false", "off", "no"}


# A window has browser chrome above the page; a headless one reports the same
# outer and inner size. Report the height a normal window would have.
HEADLESS_WINDOW_SCRIPT = """
if (window.outerHeight === window.innerHeight) {
  Object.defineProperty(window, 'outerHeight', {get: () => window.innerHeight + 87});
}
"""

_HEADLESS_UA: str | None = None
_HEADLESS_UA_LOCK = threading.Lock()


def _browser_user_agent(playwright, channel: str) -> str:
    """The browser's own user agent without the "HeadlessChrome" marker (probed once)."""
    global _HEADLESS_UA
    with _HEADLESS_UA_LOCK:
        if _HEADLESS_UA is None:
            probe = playwright.chromium.launch(headless=True, channel=channel)
            try:
                _HEADLESS_UA = probe.new_page().evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
            finally:
                probe.close()
        return _HEADLESS_UA


def show_browser(settings: dict | None = None) -> bool:
    """Whether sending opens a visible browser window (Settings, or ``JOBHUB_HEADLESS=0``)."""
    return bool((settings or {}).get("show_browser")) or not config.HEADLESS


def launch_context(playwright, channel: str | None, profile_dir: Path | None = None, visible: bool = False):
    """Open a persistent browser profile, without automation fingerprints when pacing is on.

    By default the browser runs in Chromium's full headless mode: the real
    browser and GPU stack with no window, so sending never takes focus or
    shows a window. The "HeadlessChrome" user-agent marker is removed, and the
    window reports a normal height. ``visible`` opens an ordinary window
    instead, for watching a send.
    """
    profile = str(profile_dir or config.BROWSER_PROFILE)
    custom = channel if channel and channel != "chromium" else None
    options = dict(viewport={"width": 1280, "height": 1000}, locale="en-US")
    if not human_pacing():
        return playwright.chromium.launch_persistent_context(profile, headless=not visible, channel=custom,
                                                             **options)
    args = list(STEALTH_ARGS) + ["--window-size=1280,1087"]
    if visible:
        ctx = playwright.chromium.launch_persistent_context(profile, headless=False, args=args, channel=custom,
                                                            ignore_default_args=["--enable-automation"], **options)
    else:
        # The full browser in headless mode (channel "chromium"), not the reduced headless shell.
        engine_channel = custom or "chromium"
        options["user_agent"] = _browser_user_agent(playwright, engine_channel)
        ctx = playwright.chromium.launch_persistent_context(profile, headless=True, args=args, channel=engine_channel,
                                                            ignore_default_args=["--enable-automation"], **options)
        ctx.add_init_script(HEADLESS_WINDOW_SCRIPT)
    ctx.add_init_script(STEALTH_SCRIPT)
    return ctx


# Chromium locks a profile to one process, so each simultaneous send uses its own
# profile folder: the main profile first, then numbered ones beside it.
_PROFILE_LOCK = threading.Lock()
_PROFILES_IN_USE: set[int] = set()
# Greenhouse email codes arrive in one mailbox; with codes enabled, one such send at a time.
_MAILBOX_LOCK = threading.Lock()
# How long a hand-off window waits for the candidate to finish and submit.
ASSIST_SECONDS = 20 * 60
HANDOFF_BANNER_JS = """() => {
  if (document.getElementById('starployment-handoff')) return;
  const bar = document.createElement('div');
  bar.id = 'starployment-handoff';
  bar.textContent = 'Starployment filled what it could. Finish anything left (a CAPTCHA, a code, a field), '
    + 'then press Submit yourself. Keep this window open until the confirmation appears.';
  bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;padding:10px 16px;'
    + 'font:600 14px -apple-system,system-ui,sans-serif;color:#1d1a12;background:#f3d27a;'
    + 'box-shadow:0 2px 8px rgba(0,0,0,.2);pointer-events:none';
  document.body.appendChild(bar);
}"""


@contextmanager
def browser_profile_slot():
    with _PROFILE_LOCK:
        slot = next(index for index in itertools.count() if index not in _PROFILES_IN_USE)
        _PROFILES_IN_USE.add(slot)
    try:
        path = config.BROWSER_PROFILE if slot == 0 else config.BROWSER_PROFILE / "parallel" / f"slot-{slot}"
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        yield path
    finally:
        with _PROFILE_LOCK:
            _PROFILES_IN_USE.discard(slot)


class FormEngine:
    def __init__(self, job: Job, profile: Profile, settings: dict, resume_path: str, adapter: Adapter, dry_run: bool,
                 assist: bool = False):
        self.job, self.profile, self.settings = job, profile, settings
        # Assist: a visible window, filled as far as Starployment can; the candidate
        # finishes it (a CAPTCHA, a code, a field) and presses Submit themselves.
        self.assist = assist
        # Ashby saves each answer to its server as it is entered and checks those
        # saves at Submit; a button can look pressed while its save never landed.
        self._ashby_saves: dict[str, object] = {}
        self._settled: set[int] = set()
        self.resume_path, self.adapter, self.dry_run = resume_path, adapter, dry_run
        self.log: list[str] = []
        self.screenshots: list[str] = []
        self.unanswered: list[str] = []
        self.question_details: list[dict] = []
        self.answered: dict[str, str] = {}
        self.submission_attempted = False
        self._attempt_id = uuid.uuid4().hex[:10]
        self._shot_n = 0
        self.root = None
        self._tried: set[str] = set()
        self._filled: set[str] = set()
        self._answered_signatures: dict[tuple[str, str, str, str], object] = {}
        self._failures: dict[str, str] = {}
        self._timeouts: dict[str, int] = {}   # fields whose fill timed out, retried once in a later pass
        self._opened_at = time.monotonic()
        self._rng = random.Random()

    def _log(self, msg: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")

    def _shot(self, page: Page, tag: str) -> str:
        self._shot_n += 1
        name = f"{self.job.id}_{self._attempt_id}_{self._shot_n:02d}_{tag}.png"
        path = config.SCREENSHOTS / name
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(path), full_page=True, timeout=10000)
            config.secure_file(path)
            self.screenshots.append(name)
        except Exception as exc:  # noqa: BLE001
            self._log(f"Screenshot unavailable: {str(exc)[:160]}")
        return name

    def run(self) -> ApplyResult:
        if not Path(self.resume_path).is_file():
            return self._result("needs_human", "The resume file is missing. Upload a resume before applying.")
        url = self.adapter.apply_url
        self._log(f"[{self.adapter.name}] open {url}")
        # A dedicated persistent profile retains login cookies without reading the
        # user's regular browser. Each simultaneous send has its own profile slot.
        mailbox = bool(self.settings.get("email_verification", {}).get("enabled"))
        with sync_playwright() as playwright, browser_profile_slot() as profile_dir, \
                (_MAILBOX_LOCK if mailbox else _nullcontext()):
            channel = self.settings.get("browser_channel") or None
            ctx = None
            page = None
            try:
                ctx = launch_context(playwright, channel, profile_dir,
                                     visible=self.assist or show_browser(self.settings))
                ctx.set_default_timeout(7000)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                if self.adapter.name == "ashby":
                    self._watch_ashby_saves(page)
                self._opened_at = time.monotonic()
                return self._run_page(ctx, page, url)
            except Exception as exc:  # noqa: BLE001
                if page is not None:
                    self._shot(page, "exception")
                detail = str(exc).splitlines()[0][:240]
                if re.search(r"ProcessSingleton|SingletonLock|profile.*in use", str(exc), re.I):
                    return self._result("needs_human", "The Starployment browser profile is already in use. Close the other Starployment browser and retry.")
                return self._result("needs_human" if self.submission_attempted else "failed", f"Browser could not complete the application: {detail}")
            finally:
                if ctx is not None:
                    ctx.close()

    FRAME_RE = re.compile(r"greenhouse\.io|ashbyhq\.com|lever\.co|apply|application|careers|jobs", re.I)

    @staticmethod
    def _frame_clipped(frame) -> bool:
        """A loaded form inside a collapsed parent is not ready to fill."""
        try:
            return bool(frame.frame_element().evaluate("""(element) => {
              const frame = element.getBoundingClientRect();
              for (let parent = element.parentElement; parent; parent = parent.parentElement) {
                const style = getComputedStyle(parent);
                if (!/(hidden|clip)/.test(style.overflowX + ' ' + style.overflowY)) continue;
                const bounds = parent.getBoundingClientRect();
                if (frame.top < bounds.top || frame.bottom > bounds.bottom ||
                    frame.left < bounds.left || frame.right > bounds.right) return true;
              }
              return false;
            }"""))
        except Exception:  # noqa: BLE001
            return False

    def _find_root(self, page: Page, timeout_ms: int = 15000):
        if self.adapter.settle_ms == 0:
            timeout_ms = min(timeout_ms, 600)
        deadline = time.monotonic() + timeout_ms / 1000
        poll_ms = 40 if self.adapter.settle_ms == 0 else 250
        while time.monotonic() < deadline:
            frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame and self.FRAME_RE.search(f.url or "")]
            for frame in frames:
                try:
                    if frame != page.main_frame and self._frame_clipped(frame):
                        continue
                    if not frame.query_selector(self.adapter.form_ready_selector):
                        continue
                    fields = frame.evaluate(SCAN_JS)
                    if any(f["kind"] == "file" or f["type"] == "email" or
                           re.search(r"first.?name|full.?name|^name$", f["label"], re.I) for f in fields):
                        if frame != page.main_frame:
                            self._log(f"Application form is inside iframe: {frame.url[:100]}")
                        return frame
                except Exception:  # noqa: BLE001
                    continue
            page.wait_for_timeout(poll_ms)
        return None

    def _captcha(self, page: Page) -> bool:
        roots = [page.main_frame]
        if self.root and self.root != page.main_frame:
            roots.append(self.root)
        for root in roots:
            cap = root.evaluate(CAPTCHA_JS)
            if cap.get("challenge") or cap.get("text"):
                return True
        return False

    def _run_page(self, ctx, page: Page, url: str) -> ApplyResult:
        response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if response and response.status >= 400:
            self._shot(page, "unavailable")
            return self._result("needs_human", f"The application page returned HTTP {response.status}; the job may be closed or unavailable.")
        page.wait_for_timeout(self.adapter.settle_ms)
        if page.url != url:
            self._log(f"Redirected to {page.url[:150]}")
        if self._captcha(page) and not self.assist:
            self._shot(page, "captcha")
            return self._result("needs_human", "A CAPTCHA or security check is visible. Choose Finish in browser to complete it yourself.")
        root = self._find_root(page, 3000)
        if root is None:
            for selector in self.adapter.apply_click_selectors:
                element = page.locator(selector).first
                if not element.count() or not element.is_visible():
                    continue
                self._log(f"Open application form: {selector}")
                before = set(ctx.pages)
                element.click(timeout=7000)
                page.wait_for_timeout(self.adapter.settle_ms)
                new_pages = [p for p in ctx.pages if p not in before]
                if new_pages:
                    page = new_pages[-1]
                    page.wait_for_load_state("domcontentloaded")
                break
            root = self._find_root(page, 9000)
        if root is None and self.adapter.name == "greenhouse":
            match = re.search(r"greenhouse\.io/([^/?&]+)/jobs/(\d+)", self.adapter.apply_url)
            if match:
                embed = f"https://boards.greenhouse.io/embed/job_app?for={match.group(1)}&token={match.group(2)}"
                self._log("Trying Greenhouse's standalone application form")
                page.goto(embed, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(self.adapter.settle_ms)
                root = self._find_root(page, 8000)
        if root is None and self.assist:
            # Nothing to fill (a check page, an unfamiliar form): the candidate can still finish it.
            self.root = page.main_frame
            return self._hand_off(page)
        if root is None:
            self._shot(page, "noform")
            return self._result("needs_human", "No supported application form found. The job may be closed, login-gated, or use a different flow.")
        self.root = root
        self._hide_banners(page)
        if self._captcha(page) and not self.assist:
            self._shot(page, "captcha")
            return self._result("needs_human", "A CAPTCHA or security check must be completed by you. Choose Finish in browser to do it.")

        # A choice may reveal follow-up questions. Stop after four bounded passes.
        for pass_no in range(1, 5):
            fields = self._scan_fields(page)
            self._reconcile_answered(fields)
            todo = [f for f in fields if f["idx"] not in self._tried]
            if not todo:
                break
            self._log(f"Pass {pass_no}: {len(todo)} fields to consider")
            self._fill_fields(page, todo)
            if self.adapter.settle_ms:
                page.wait_for_timeout(min(450, self.adapter.settle_ms))
        fields = self._scan_fields(page)
        if not fields:
            self._shot(page, "form-disappeared")
            return self._result("needs_human", "The application form disappeared after filling; no submission was attempted.")
        self._reconcile_answered(fields)
        self._collect_unanswered(fields)
        self._shot(page, "filled")
        required_missing = [f for f in fields if f["required"] and self._missing(f)]
        required_names = list(dict.fromkeys(f.get("groupLabel", f["label"]) for f in required_missing))
        invalid = [f for f in fields if f["invalid"] or f.get("ariaInvalid")]
        invalid_names = list(dict.fromkeys(f.get("groupLabel", f["label"]) for f in invalid))
        if invalid_names:
            self._log("Invalid after filling: " + "; ".join(invalid_names[:12]))
        if self.adapter.name == "ashby" and not self.dry_run:
            unsaved = self._resave_ashby(page, fields)
            if unsaved and not self.assist:
                names = [f.get("groupLabel", f["label"]) for f in unsaved]
                for f in unsaved:
                    self.question_details.append({"label": f.get("groupLabel", f["label"]), "kind": f["kind"],
                                                  "required": bool(f["required"]),
                                                  "options": list(f.get("options") or []), "invalid": True})
                self._shot(page, "unsaved")
                return self._result("needs_human", "The employer's form didn't save these answers, so nothing was "
                                                   "submitted: " + "; ".join(names[:8]))
        if self.assist:
            return self._hand_off(page)
        if self.dry_run:
            # A practice run predicts a real send: it names what would stop Submit.
            count = len(required_names)
            suffix = (f" {count} required question{'s' if count != 1 else ''} "
                      f"still need{'s' if count == 1 else ''} "
                      f"{'an answer' if count == 1 else 'answers'}.") if count else ""
            blocking = list(dict.fromkeys(required_names + invalid_names))
            if blocking:
                suffix += " A real send would stop at: " + "; ".join(blocking[:8]) + "."
            return self._result("dry_run", f"Dry run: filled {len(self._filled)} fields; nothing was submitted.{suffix}")
        if required_missing or invalid:
            names = list(dict.fromkeys(required_names + [f.get("groupLabel", f["label"]) for f in invalid]))
            return self._result("needs_human", "Required or invalid fields need attention: " + "; ".join(names[:8]))
        if self._captcha(page):
            self._shot(page, "captcha")
            return self._result("needs_human", "A CAPTCHA or security check must be completed by you. Choose Finish in browser to do it.")
        return self._submit(page)

    def _scan_fields(self, page: Page) -> list[dict]:
        """Resume parsing can temporarily disable every control on a form."""
        fields = self.root.evaluate(SCAN_JS)
        if fields or not self.adapter.settle_ms:
            return fields
        deadline = time.monotonic() + 10
        while (time.monotonic() < deadline and
               self.root.locator("input:disabled,textarea:disabled,select:disabled").count()):
            page.wait_for_timeout(200)
            fields = self.root.evaluate(SCAN_JS)
            if fields:
                break
        return fields

    @staticmethod
    def _signature(field: dict) -> tuple[str, str, str, str]:
        return (field["kind"], str(field.get("name") or ""), str(field.get("id") or ""),
                re.sub(r"\s+", " ", str(field["label"]).strip()).casefold())

    @staticmethod
    def _answer_retained(field: dict, expected: object) -> bool:
        if field["kind"] == "checkbox" and expected is False:
            return field.get("value") is False   # an unticked box is the answer, not an empty field
        if field.get("empty") or field.get("invalid") or field.get("ariaInvalid"):
            return False
        kind = field["kind"]
        if kind == "file":
            return field.get("fileName") == Path(str(expected)).name
        if kind == "checkbox":
            return field.get("value") is expected
        if kind in {"radio", "select"}:
            chosen = str(field.get("selectedOption") or "")
            return bool(chosen and pick_option([chosen], str(expected)))
        if kind == "combobox":
            chosen = str(field.get("value") or "")
            if chosen and pick_option([chosen], str(expected)):
                return True
            return bool(re.search(r"\bcity\b", field["label"], re.I) and
                        chosen.casefold().startswith(str(expected).casefold().strip() + ","))
        if field.get("datePicker"):
            actual, wanted = form_date(field.get("value")), form_date(expected)
            if wanted is None and (available := availability_date(field["label"], expected)):
                wanted = (available[0], True)
            return bool(actual and wanted and actual[0] == wanted[0])
        fmt = "number" if kind == "number" else {"email": "email", "url": "url"}.get(str(field.get("type") or ""))
        if fmt and (fitted := fit_answer(fmt, field["label"], str(expected))):
            expected = fitted[0]   # compare with what was typed ("$120k" was entered as 120000)
        if field.get("type") == "tel" or re.search(r"\b(?:phone|mobile|telephone)\b", field["label"], re.I):
            actual_digits = re.sub(r"\D", "", str(field.get("value") or ""))
            expected_digits = re.sub(r"\D", "", str(expected))
            return bool(expected_digits and actual_digits in {expected_digits, "1" + expected_digits})
        return str(field.get("value") or "").strip() == str(expected).strip()

    def _reconcile_answered(self, fields: list[dict]) -> None:
        """Retain evidence when a React form replaces fields after a choice."""
        from collections import Counter

        counts = Counter(self._signature(field) for field in fields)
        for field in fields:
            signature = self._signature(field)
            if counts[signature] != 1 or signature not in self._answered_signatures:
                continue
            if self._answer_retained(field, self._answered_signatures[signature]):
                self._filled.add(field["idx"])
                self._tried.add(field["idx"])
            else:
                self._filled.discard(field["idx"])
                self._tried.discard(field["idx"])

    def _missing(self, field: dict) -> bool:
        if field["kind"] == "checkbox" and "groupEmpty" in field:
            return bool(field["groupEmpty"]) or (field["required"] and not any(
                key in self._filled for key in field.get("groupMembers", [])))
        # A default selection or browser autofill is not evidence that an
        # unknown factual answer/attestation is true for this candidate.
        if field["required"] and field["idx"] not in self._filled:
            return True
        # React-select clears its search input after selection. A successful
        # click verified by _fill_combobox is sufficient when no value is exposed.
        if field["kind"] == "combobox" and field["idx"] in self._filled and not field.get("ariaInvalid"):
            return False
        return bool(field["empty"])

    def _collect_unanswered(self, fields: list[dict]) -> None:
        missing = [f for f in fields
                   if self._missing(f) and (f["required"] or (f["idx"] not in self._filled and f["kind"] not in {"file", "checkbox"}))]
        self.unanswered = list(dict.fromkeys(
            f.get("groupLabel", f["label"]) + (" (required)" if f["required"] else "") for f in missing))
        # The question text, type, and choices let a reviewer answer later
        # without reopening the employer's form. Values are never recorded.
        details: dict[str, dict] = {}
        # A filled field the form marks invalid blocks Submit too; report it so the
        # draft can ask for a different answer instead of stopping with nothing to edit.
        invalid = [f for f in fields if (f["invalid"] or f.get("ariaInvalid")) and f not in missing]
        for f in missing + invalid:
            label = f.get("groupLabel", f["label"])
            entry = details.setdefault(label, {"label": label, "kind": f["kind"], "required": bool(f["required"]),
                                               "options": list(f.get("options") or [])})
            if f in invalid:
                entry["invalid"] = True
            if "groupLabel" in f:
                entry["kind"] = "checkbox"
                member = f["label"].rpartition(" - ")[-1]
                if member and member not in entry["options"]:
                    entry["options"].append(member)
        self.question_details = list(details.values())

    def _fill_fields(self, page: Page, fields: list[dict]) -> None:
        questions = [Q(label=f["label"], kind=f["kind"], options=f.get("options") or [],
                       required=f["required"], name=f["name"], id=f["id"]) for f in fields]
        answers: dict[int, Answer | None] = {}
        unknown: list[tuple[int, Q]] = []
        for index, (field, question) in enumerate(zip(fields, questions)):
            self._tried.add(field["idx"])
            if question.kind == "file":
                answers[index] = self._file_answer(question)
                continue
            answer = rule_answer(question, self.profile, self.settings, self.job)
            answers[index] = answer
            if answer is None or answer.value in ("", None, []):
                unknown.append((index, question))
        # Native selects expose their choices during the scan, but ATS widgets
        # such as React Select and Greenhouse's custom dropdowns often render
        # options only after opening the control. Discover those choices before
        # asking the answer resolver, so an explicit opt-out can be selected
        # without inventing a residence or eligibility fact.
        inspect = list(unknown) + [(index, question) for index, question in enumerate(questions)
                                  if question.kind == "combobox" and answers.get(index) and
                                  str(answers[index].value).casefold() == "decline" and
                                  (index, question) not in unknown]
        for index, question in inspect:
            field = fields[index]
            if question.kind != "combobox":
                continue
            discovered = self._discover_combobox_options(page, field)
            if not discovered:
                if not question.required and answers.get(index) and str(answers[index].value).casefold() == "decline":
                    answers[index] = None  # Leave optional personal questions blank when no opt-out is offered.
                continue
            question.options = discovered
            answers[index] = rule_answer(question, self.profile, self.settings, self.job)
            if answers[index] is not None and answers[index].value not in ("", None, []):
                if (index, question) in unknown:
                    unknown.remove((index, question))
        if unknown:
            self._log(f"Checking known candidate facts for {len(unknown)} additional question(s)")
            found = llm_answers([q for _, q in unknown], self.profile, self.job, self.settings)
            for index, question in unknown:
                if question.label in found:
                    answers[index] = found[question.label]
        for index, (field, question) in enumerate(zip(fields, questions)):
            answer = answers.get(index)
            if answer is None or answer.value in ("", None, []):
                if question.required:
                    self._log(f"No known answer: {question.label[:100]} (required)")
                continue
            if not self._loc(page, field).count():
                self._log("Form changed after a previous answer; rescanning fields")
                for remaining in fields[index:]:
                    self._tried.discard(remaining["idx"])
                return
            try:
                if human_pacing():
                    page.wait_for_timeout(self._rng.randint(250, 750))
                self._fill_one(page, field, question, answer)
                self._filled.add(field["idx"])
                retained_value = answer.value
                if field["kind"] == "combobox":
                    # A location widget may display a canonical option such as
                    # "California" after the candidate supplied "CA". Keep
                    # the option actually selected as the rerender invariant.
                    try:
                        selected = next((current for current in self.root.evaluate(SCAN_JS)
                                         if current["idx"] == field["idx"]), None)
                        if selected and not selected["empty"] and selected.get("value"):
                            retained_value = selected["value"]
                    except Exception:  # A React rerender can briefly detach it.
                        pass
                self._answered_signatures[self._signature(field)] = retained_value
                self.answered[question.label] = str(answer.value)[:80]
                # Do not duplicate personal information into logs.
                self._log(f"Filled {question.kind}: {question.label[:100]} ({answer.source})")
            except Exception as exc:  # noqa: BLE001
                self._failures[field["idx"]] = str(exc).splitlines()[0][:160]
                self._log(f"Could not fill {question.label[:70]}: {self._failures[field['idx']]}")
                if "Timeout" in self._failures[field["idx"]] and not self._timeouts.get(field["idx"]):
                    # A form still settling (an overlay, a parse in progress) blocks clicks
                    # for a moment. Try once more in the next pass.
                    self._timeouts[field["idx"]] = 1
                    self._tried.discard(field["idx"])
                if not self._loc(page, field).count():
                    for remaining in fields[index:]:
                        self._tried.discard(remaining["idx"])
                    return
                try:
                    page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass

    def _discover_combobox_options(self, page: Page, field: dict) -> list[str]:
        """Open a choice widget briefly and return its currently visible options."""
        locator = self._loc(page, field)
        try:
            self._click(locator)
            if self.adapter.settle_ms:
                page.wait_for_timeout(min(250, self.adapter.settle_ms))
            options = self._visible_options()
            page.keyboard.press("Escape")
            return options
        except Exception as exc:  # noqa: BLE001
            self._log(f"Could not inspect choices for {field['label'][:70]}: {str(exc).splitlines()[0][:120]}")
            try:
                page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            return []

    def _file_answer(self, question: Q) -> Answer | None:
        # An exact candidate-supplied question answer may name a local file,
        # such as a transcript or cover letter. Never invent attachments.
        user_file = rule_answer(question, self.profile, self.settings, self.job)
        if user_file and isinstance(user_file.value, str) and Path(user_file.value).is_file():
            return user_file
        label = " ".join((question.label, question.id, question.name)).lower()
        if re.search(r"cover", label) and question.required and \
                (self.settings.get("answers") or {}).get("cover_letter_mode", "auto") == "auto":
            # A required cover letter is written from the candidate's own resume wording.
            from ..tailor import cover_letter_pdf
            return Answer(cover_letter_pdf(self.profile, self.job, self.settings), "rule")
        if re.search(r"cover|transcript|portfolio|other|additional|photo|picture", label):
            return None
        if re.search(r"autofill|auto-fill|parse", label) and not question.required:
            # Ashby's "Autofill from resume" only prefills the form. Its parsing locks the
            # fields for several seconds and can overwrite answers already entered.
            return None
        if re.search(r"resume|résumé|\bcv\b", label):
            return Answer(self.resume_path, "rule")
        return None

    def _loc(self, page: Page, field: dict):
        return self.root.locator(f'[data-jh="{field["idx"]}"]').first

    def _fill_one(self, page: Page, field: dict, question: Q, answer: Answer) -> None:
        kind, value = question.kind, answer.value
        locator = self._loc(page, field)
        if kind == "file":
            locator.set_input_files(str(value))
            if self.adapter.settle_ms:
                page.wait_for_timeout(min(1000, self.adapter.settle_ms))
            return
        if kind in {"text", "number", "textarea"} and field.get("datePicker"):
            self._fill_date(locator, field, str(value))
            return
        if kind in {"text", "number", "textarea"}:
            text = str(value)
            fmt = {"number": "number", "email": "email", "url": "url"}.get(
                "number" if kind == "number" else str(field.get("type") or ""))
            if fmt:
                fitted = fit_answer(fmt, question.label, text)
                if fitted is None:
                    raise RuntimeError(f"“{text}” is not {FORMAT_NAMES[fmt]}, which this field needs")
                text = str(fitted[0])
            if locator.input_value() == text:
                pass   # already correct (for example, the form's own resume autofill)
            elif human_pacing():
                # Clear first: typing appends to any existing value, and a click
                # can leave the cursor in the middle of prefilled text.
                locator.fill("")
                self._click(locator)
                if len(text) <= 300:
                    self._type(locator, text)
                else:   # long answers: paste most of it, type the end, like pasting a drafted essay
                    locator.fill(text[:-12])
                    self._type(locator, text[-12:])
                if locator.input_value() != text:
                    locator.fill(text)   # a widget reformatted keystrokes; set the exact value
            else:
                locator.fill(text)
            locator.press("Tab")
            if not locator.count():
                raise RuntimeError("The field was replaced while entering the answer")
            if locator.input_value() != text and not locator.input_value().strip():
                raise RuntimeError("The form did not retain the supplied value")
            return
        if kind == "select":
            option = pick_option(question.options, str(value))
            if option is None and CATALOG_RE.search(question.label):
                # A school missing from the form's list is truthfully "Other".
                option = next((o for o in question.options if re.fullmatch(r"\s*other\s*", o, re.I)), None)
                if option:
                    self._log(f"{question.label}: not in the form's list, chose Other")
            if option is None:
                raise RuntimeError("No unambiguous matching option")
            # Lists enhanced by select2 or Chosen keep the real <select> visually hidden.
            locator.select_option(label=option, force=True)
            locator.evaluate("e => { if (window.jQuery && e.classList.contains('chosen-select')) "
                             "window.jQuery(e).trigger('chosen:updated'); }")   # redraw Chosen's stand-in
            return
        if kind == "checkbox":
            if not isinstance(value, bool):
                raise RuntimeError("Checkbox answers must be an explicit true or false value")
            try:
                locator.set_checked(value, timeout=5000, force=True)
            except Exception:  # noqa: BLE001 - custom-styled boxes (Lever) ignore a positioned click
                if locator.is_checked() != value:
                    locator.evaluate("el => el.click()")
            if locator.is_checked() != value:
                # Some widgets toggle only through their label.
                locator.evaluate("el => { const label = el.labels?.[0] || el.closest('label'); if (label) label.click(); }")
            if locator.is_checked() != value:
                raise RuntimeError("The checkbox did not retain the requested state")
            return
        if kind == "radio":
            option = pick_option(question.options, str(value))
            if option is None:
                raise RuntimeError("No unambiguous matching radio option")
            index = question.options.index(option)
            radio = self.root.locator(f'[data-jh="{field["optionIdx"][index]}"]').first
            if field.get("ariaOptions"):
                native = radio.locator('input[type=radio]')
                if native.count():
                    native.evaluate("el => el.click()")
                else:
                    radio.click()
                if radio.get_attribute("aria-checked") != "true":
                    raise RuntimeError("The radio choice was not retained")
                return
            if field.get("buttonOptions"):
                if radio.get_attribute("aria-pressed") == "true":
                    radio.click()   # clicking a pressed choice clears it; enter it fresh so it saves again
                    page.wait_for_timeout(150)
                radio.click()
                if radio.get_attribute("aria-pressed") != "true":
                    raise RuntimeError("The yes/no choice was not retained")
                return
            try:
                radio.check(timeout=4000, force=True)
            except Exception:  # noqa: BLE001
                radio.evaluate("el => { const label = el.labels?.[0]; if (label) label.click(); }")
            if not radio.is_checked():
                raise RuntimeError("The radio option was not selected")
            return
        if kind == "combobox":
            if isinstance(value, list):   # a multi-select list: pick each chosen value
                for item in value:
                    self._fill_combobox(page, locator, str(item), question)
                return
            self._fill_combobox(page, locator, str(value), question)
            return
        raise RuntimeError(f"Unsupported field kind: {kind}")

    def _type(self, locator, text: str) -> None:
        """Type like a person. The time limit grows with the text: a 250-character answer
        at human speed takes longer than the default 7-second action limit."""
        delay = self._rng.randint(18, 45) if human_pacing() else 0
        locator.press_sequentially(text, delay=delay, timeout=max(7000, len(text) * (delay + 30) + 5000))

    def _fill_date(self, locator, field: dict, text: str) -> None:
        """Type a date into a date picker so the widget registers it.

        Setting the value directly (``fill``) shows the text but leaves a
        react-datepicker empty, and the employer then rejects the field. The
        date is typed as MM/DD/YYYY and committed with Tab, never Enter, which
        could submit the form.
        """
        parsed = form_date(text)
        if parsed is None and (available := availability_date(str(field.get("label") or ""), text)):
            # A saved "Flexible" or "2 weeks notice" becomes the date it means, as of today.
            parsed = (available[0], True)
            self._log(f"Entered “{text}” as {available[0]:%m/%d/%Y} ({available[1]})")
        if field.get("nativeDate"):
            if parsed is None:
                raise RuntimeError(f"“{text}” is not a date this field accepts")
            locator.fill(parsed[0].isoformat())
            return
        typed = parsed[0].strftime("%m/%d/%Y") if parsed else text
        if not (parsed and (current := form_date(locator.input_value())) and current[0] == parsed[0]):
            locator.fill("")
            self._click(locator)
            self._type(locator, typed)
            locator.press("Tab")
        retained = form_date(locator.input_value())
        if parsed is None or retained is None or retained[0] != parsed[0]:
            raise RuntimeError(f"The date picker did not accept “{text}”")
        if not parsed[1]:
            self._log(f"Entered {text} as {typed}: the form needs a full date")

    # Greenhouse/Ashby react-select options, ARIA options, and Lever's location suggestions.
    OPT_SEL = ('[role="option"]:visible,[class*="select__option"]:visible,[class*="option" i][id*="option" i]:visible,'
               '.dropdown-location:visible')

    @staticmethod
    def _search(locator, text: str) -> None:
        """Type into a search-as-you-type field. Lever runs its search on key presses, so a
        value set directly never shows suggestions: the last character is a real keystroke."""
        locator.fill(text[:-1])
        if text:
            locator.press_sequentially(text[-1], delay=30)

    def _await_options(self, page: Page, timeout_ms: int | None = None) -> list[str]:
        """Options of a searchable list, waiting for results that load as you type."""
        if timeout_ms is None:
            timeout_ms = 400 if self.adapter.settle_ms == 0 else 3000
        deadline = time.monotonic() + timeout_ms / 1000
        poll_ms = 40 if self.adapter.settle_ms == 0 else 150
        options = self._visible_options()
        while time.monotonic() < deadline and (not options or any(re.search(r"^loading|searching", o, re.I)
                                                                    for o in options)):
            page.wait_for_timeout(poll_ms)
            options = self._visible_options()
        return [o for o in options if not re.search(r"^loading|searching", o, re.I)]

    def _visible_options(self) -> list[str]:
        options = self.root.locator(self.OPT_SEL)
        return [text for text in (options.nth(i).inner_text().strip() for i in range(min(options.count(), 80)))
                if text and not re.search(r"^no (options|results)", text, re.I)]

    def _hide_banners(self, page: Page) -> None:
        """Hide cookie and consent banners on the page and in the form's frame (see HIDE_BANNERS_JS)."""
        hidden = 0
        for scope in dict.fromkeys([page.main_frame, getattr(self.root, "page", None) and self.root]):
            if scope is None:
                continue
            try:
                hidden += int(scope.evaluate(HIDE_BANNERS_JS) or 0)
            except Exception:  # noqa: BLE001 - a frame may be detached or the root a locator
                pass
        if hidden:
            self._log("Hid a cookie banner covering the form (no consent given)")

    def _click_option(self, text: str) -> None:
        options = self.root.locator(self.OPT_SEL)
        for index in range(min(options.count(), 80)):
            if options.nth(index).inner_text().strip() == text:
                if options.nth(index).get_attribute("aria-disabled") == "true":
                    raise RuntimeError("The matching option is disabled")
                self._click(options.nth(index))   # never scroll first: many lists close on scroll
                return
        raise RuntimeError("The matching option disappeared")

    def _region_option(self, options: list[str], region: str) -> str | None:
        """The one option that is exactly "<region>, <the candidate's country>"."""
        countries = {self.profile.country.strip().casefold()} - {""}
        us = {"united states", "united states of america", "us", "usa"}
        if countries & us or region.strip().casefold() in {name.casefold() for name in US_STATES.values()}:
            countries |= us
        matches = [option for option in options
                   if len(parts := [part.strip().casefold() for part in option.split(",")]) == 2
                   and parts[0] == region.strip().casefold() and parts[1] in countries]
        return matches[0] if len(matches) == 1 else None

    def _current_location_option(self, options: list[str]) -> str | None:
        """Select the candidate's exact city, region, and country if unique."""
        city = self.profile.city.strip().casefold()
        region = (self.profile.state or self.profile.region).strip()
        country = self.profile.country.strip().casefold()
        if not city or not country:
            return None
        regions = {region.casefold(), US_STATES.get(region.upper(), region).casefold()} if region else set()
        countries = {country}
        if country in {"united states", "united states of america", "us", "usa"}:
            countries.update({"united states", "united states of america", "us", "usa"})
        matches = []
        for option in options:
            parts = [part.strip().casefold() for part in option.split(",")]
            if len(parts) != 3 or parts[0] != city or parts[2] not in countries:
                continue
            if regions and parts[1] not in regions:
                continue
            matches.append(option)
        return matches[0] if len(matches) == 1 else None

    def _fill_combobox(self, page: Page, locator, value: str, question: Q) -> None:
        self._click(locator)
        editable = locator.evaluate("el => el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable")
        if self.adapter.settle_ms:
            page.wait_for_timeout(min(200, self.adapter.settle_ms))
        # select2-style lists open a separate search box when clicked.
        search = locator if editable else self._open_search_box()
        options = self._visible_options()
        chosen = self._pick(options, value)
        location = bool(re.search(r"location|\bcity\b|where (?:are you|do you|will you|would you)", question.label, re.I))
        current_location = bool(re.search(
            r"current(?:ly)? location|where (?:are you|do you) (?:currently )?(?:located|based|live)|^location",
            question.label, re.I,
        )) or (location and bool(self.profile.city) and self.profile.city.casefold() in value.casefold())
        if chosen is None and search is not None:
            self._search(search, self.profile.city if current_location and self.profile.city else value)
            options = self._await_options(page)
            chosen = self._pick(options, value)
            if chosen is None and current_location:
                chosen = self._current_location_option(options)
            if chosen is None and re.search(r"\bcity\b", question.label, re.I) and \
                    value.casefold().strip() == self.profile.city.casefold().strip():
                country = self.profile.country.casefold().strip()
                matches = [option for option in options
                           if option.casefold().startswith(value.casefold().strip() + ",") and
                           option.casefold().endswith(", " + country)]
                if len(matches) == 1:
                    chosen = matches[0]
            if chosen is None and re.search(r"\bstate\b|province|\bregion\b", question.label, re.I) and \
                    not re.search(r"\bcity\b", question.label, re.I):
                # A location search answers "California" with places named California;
                # the state itself is listed as "California, United States".
                chosen = self._region_option(options, value)
            # Catalog searches (schools, degrees) often match only a shorter prefix:
            # "California Polytechnic" finds "California Polytechnic State University - San Luis Obispo".
            words = re.sub(r"[^\w&' ]+", " ", value).split()
            searched = {value.casefold()}
            for prefix in dict.fromkeys(" ".join(words[:n]) for n in (len(words), 3, 2, 1) if 0 < n <= len(words)):
                if chosen is not None:
                    break
                if prefix.casefold() in searched:
                    continue
                searched.add(prefix.casefold())
                self._search(search, prefix)
                options = self._await_options(page)
                chosen = self._pick(options, value)
                if chosen is None and current_location:
                    chosen = self._current_location_option(options)
            if chosen is None and current_location and self.profile.country:
                # Some location pickers list countries only.
                self._search(search, self.profile.country)
                options = self._await_options(page)
                chosen = self._pick(options, self.profile.country)
            if chosen is None and CATALOG_RE.search(question.label):
                # A school or field missing from the catalog is truthfully "Other".
                self._search(search, "Other")
                options = self._await_options(page)
                others = [option for option in options
                          if re.fullmatch(r"\s*other\s*", option.split("\n")[0], re.I)]
                if len(others) == 1:
                    chosen = others[0]
                    self._log(f"{question.label}: not in the form's list, chose Other")
        if chosen is None:
            # Closed lists are answerable only against their actual options.
            if search is not None:
                search.fill("")
                if self.adapter.settle_ms:
                    page.wait_for_timeout(min(250, self.adapter.settle_ms))
            options = self._visible_options() or options
            actual = Q(question.label, "select", options, question.required, question.name, question.id)
            answer = rule_answer(actual, self.profile, self.settings, self.job)
            if answer and answer.value is not None:
                chosen = self._pick(options, str(answer.value))
        if chosen is None:
            if search is not None:
                search.fill("")
            page.keyboard.press("Escape")
            raise RuntimeError("No unambiguous option matched the candidate's answer")
        self._click_option(chosen)
        if self.adapter.settle_ms:
            page.wait_for_timeout(min(250, self.adapter.settle_ms))
        if locator.get_attribute("aria-invalid") == "true":
            raise RuntimeError("The form rejected the selected option")

    @staticmethod
    def _pick(options: list[str], value: str) -> str | None:
        """Match an option, also by its first line: Ashby lists schools as
        "University of California, Berkeley / United States / berkeley.edu"."""
        chosen = pick_option(options, value)
        if chosen is None:
            first: dict[str, str] = {}
            for option in options:
                first.setdefault(option.split("\n")[0].strip(), option)
            hit = pick_option(list(first), value)
            chosen = first[hit] if hit else None
        return chosen

    def _open_search_box(self):
        """The search box a select2-style list shows once opened, if any."""
        boxes = self.root.locator(".select2-search__field:visible, .select2-container--open input:visible, "
                                  "input[role=searchbox]:visible, input[type=search]:visible")
        return boxes.first if boxes.count() else None

    def _click(self, locator, timeout: int = 5000) -> None:
        """Click a field; when a sticky header or overlay intercepts the click, scroll it into
        view and click through, then fall back to focusing it."""
        try:
            locator.click(timeout=timeout)
            return
        except Exception:  # noqa: BLE001
            pass
        try:
            locator.scroll_into_view_if_needed(timeout=2000)
            locator.click(timeout=2000, force=True)
        except Exception:  # noqa: BLE001
            locator.focus()

    def _page_text(self, page: Page) -> str:
        texts = [page.inner_text("body")[:60000]]
        if self._root_attached(page) and self.root not in (page, page.main_frame):
            try:
                texts.append(self.root.inner_text("body")[:60000])
            except Exception:  # A navigation may detach the old frame.
                pass
        return "\n".join(texts)

    def _root_attached(self, page: Page) -> bool:
        """Return whether the current form root can still be queried."""
        if not self.root:
            return False
        if self.root is page or self.root is page.main_frame:
            return True
        try:
            return not self.root.is_detached()
        except AttributeError:
            # Page-like test doubles and Playwright Pages do not expose
            # Frame.is_detached, but remain valid query roots.
            return True
        except Exception:
            return False

    def _confirmation_signals(self, page: Page) -> set[str]:
        return {match.group(0).casefold() for match in self.adapter.success_re.finditer(self._page_text(page))}

    def _allowed_hosts(self) -> set[str]:
        hosts = {
            "greenhouse.io", "boards.greenhouse.io", "job-boards.greenhouse.io",
            "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io",
            "ashbyhq.com", "jobs.ashbyhq.com",
            "lever.co", "jobs.lever.co", "jobs.eu.lever.co",
            "127.0.0.1", "localhost",
        }
        for url_attr in (getattr(self.adapter, "apply_url", None),
                         getattr(self.job, "apply_url", None),
                         getattr(self.job, "url", None)):
            if url_attr:
                try:
                    h = urlsplit(url_attr).hostname
                    if h:
                        hosts.add(h.lower())
                except Exception:
                    pass
        return hosts

    def _is_allowed_host(self, host: str | None, allowed_hosts: set[str] | None = None) -> bool:
        if not host:
            return False
        host = host.lower()
        if allowed_hosts is None:
            allowed_hosts = self._allowed_hosts()
        for allowed in allowed_hosts:
            allowed = allowed.lower()
            if host == allowed or host.endswith("." + allowed):
                return True
        return False

    def _email_verification_challenge(self, page: Page) -> str | None:
        """Detect whether page presents a verification challenge: 'code', 'link', or None."""
        text = self._page_text(page)
        if self._verification_fields(page, None):
            return "code"
        if re.search(
            r"(?:verification|security|confirmation|one[- ]time) code (?:was sent|has been sent|is sent|sent to)|enter the \d+[- ]character code|enter (?:the )?(?:verification|security|confirmation|one[- ]time) code|we sent a code to|check your email for a code",
            text, re.I
        ):
            return "code"
        if re.search(
            r"(?:verification|confirmation) link (?:has been |was )?sent|check your email to (?:confirm|verify|complete)|click the link (?:in|sent to) (?:your )?email|we sent a (?:verification|confirmation) email to|verify your email (?:address )?(?:to )?(?:continue|complete)|an email with a verification link",
            text, re.I
        ):
            return "link"
        return None

    def _email_verification_required(self, page: Page) -> bool:
        """Detect an employer's email verification step before timeout."""
        return self._email_verification_challenge(page) is not None

    def _verification_fields(self, page: Page, length: int | None = None):
        """Locate a recognizable one-time-code widget without touching other fields."""
        roots = [page, *(frame for frame in page.frames if frame != page.main_frame)]
        selectors = (
            "input[autocomplete='one-time-code']",
            "input[name='verification_code' i]",
            "input[id='verification_code' i]",
            "input[name='otp' i]",
            "input[id='otp' i]",
            "input[aria-label*='verification code' i]",
            "input[aria-label*='one-time code' i]",
        )
        for root in roots:
            for selector in selectors:
                locator = root.locator(selector)
                visible = [locator.nth(index) for index in range(locator.count())
                           if locator.nth(index).is_visible() and locator.nth(index).is_enabled()]
                if len(visible) == 1 and visible[0].get_attribute("maxlength") != "1":
                    return visible
            if length is not None:
                locator = root.locator("input[maxlength='1']")
                visible = [locator.nth(index) for index in range(locator.count())
                           if locator.nth(index).is_visible() and locator.nth(index).is_enabled()]
                if len(visible) == length:
                    return visible
            # A generic name="code" is acceptable only with an explicit OTP
            # label. Never mistake another field such as postal_code for it.
            locator = root.locator("input[name='code' i], input[id='code' i]")
            visible = [locator.nth(index) for index in range(locator.count())
                       if locator.nth(index).is_visible() and locator.nth(index).is_enabled()]
            if len(visible) == 1 and visible[0].get_attribute("maxlength") != "1":
                label = visible[0].evaluate("e => [...(e.labels || [])].map(l => l.textContent).join(' ') + ' ' + (e.getAttribute('aria-label') || '')")
                if re.search(r"verification|one[- ]time|security|email.*code|otp", label, re.I):
                    return visible
        return []

    def _log_email_confirmation(self, page: Page, signal: str) -> None:
        parts = urlsplit(page.url)
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        self._log(f"Employer confirmation after email verification: {url} [{signal}]")

    def _complete_email_code_verification(self, page: Page, code: str, confirmation_baseline: set[str]) -> ApplyResult:
        if not self.submission_attempted:
            db.mark_submission_attempted(self.job.id)
            self.submission_attempted = True
        try:
            fields = self._verification_fields(page, len(code))
            if len(fields) == 1:
                fields[0].fill(code)
                if fields[0].input_value() != code:
                    raise RuntimeError("The code field did not retain the code")
            elif len(fields) == len(code):
                for field, character in zip(fields, code):
                    field.fill(character)
                if "".join(field.input_value() for field in fields) != code:
                    raise RuntimeError("The code fields did not retain the code")
            else:
                raise RuntimeError("No recognizable code input was found")
            self._log("Filled a fresh email verification code")
            if self.adapter.settle_ms:
                page.wait_for_timeout(min(500, self.adapter.settle_ms))
            signals = self._confirmation_signals(page) - confirmation_baseline
            if signals:
                self._log_email_confirmation(page, sorted(signals)[0])
                self.unanswered = [item for item in self.unanswered if "verification" not in item.lower()]
                return self._result("applied", "Application submitted. An employer confirmation appeared after email verification.")
            root = fields[0].locator("xpath=ancestor::form[1]")
            if not root.count():
                root = page
            candidates = root.locator("button[type=submit], input[type=submit], button")
            button = next((candidate for index in range(candidates.count())
                           if (candidate := candidates.nth(index)).is_visible()
                           and candidate.is_enabled()
                           and re.search(r"submit|verify|confirm|continue", (
                               candidate.inner_text() or candidate.get_attribute("value") or ""), re.I)), None)
            if button is None:
                raise RuntimeError("No verification Submit button was found")
            errors_before = set(page.evaluate(ERROR_JS))
            button.click(timeout=10000)
            deadline = time.monotonic() + min(float(self.settings.get("submission_timeout_sec", 35)), 35)
            poll_ms = 40 if self.adapter.settle_ms == 0 else 500
            while time.monotonic() < deadline:
                signals = self._confirmation_signals(page) - confirmation_baseline
                if signals:
                    self._log_email_confirmation(page, sorted(signals)[0])
                    self.unanswered = [item for item in self.unanswered if "verification" not in item.lower()]
                    return self._result("applied", "Application submitted. An employer confirmation appeared after email verification.")
                if set(page.evaluate(ERROR_JS)) - errors_before:
                    return self._result("needs_human", "The employer reported an error after email verification. Check the application manually before any retry.")
                if self._captcha(page):
                    break
                page.wait_for_timeout(poll_ms)
        except Exception:
            return self._result("needs_human", "The email code step could not be completed safely. Check the employer form manually.")
        return self._result("needs_human", "The email code was entered, but no employer confirmation appeared. Check manually before retrying.")

    def _complete_email_link_verification(self, page: Page, link: str, confirmation_baseline: set[str]) -> ApplyResult:
        if not self.submission_attempted:
            db.mark_submission_attempted(self.job.id)
            self.submission_attempted = True
        try:
            parsed = urlsplit(link)
        except Exception:
            return self._result("needs_human", "The verification link is malformed.")
        if parsed.scheme not in ("http", "https", "file"):
            return self._result("needs_human", "The verification link has an invalid scheme.")
        if parsed.scheme in ("http", "https") and not self._is_allowed_host(parsed.hostname):
            self._log(f"Rejected verification link to untrusted host: {parsed.hostname}")
            return self._result("needs_human", "The verification link points to an untrusted domain. Complete verification manually.")

        redacted_link = redact_token(link, kind="link")
        self._log(f"Navigating to verification link: {redacted_link}")
        verify_page = page.context.new_page()
        try:
            verify_page.goto(link, wait_until="domcontentloaded", timeout=25000)
            verify_host = (urlsplit(verify_page.url).hostname or "").lower()
            if parsed.scheme in ("http", "https") and not self._is_allowed_host(verify_host):
                self._log(f"Verification link redirected to untrusted host: {verify_host}")
                return self._result("needs_human", "The verification link redirected to an untrusted domain.")

            if self.adapter.settle_ms:
                verify_page.wait_for_timeout(min(1000, self.adapter.settle_ms))
            signals = self._confirmation_signals(verify_page) - confirmation_baseline
            if signals:
                self._log_email_confirmation(verify_page, sorted(signals)[0])
                self.unanswered = [item for item in self.unanswered if "verification" not in item.lower()]
                return self._result("applied", "Application submitted. An employer confirmation appeared after email verification.")

            candidates = verify_page.locator("button[type=submit], input[type=submit], button, a.btn, a.button")
            action_btn = next((candidates.nth(i) for i in range(candidates.count())
                               if candidates.nth(i).is_visible() and candidates.nth(i).is_enabled()
                               and re.search(r"confirm|verify|complete|submit|continue",
                                             (candidates.nth(i).inner_text() or candidates.nth(i).get_attribute("value") or ""), re.I)), None)
            if action_btn is not None:
                action_btn.click(timeout=10000)
                if self.adapter.settle_ms:
                    verify_page.wait_for_timeout(min(1000, self.adapter.settle_ms))
                signals = self._confirmation_signals(verify_page) - confirmation_baseline
                if signals:
                    self._log_email_confirmation(verify_page, sorted(signals)[0])
                    self.unanswered = [item for item in self.unanswered if "verification" not in item.lower()]
                    return self._result("applied", "Application submitted. An employer confirmation appeared after email verification.")

            page.bring_to_front()
            if self.adapter.settle_ms:
                page.wait_for_timeout(min(1000, self.adapter.settle_ms))
            signals = self._confirmation_signals(page) - confirmation_baseline
            if signals:
                self._log_email_confirmation(page, sorted(signals)[0])
                self.unanswered = [item for item in self.unanswered if "verification" not in item.lower()]
                return self._result("applied", "Application submitted. An employer confirmation appeared after email verification.")
        except Exception as exc:
            self._log(f"Verification link processing failed: {exc}")
            return self._result("needs_human", "The verification link could not be processed safely. Check the application manually.")
        finally:
            try:
                verify_page.close()
            except Exception:
                pass
        return self._result("needs_human", "The verification link was opened, but no employer confirmation appeared. Check manually before retrying.")

    def _complete_email_verification(self, page: Page, inbox: EmailVerificationInbox,
                                     baseline_data: Baseline | tuple[int, datetime] | None,
                                     confirmation_baseline: set[str]) -> ApplyResult:
        if baseline_data is None:
            return self._result("needs_human", "The email verification baseline was missing. Complete the employer form manually.")
        if isinstance(baseline_data, Baseline):
            baseline_uid = baseline_data.max_uid
            started_at = baseline_data.started_at
            uidvalidity = baseline_data.uidvalidity
        else:
            baseline_uid, started_at = baseline_data
            uidvalidity = None

        text = self._page_text(page)
        match = re.search(r"enter the (\d+)-character code", text, re.I)
        length = int(match.group(1)) if match else None

        allowed_hosts = self._allowed_hosts()
        employer = self.job.company if self.job else None
        try:
            if hasattr(inbox, "wait_for_token"):
                token = inbox.wait_for_token(baseline_uid, started_at, baseline_uidvalidity=uidvalidity,
                                             length=length, employer=employer, allowed_hosts=allowed_hosts)
            elif hasattr(inbox, "wait_for_code"):
                code_val = inbox.wait_for_code(baseline_uid, started_at, length=length or 8)
                token = VerificationToken(kind="code", value=code_val) if code_val else None
            else:
                token = None
        except Exception:
            return self._result("needs_human", "The verification mailbox could not identify one fresh code. Complete the employer form manually.")
        if not token:
            return self._result("needs_human", "No matching fresh email verification arrived in time. Complete the employer form manually.")

        if token.kind == "link":
            return self._complete_email_link_verification(page, token.value, confirmation_baseline)
        return self._complete_email_code_verification(page, token.value, confirmation_baseline)

    def _watch_ashby_saves(self, page: Page) -> None:
        def on_request(request) -> None:
            if "ApiSetFormValue" not in request.url:
                return
            try:
                path = json.loads(request.post_data or "{}")["variables"]["path"]
            except (TypeError, ValueError, KeyError):
                return
            self._ashby_saves[str(path)] = request   # the latest save for that field counts
        page.on("request", on_request)
        page.on("requestfinished", lambda request: self._settled.add(id(request)))
        page.on("requestfailed", lambda request: self._settled.add(id(request)))

    def _ashby_unsaved(self, page: Page, fields: list[dict]) -> list[dict]:
        """Filled fields whose latest Ashby save failed, or yes/no buttons that never saved."""
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and any(id(r) not in self._settled for r in self._ashby_saves.values()):
            page.wait_for_timeout(250)
        failed: set[str] = set()
        for path, request in self._ashby_saves.items():
            try:
                response = None if request.failure or id(request) not in self._settled else request.response()
                body = response.json() if response is not None and response.status < 400 else None
            except Exception:  # noqa: BLE001
                body = None
            if not isinstance(body, dict) or body.get("errors"):
                failed.add(path)
        out = []
        for field in fields:
            if field["idx"] not in self._filled or not field.get("name") or field["kind"] == "file":
                continue
            if field["name"] in failed or (field.get("buttonOptions") and field["name"] not in self._ashby_saves):
                out.append(field)
        return out

    def _resave_ashby(self, page: Page, fields: list[dict]) -> list[dict]:
        """Enter unsaved answers once more; returns fields whose save still failed."""
        unsaved = self._ashby_unsaved(page, fields)
        if not unsaved:
            return []
        self._log("Ashby didn't save: " + "; ".join(f.get("groupLabel", f["label"]) for f in unsaved[:8])
                  + ". Entering them again")
        for field in unsaved:
            self._filled.discard(field["idx"])
            self._tried.discard(field["idx"])
        self._fill_fields(page, unsaved)
        still = self._ashby_unsaved(page, self._scan_fields(page))
        # A yes/no button with no save request at all may simply not use one; only a refused save blocks.
        return [f for f in still if f["name"] in self._ashby_saves]

    def _hand_off(self, page: Page) -> ApplyResult:
        """Give the filled form to the candidate and wait for the employer's confirmation.

        The candidate may press Submit at any moment, so the submit boundary is
        recorded first. Starployment never clicks Submit in this mode.
        """
        baseline = self._confirmation_signals(page)
        if not self.submission_attempted:
            db.mark_submission_attempted(self.job.id)
            self.submission_attempted = True
        inbox = baseline_data = None
        if bool(self.settings.get("email_verification", {}).get("enabled")):
            try:
                inbox = EmailVerificationInbox(self.settings["email_verification"], self.profile.email)
                baseline_data = inbox.baseline()
            except Exception:  # noqa: BLE001
                inbox = None
                self._log("Automatic email verification is unavailable; enter any code yourself")
        self._log("Handed the form to you in a browser window; waiting for you to press Submit")
        try:
            page.evaluate(HANDOFF_BANNER_JS)
            page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        deadline = time.monotonic() + ASSIST_SECONDS
        code_tried = False
        while time.monotonic() < deadline:
            try:
                if page.is_closed():
                    break
                if self._confirmation_signals(page) - baseline:
                    self._shot(page, "success")
                    return self._result("applied", "You submitted it in the browser, and the employer's confirmation appeared.")
                if inbox is not None and not code_tried and self._email_verification_required(page):
                    code_tried = True
                    self._log("The employer asked for an email code; reading it from your inbox")
                    result = self._complete_email_verification(page, inbox, baseline_data, baseline)
                    if result.status == "applied":
                        return result
                page.wait_for_timeout(1000)
            except Exception:  # noqa: BLE001 - the window was closed, or a navigation detached the form
                if page.is_closed():
                    break
                time.sleep(1)
        else:
            self._shot(page, "unconfirmed")
            return self._result("needs_human", "No employer confirmation appeared within "
                                f"{ASSIST_SECONDS // 60} minutes. If you submitted it, check your email, then Mark as sent.")
        return self._result("needs_human", "The browser was closed before an employer confirmation appeared. "
                                           "If you submitted it, check your email, then Mark as sent.")

    def _submit(self, page: Page) -> ApplyResult:
        button = None
        for selector in self.adapter.submit_selectors:
            candidates = self.root.locator(selector)
            for index in range(candidates.count()):
                candidate = candidates.nth(index)
                if candidate.is_visible() and candidate.is_enabled():
                    button = candidate
                    break
            if button is not None:
                break
        baseline = self._confirmation_signals(page)
        verification_before = self._email_verification_required(page)
        inbox = None
        baseline_data = None
        inbox_unavailable = False
        if bool(self.settings.get("email_verification", {}).get("enabled")):
            try:
                inbox = EmailVerificationInbox(self.settings["email_verification"], self.profile.email)
                baseline_data = inbox.baseline()
            except Exception:
                inbox_unavailable = True
                self._log("Automatic email verification is unavailable; continuing with one ordinary Submit")

        if button is None:
            if self._email_verification_required(page):
                self._log("Email verification required before Submit button is available")
                if inbox is not None:
                    res = self._complete_email_verification(page, inbox, baseline_data, baseline)
                    if res.status == "applied":
                        return res
                    for selector in self.adapter.submit_selectors:
                        candidates = self.root.locator(selector)
                        for index in range(candidates.count()):
                            candidate = candidates.nth(index)
                            if candidate.is_visible() and candidate.is_enabled():
                                button = candidate
                                break
                        if button is not None:
                            break
                elif inbox_unavailable:
                    return self._result("needs_human", "The employer requested email verification, but the configured mailbox or system credential vault was unavailable. Complete the employer form manually; submission is not confirmed.")
                else:
                    return self._result("needs_human", "The employer requires email verification before submission. Complete it on the form manually.")
        if button is None:
            return self._result("needs_human", "An enabled application Submit button was not found.")
        button.scroll_into_view_if_needed()
        if human_pacing():
            # Review the form briefly and move to the button before clicking.
            remaining = MIN_PAGE_SECONDS - (time.monotonic() - self._opened_at)
            page.mouse.wheel(0, -400)
            page.wait_for_timeout(int(max(0.0, remaining) * 1000) + self._rng.randint(600, 1500))
            button.scroll_into_view_if_needed()
            button.hover()
            page.wait_for_timeout(self._rng.randint(300, 800))
        self._shot(page, "before_submit")
        # Record this durably before clicking. An interrupted or unconfirmed
        # request must never be automatically submitted a second time.
        if not self.submission_attempted:
            db.mark_submission_attempted(self.job.id)
            self.submission_attempted = True
        self._log("Submitting application once")
        button.click(timeout=10000)
        deadline = time.monotonic() + float(self.settings.get("submission_timeout_sec", 35))
        poll_ms = 40 if self.adapter.settle_ms == 0 else 500
        while time.monotonic() < deadline:
            try:
                if self._confirmation_signals(page) - baseline:
                    self._shot(page, "success")
                    return self._result("applied", "Application submitted. A new application confirmation was detected on the page.")
                if not verification_before and self._email_verification_required(page):
                    challenge = self._email_verification_challenge(page)
                    unanswered_label = "Email verification link (required)" if challenge == "link" else "Email verification code (required)"
                    self.unanswered = list(dict.fromkeys([*self.unanswered, unanswered_label]))
                    self._shot(page, "email_verification")
                    if inbox is not None:
                        return self._complete_email_verification(page, inbox, baseline_data, baseline)
                    if inbox_unavailable:
                        return self._result("needs_human", "The employer requested an email code, but the configured mailbox or system credential vault was unavailable. Complete the employer form manually; submission is not confirmed.")
                    msg = ("The employer sent an email verification link. Open it to complete verification; the application is not confirmed yet."
                           if challenge == "link" else
                           "The employer sent an email verification code, and the application is not confirmed yet. Choose Finish in browser to enter it, or turn on Finish email verification in Settings → Connections.")
                    return self._result("needs_human", msg)
                if self._captcha(page):
                    self._shot(page, "captcha")
                    return self._result("needs_human", "A CAPTCHA or security check appeared after Submit. Check your email for a confirmation; if there is none, choose Finish in browser to solve it yourself.")
                if self._root_attached(page):
                    errors = self.root.evaluate(ERROR_JS)
                    if errors:
                        self._shot(page, "errors")
                        if SPAM_RE.search(" ".join(errors)):
                            return self._result(
                                "needs_human",
                                "The employer's spam filter rejected this automated submission, so it was not "
                                "received. Open the posting and submit it yourself; your answers are in this "
                                "application. (Form message: " + " | ".join(errors[:2])[:240] + ")")
                        return self._result("needs_human", "The form reported: " + " | ".join(errors[:6]))
            except Exception:  # A navigation may briefly detach the previous form.
                pass
            page.wait_for_timeout(poll_ms)
        self._shot(page, "unconfirmed")
        return self._result("needs_human", "Submit was clicked, but no new confirmation was detected. Check the employer's confirmation or email before any retry.")

    def _result(self, status: str, message: str) -> ApplyResult:
        self._log(f"Result: {status}: {message}")
        return ApplyResult(status=status, message=message, screenshots=self.screenshots, log=self.log,
                           unanswered=self.unanswered, resume_path=self.resume_path,
                           submission_attempted=self.submission_attempted, questions=self.question_details)
