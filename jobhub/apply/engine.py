"""Fill supported ATS forms from known candidate facts and verify submission.

Each attempt uses the local, dedicated browser profile. Unknown required answers,
visible challenges, invalid fields, and ambiguous submission outcomes stop the
attempt with a screenshot. No CAPTCHA bypass or guessed factual answers.
"""
from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Page, sync_playwright

from .. import config, db
from ..answers import Answer, Q, US_STATES, llm_answers, pick_option, rule_answer
from ..models import ApplyResult, Job, Profile
from .adapters import Adapter
from .email_verification import EmailVerificationInbox

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
  const ownLabel = e => {
    const labels = [...(e.labels || [])];
    if (labels.length) return labels.map(l => l.innerText || l.textContent).join(' ');
    if (e.getAttribute('aria-labelledby')) return e.getAttribute('aria-labelledby').split(' ').map(
      id => document.getElementById(id)?.innerText || '').join(' ');
    return e.getAttribute('aria-label') || '';
  };
  const placeholder = t => !t || /^(start typing|select|choose|pick|type|search|enter)\b/i.test(t.trim());
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
      e.closest('[role=group][aria-required=true],[role=radiogroup][aria-required=true],fieldset[aria-required=true]'));
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
  for (const e of scope.querySelectorAll('input,select,textarea,[role=combobox]')) {
    const type = (e.type || '').toLowerCase();
    if (e.disabled || e.getAttribute('aria-disabled') === 'true' ||
        ['hidden','submit','button','reset'].includes(type) && e.getAttribute('role') !== 'combobox') continue;
    if (seen.has(e)) continue;
    // Custom radios/checkboxes often hide the native control behind a visible label.
    const labeledVisible = [...(e.labels || [])].some(visible);
    if (type !== 'file' && !visible(e) && !(['radio','checkbox'].includes(type) && labeledVisible)) continue;
    const rawLabel = labelFor(e);
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
      rec.kind = 'radio'; rec.label = clean(question) || e.name || rec.label;
      rec.options = optionLabels; rec.optionIdx = all.map(idFor);
      rec.required = all.some(r => required(r, question)); rec.empty = !all.some(r => r.checked);
      rec.selectedOption = optionLabels[all.findIndex(r => r.checked)] || '';
      rec.invalid = all.some(r => r.willValidate && !r.validity.valid);
    }
    else if (type === 'checkbox') {
      rec.kind = 'checkbox'; rec.empty = !e.checked; rec.value = e.checked;
      const group = e.closest('fieldset[aria-required],.ashby-application-form-field-entry,.application-question');
      const members = group ? [...group.querySelectorAll('input[type=checkbox]')] : [];
      if (members.length > 1) {
        const title = group.querySelector('legend,.ashby-application-form-question-title,.application-label');
        rec.groupLabel = clean(title?.innerText) || 'Checkbox choices';
        rec.groupEmpty = !members.some(m => m.checked);
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
             /select__input|combobox|autocomplete/i.test(String(e.className)) || e.closest('[class*=select__control]')) {
      rec.kind = 'combobox';
      const box = e.closest('[class*=select__control]') || e.parentElement;
      const chosen = box?.querySelector('[class*=singleValue],[class*=single-value],[class*=multiValue]');
      if (chosen?.textContent.trim()) { rec.value = chosen.textContent.trim(); rec.empty = false; }
      if (e.tagName === 'BUTTON') { rec.value = clean(e.innerText); rec.empty = placeholder(rec.value); }
    }
    else if (['text','email','tel','url','number','search','date',''].includes(type)) rec.kind = type === 'number' ? 'number' : 'text';
    else continue;
    if (!rec.label) rec.label = rec.kind === 'file' ? 'Attachment' : 'Unlabeled field';
    out.push(rec);
  }
  return out;
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


class FormEngine:
    def __init__(self, job: Job, profile: Profile, settings: dict, resume_path: str, adapter: Adapter, dry_run: bool):
        self.job, self.profile, self.settings = job, profile, settings
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
        # user's regular browser. Its OS lock prevents concurrent browser use.
        with sync_playwright() as playwright:
            channel = self.settings.get("browser_channel") or None
            ctx = None
            page = None
            try:
                ctx = playwright.chromium.launch_persistent_context(
                    str(config.BROWSER_PROFILE), headless=config.HEADLESS,
                    channel=channel if channel != "chromium" else None,
                    viewport={"width": 1280, "height": 1000}, locale="en-US")
                ctx.set_default_timeout(7000)
                page = ctx.new_page()
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
        deadline = time.monotonic() + timeout_ms / 1000
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
            page.wait_for_timeout(250)
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
        if self._captcha(page):
            self._shot(page, "captcha")
            return self._result("needs_human", "A CAPTCHA or security check is visible. Open the job link to complete it manually.")
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
        if root is None:
            self._shot(page, "noform")
            return self._result("needs_human", "No supported application form found. The job may be closed, login-gated, or use a different flow.")
        self.root = root
        if self._captcha(page):
            self._shot(page, "captcha")
            return self._result("needs_human", "A CAPTCHA or security check must be completed manually.")

        # A choice may reveal follow-up questions. Stop after four bounded passes.
        for pass_no in range(1, 5):
            fields = self.root.evaluate(SCAN_JS)
            self._reconcile_answered(fields)
            todo = [f for f in fields if f["idx"] not in self._tried]
            if not todo:
                break
            self._log(f"Pass {pass_no}: {len(todo)} fields to consider")
            self._fill_fields(page, todo)
            page.wait_for_timeout(450)
        fields = self.root.evaluate(SCAN_JS)
        self._reconcile_answered(fields)
        self._collect_unanswered(fields)
        self._shot(page, "filled")
        required_missing = [f for f in fields if f["required"] and self._missing(f)]
        required_names = list(dict.fromkeys(f.get("groupLabel", f["label"]) for f in required_missing))
        invalid = [f for f in fields if f["invalid"] or f.get("ariaInvalid")]
        if self.dry_run:
            count = len(required_names)
            suffix = (f" {count} required question{'s' if count != 1 else ''} "
                      f"still need{'s' if count == 1 else ''} "
                      f"{'an answer' if count == 1 else 'answers'}.") if count else ""
            return self._result("dry_run", f"Dry run: filled {len(self._filled)} fields; nothing was submitted.{suffix}")
        if required_missing or invalid:
            names = list(dict.fromkeys(required_names + [f.get("groupLabel", f["label"]) for f in invalid]))
            return self._result("needs_human", "Required or invalid fields need attention: " + "; ".join(names[:8]))
        if self._captcha(page):
            self._shot(page, "captcha")
            return self._result("needs_human", "A CAPTCHA or security check must be completed manually.")
        return self._submit(page)

    @staticmethod
    def _signature(field: dict) -> tuple[str, str, str, str]:
        return (field["kind"], str(field.get("name") or ""), str(field.get("id") or ""),
                re.sub(r"\s+", " ", str(field["label"]).strip()).casefold())

    @staticmethod
    def _answer_retained(field: dict, expected: object) -> bool:
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
        for f in missing:
            label = f.get("groupLabel", f["label"])
            entry = details.setdefault(label, {"label": label, "kind": f["kind"], "required": bool(f["required"]),
                                               "options": list(f.get("options") or [])})
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
        for index, question in list(unknown):
            field = fields[index]
            if question.kind != "combobox":
                continue
            discovered = self._discover_combobox_options(page, field)
            if not discovered:
                continue
            question.options = discovered
            answers[index] = rule_answer(question, self.profile, self.settings, self.job)
            if answers[index] is not None and answers[index].value not in ("", None, []):
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
            locator.click(timeout=5000)
            page.wait_for_timeout(250)
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
        if re.search(r"cover|transcript|portfolio|other|additional|photo|picture", label):
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
            page.wait_for_timeout(1000)
            return
        if kind in {"text", "number", "textarea"}:
            locator.fill(str(value))
            locator.press("Tab")
            if locator.input_value() != str(value) and not locator.input_value().strip():
                raise RuntimeError("The form did not retain the supplied value")
            return
        if kind == "select":
            option = pick_option(question.options, str(value))
            if option is None:
                raise RuntimeError("No unambiguous matching option")
            locator.select_option(label=option)
            return
        if kind == "checkbox":
            if not isinstance(value, bool):
                raise RuntimeError("Checkbox answers must be an explicit true or false value")
            locator.set_checked(value, timeout=5000, force=True)
            if locator.is_checked() != value:
                raise RuntimeError("The checkbox did not retain the requested state")
            return
        if kind == "radio":
            option = pick_option(question.options, str(value))
            if option is None:
                raise RuntimeError("No unambiguous matching radio option")
            index = question.options.index(option)
            radio = self.root.locator(f'[data-jh="{field["optionIdx"][index]}"]').first
            if field.get("buttonOptions"):
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
            self._fill_combobox(page, locator, str(value), question)
            return
        raise RuntimeError(f"Unsupported field kind: {kind}")

    OPT_SEL = '[role="option"]:visible,[class*="select__option"]:visible,[class*="option" i][id*="option" i]:visible'

    def _visible_options(self) -> list[str]:
        options = self.root.locator(self.OPT_SEL)
        return [text for text in (options.nth(i).inner_text().strip() for i in range(min(options.count(), 80)))
                if text and not re.search(r"^no (options|results)", text, re.I)]

    def _click_option(self, text: str) -> None:
        options = self.root.locator(self.OPT_SEL)
        for index in range(min(options.count(), 80)):
            if options.nth(index).inner_text().strip() == text:
                if options.nth(index).get_attribute("aria-disabled") == "true":
                    raise RuntimeError("The matching option is disabled")
                options.nth(index).click()
                return
        raise RuntimeError("The matching option disappeared")

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
        locator.click(timeout=5000)
        editable = locator.evaluate("el => el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable")
        page.wait_for_timeout(200)
        options = self._visible_options()
        chosen = pick_option(options, value)
        if chosen is None and editable:
            current_location = bool(re.search(
                r"current(?:ly)? location|where (?:are you|do you) (?:currently )?(?:located|based|live)|^location",
                question.label, re.I,
            ))
            locator.fill(self.profile.city if current_location and self.profile.city else value)
            page.wait_for_timeout(900)
            options = self._visible_options()
            chosen = pick_option(options, value)
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
            if chosen is None and len(value.split()) > 1:
                locator.fill(" ".join(value.split()[:2]))
                page.wait_for_timeout(700)
                options = self._visible_options()
                chosen = pick_option(options, value)
        if chosen is None:
            # Closed lists are answerable only against their actual options.
            if editable:
                locator.fill("")
                page.wait_for_timeout(250)
            options = self._visible_options() or options
            actual = Q(question.label, "select", options, question.required, question.name, question.id)
            answer = rule_answer(actual, self.profile, self.settings, self.job)
            if answer and answer.value is not None:
                chosen = pick_option(options, str(answer.value))
        if chosen is None:
            if editable:
                locator.fill("")
            page.keyboard.press("Escape")
            raise RuntimeError("No unambiguous option matched the candidate's answer")
        self._click_option(chosen)
        page.wait_for_timeout(250)
        if locator.get_attribute("aria-invalid") == "true":
            raise RuntimeError("The form rejected the selected option")

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

    def _email_verification_required(self, page: Page) -> bool:
        """Detect an employer's post-submit email-code step before timeout."""
        return bool(re.search(
            r"(?:verification|security) code was sent to|enter the \d+-character code",
            self._page_text(page), re.I,
        ))

    def _verification_fields(self, page: Page, length: int):
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

    def _complete_email_verification(self, page: Page, inbox: EmailVerificationInbox,
                                     baseline_uid: int, started_at, confirmation_baseline: set[str]) -> ApplyResult:
        text = self._page_text(page)
        match = re.search(r"enter the (\d+)-character code", text, re.I)
        length = int(match.group(1)) if match else 8
        if not 4 <= length <= 12:
            return self._result("needs_human", "The employer requested an unsupported email code length.")
        try:
            code = inbox.wait_for_code(baseline_uid, started_at, length=length)
        except Exception:
            return self._result("needs_human", "The verification mailbox could not identify one fresh code. Complete the employer form manually.")
        if not code:
            return self._result("needs_human", "No matching fresh email code arrived in time. Complete the employer form manually.")
        try:
            fields = self._verification_fields(page, length)
            if len(fields) == 1:
                fields[0].fill(code)
                if fields[0].input_value() != code:
                    raise RuntimeError("The code field did not retain the code")
            elif len(fields) == length:
                for field, character in zip(fields, code):
                    field.fill(character)
                if "".join(field.input_value() for field in fields) != code:
                    raise RuntimeError("The code fields did not retain the code")
            else:
                raise RuntimeError("No recognizable code input was found")
            self._log("Filled a fresh Greenhouse email code")
            # Some code widgets submit automatically after the final character.
            page.wait_for_timeout(500)
            signals = self._confirmation_signals(page) - confirmation_baseline
            if signals:
                # A screenshot could persist the one-time code if the employer
                # leaves its widget visible beside the confirmation text.
                self._log_email_confirmation(page, sorted(signals)[0])
                self.unanswered = [item for item in self.unanswered if item != "Email verification code (required)"]
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
            # The employer may echo the secret in an error. Keep only a
            # categorical outcome, never its raw error text or a screenshot.
            errors_before = set(page.evaluate(ERROR_JS))
            # One post-code click only. An uncertain result is never retried.
            button.click(timeout=10000)
            deadline = time.monotonic() + min(float(self.settings.get("submission_timeout_sec", 35)), 35)
            while time.monotonic() < deadline:
                page.wait_for_timeout(700)
                signals = self._confirmation_signals(page) - confirmation_baseline
                if signals:
                    self._log_email_confirmation(page, sorted(signals)[0])
                    self.unanswered = [item for item in self.unanswered if item != "Email verification code (required)"]
                    return self._result("applied", "Application submitted. An employer confirmation appeared after email verification.")
                if set(page.evaluate(ERROR_JS)) - errors_before:
                    return self._result("needs_human", "The employer reported an error after email verification. Check the application manually before any retry.")
                if self._captcha(page):
                    break
        except Exception:
            return self._result("needs_human", "The email code step could not be completed safely. Check the employer form manually.")
        return self._result("needs_human", "The email code was entered, but no employer confirmation appeared. Check manually before retrying.")

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
        if button is None:
            return self._result("needs_human", "An enabled application Submit button was not found.")
        baseline = self._confirmation_signals(page)
        verification_before = self._email_verification_required(page)
        inbox = None
        baseline_uid = None
        started_at = None
        inbox_unavailable = False
        if self.adapter.name == "greenhouse" and self.settings.get("email_verification", {}).get("enabled"):
            try:
                inbox = EmailVerificationInbox(self.settings["email_verification"], self.profile.email)
                baseline_uid, started_at = inbox.baseline()
            except Exception:
                inbox_unavailable = True
                self._log("Automatic email verification is unavailable; continuing with one ordinary Submit")
        button.scroll_into_view_if_needed()
        self._shot(page, "before_submit")
        # Record this durably before clicking. An interrupted or unconfirmed
        # request must never be automatically submitted a second time.
        db.mark_submission_attempted(self.job.id)
        self.submission_attempted = True
        self._log("Submitting application once")
        button.click(timeout=10000)
        deadline = time.monotonic() + float(self.settings.get("submission_timeout_sec", 35))
        while time.monotonic() < deadline:
            page.wait_for_timeout(700)
            try:
                if self._confirmation_signals(page) - baseline:
                    self._shot(page, "success")
                    return self._result("applied", "Application submitted. A new application confirmation was detected on the page.")
                if not verification_before and self._email_verification_required(page):
                    self.unanswered = list(dict.fromkeys([*self.unanswered, "Email verification code (required)"]))
                    self._shot(page, "email_verification")
                    if inbox is not None:
                        return self._complete_email_verification(page, inbox, baseline_uid, started_at, baseline)
                    if inbox_unavailable:
                        return self._result("needs_human", "The employer requested an email code, but the configured mailbox or system credential vault was unavailable. Complete the employer form manually; submission is not confirmed.")
                    return self._result("needs_human", "The employer sent an email verification code. Enter it on the employer form and resubmit; the application is not confirmed yet.")
                if self._captcha(page):
                    self._shot(page, "captcha")
                    return self._result("needs_human", "A CAPTCHA or security check appeared after Submit. Check the application manually before trying again.")
                if self._root_attached(page):
                    errors = self.root.evaluate(ERROR_JS)
                    if errors:
                        self._shot(page, "errors")
                        return self._result("needs_human", "The form reported: " + " | ".join(errors[:6]))
            except Exception:  # A navigation may briefly detach the previous form.
                continue
        self._shot(page, "unconfirmed")
        return self._result("needs_human", "Submit was clicked, but no new confirmation was detected. Check the employer's confirmation or email before any retry.")

    def _result(self, status: str, message: str) -> ApplyResult:
        self._log(f"Result: {status}: {message}")
        return ApplyResult(status=status, message=message, screenshots=self.screenshots, log=self.log,
                           unanswered=self.unanswered, resume_path=self.resume_path,
                           submission_attempted=self.submission_attempted, questions=self.question_details)
