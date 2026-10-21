"""Per-job resume drafts: prioritize saved profile facts and render a PDF.

Truthfulness guard: summary wording and factual bullets come from the saved profile. Job text is
used only to rank that material, never as a source of candidate claims.
Falls back to the original resume PDF whenever the structured profile is too thin to render well.
"""
from __future__ import annotations

import re
import os
import shutil
from pathlib import Path

from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import (BaseDocTemplate, Frame, HRFlowable, KeepInFrame, PageTemplate, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

from . import config, db
from .models import Job, Profile


STOP = {"and", "the", "with", "for", "code", "data", "team", "work", "using", "our", "you", "will", "that", "this",
        "from", "have", "are", "your", "all", "can", "web", "new", "build", "tools", "suite", "studio", "engineer",
        "engineering", "software", "systems", "system", "experience"}


def _words(s: str) -> list[str]:
    """Lowercase terms, keeping tech spellings (c++, c#, node.js) but not a sentence's final period."""
    return [t for t in (w.rstrip(".") for w in re.findall(r"[a-z0-9+#.]{2,}", (s or "").lower())) if len(t) > 1]


def _tokens(s: str) -> set[str]:
    return {t for t in _words(s) if t not in STOP}


def _relevance(text: str, jd_tokens: set[str]) -> float:
    t = _tokens(text)
    return len(t & jd_tokens) / (len(t) ** 0.5 + 1)




def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def polish_bullets(bullets: list[str], job: Job, profile: Profile) -> list[str]:
    """Compatibility entrypoint: preserve every fact by preserving the wording.

    Comparing numbers alone cannot catch invented employers, credentials, scope,
    technology, or achievements. Tailoring therefore selects and reorders only.
    """
    return list(bullets)


def tailored_headline(profile: Profile, job: Job) -> str:
    """Use only the candidate's own headline, never the target job title."""
    return profile.headline or ""


def tailored_summary(profile: Profile, job: Job, limit: int = 2) -> str:
    """Return the most job-relevant sentences from the candidate's saved summary.

    This is extractive: job-description wording is never inserted into candidate claims.
    If the profile has no summary, omit this section rather than inventing one.
    """
    source = (profile.summary or "").strip()
    if not source:
        return ""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", source) if part.strip()]
    if not sentences:
        return ""
    jd = _tokens(job.title + " " + (job.description or ""))
    ranked = sorted(enumerate(sentences), key=lambda item: (-_relevance(item[1], jd), item[0]))
    return " ".join(sentence for _, sentence in ranked[:max(1, limit)])


# ------------------------------------------------------------------ one-page resume builder
# The candidate keeps a large resume library (any number of jobs, projects,
# and skills) in the profile. For each role, the builder ranks that library
# against the posting and drops the least relevant material until the PDF
# fits on exactly one page. It selects and orders; it never rewrites.

WEIGHT_TITLE = 3.0
SYNONYMS = {
    "ml": "machine learning", "ai": "artificial intelligence", "nlp": "natural language processing",
    "llm": "large language models", "k8s": "kubernetes", "js": "javascript", "ts": "typescript",
    "quant": "quantitative", "swe": "software", "postgres": "postgresql", "gcp": "google cloud",
}


def _job_weights(job: Job) -> dict[str, float]:
    """Token weights for a posting: title terms count most, frequent JD terms more."""
    weights: dict[str, float] = {}
    for token in _tokens(job.title):
        weights[token] = weights.get(token, 0.0) + WEIGHT_TITLE
    description = (job.description or "")[:20000].lower()
    counts: dict[str, int] = {}
    for token in _words(description):
        if token not in STOP:
            counts[token] = counts.get(token, 0) + 1
    for token, count in counts.items():
        weights[token] = weights.get(token, 0.0) + min(2.0, 0.6 + 0.35 * count)
    for short, long in SYNONYMS.items():
        if short in weights:
            for part in long.split():
                weights.setdefault(part, weights[short] * 0.6)
    return weights


def _score_text(text: str, weights: dict[str, float]) -> float:
    tokens = _tokens(text)
    if not tokens:
        return 0.0
    return sum(weights.get(t, 0.0) for t in tokens) / (len(tokens) ** 0.5 + 1)


def _year(value: str) -> int:
    if re.search(r"present|current|now", value or "", re.I):
        return 9999
    years = re.findall(r"(?:19|20)\d{2}", value or "")
    return int(years[-1]) if years else 0


def _degree_rank(degree: str) -> int:
    d = (degree or "").lower()
    if re.search(r"\bph\.?d\b|doctor", d):
        return 4
    if re.search(r"\bm\.?\s*s\.?\b|\bm\.?\s*a\.?\b|master|mba|\bm\.?\s*eng\.?\b", d):
        return 3
    if re.search(r"\bb\.?\s*s\.?\b|\bb\.?\s*a\.?\b|bachelor|\bb\.?\s*eng\.?\b", d):
        return 2
    if re.search(r"\bassociate\b|\ba\.?\s*s\.?\b|\ba\.?\s*a\.?\b", d):
        return 1
    return 0


class _Item:
    """A ranked resume entry and the bullets currently selected for it."""

    def __init__(self, kind: str, index: int, source, score: float, bullet_scores: list[float],
                 relevance: float, bullet_relevance: list[float]):
        self.kind, self.index, self.source, self.score = kind, index, source, score
        self.bullet_scores = bullet_scores
        self.relevance, self.bullet_relevance = relevance, bullet_relevance
        order = sorted(range(len(source.bullets)), key=lambda i: (-bullet_scores[i], i))
        self.ranked_bullets = order
        self.keep: set[int] = set()

    @property
    def bullets(self) -> list[str]:
        order = sorted(self.keep, key=lambda i: (-self.bullet_relevance[i], i))
        return [self.source.bullets[i] for i in order]


class ResumePlan:
    def __init__(self, profile: Profile, job: Job, settings: dict | None = None,
                 selection: dict | None = None):
        settings = settings or {}
        builder = settings.get("resume_builder") or {}
        self.profile, self.job = profile, job
        self.font_scale = 1.0
        self.weights = _job_weights(job)
        self.reason = "Selected by Luna" if selection else "Ranked by relevance to this role"
        self.headline = tailored_headline(profile, job)
        self.summary = tailored_summary(profile, job)
        newest_edu = max((_year(e.end) or _year(e.start) for e in profile.education), default=0)

        def _edu_score(e):
            text = ' '.join([e.school, e.degree, e.field, *e.details])
            rel = _score_text(text, self.weights)
            yr = _year(e.end) or _year(e.start)
            recency = max(0.0, 0.2 - 0.05 * max(0, newest_edu - yr)) if newest_edu and yr else 0.0
            return rel + recency

        def _edu_sort_key(e):
            rank = _degree_rank(f"{e.degree} {e.field}")
            yr = _year(e.end) or _year(e.start)
            return (-rank, -yr, -_edu_score(e))

        self.education = sorted(profile.education, key=_edu_sort_key)[:3]
        self.max_skills = int(builder.get("max_skills", 28) or 28)
        self.experience = self._rank("experience", profile.experience, selection, 4)
        self.projects = self._rank("projects", profile.projects, selection, 3)
        # Saved tools from selected entries are also evidence, even when the
        # candidate did not repeat them in the library's general skills list.
        saved_skills = list(dict.fromkeys(profile.skills + [
            skill for item in self.experience + self.projects for skill in item.source.tech
        ]))
        ranked_skills = sorted(saved_skills, key=lambda s: -_score_text(s, self.weights))
        chosen = (selection or {}).get("skills") or []
        chosen = [s for s in chosen if s in saved_skills]
        ordered = sorted(dict.fromkeys(chosen + ranked_skills), key=lambda s: -_score_text(s, self.weights))
        self.skills = ordered[:self.max_skills]
        self.awards = sorted(profile.awards, key=lambda a: -_score_text(a, self.weights))[:4]

    def _rank(self, kind: str, entries: list, selection: dict | None, bullets_each: int) -> list[_Item]:
        items: list[_Item] = []
        newest = max((_year(getattr(e, "end", "")) for e in entries), default=0)
        picked = list((selection or {}).get(kind) or [])
        for index, entry in enumerate(entries):
            if getattr(entry, "hidden", False):
                continue
            text = " ".join([getattr(entry, "title", "") or getattr(entry, "role", ""),
                             getattr(entry, "name", "") or getattr(entry, "company", ""),
                             " ".join(entry.tech), " ".join(entry.bullets)])
            relevance = score = _score_text(text, self.weights)
            if kind == "experience" and newest:
                year = _year(entry.end)
                score += 0.4 if year == 9999 else max(0.0, 0.3 - 0.1 * max(0, min(newest, 2100) - year))
            if index in picked:
                score += 100.0 + (len(picked) - picked.index(index))
            elif picked and not entry.pinned:
                continue  # Luna left it out; pinned entries always stay
            if entry.pinned:
                score += 1000.0
            bullet_relevance = [_score_text(b, self.weights) for b in entry.bullets]
            bullet_scores = list(bullet_relevance)
            chosen_bullets = ((selection or {}).get("bullets") or {}).get(f"{kind}:{index}") or []
            for i in chosen_bullets:
                if isinstance(i, int) and 0 <= i < len(bullet_scores):
                    bullet_scores[i] += 50.0
            item = _Item(kind, index, entry, score, bullet_scores, relevance, bullet_relevance)
            item.keep = set(item.ranked_bullets[:bullets_each])
            items.append(item)
        items.sort(key=lambda item: (-item.score, item.index))
        # Pinning and Luna affect what survives, while the displayed order
        # always reflects relevance to the posting.
        return sorted(items[:6 if kind == "experience" else 5], key=lambda item: (-item.relevance, item.index))

    # -- shrinking ---------------------------------------------------------
    def shrink(self) -> bool:
        """Drop the single least valuable piece of content. False when nothing is left to drop."""
        if len(self.awards) > 2:
            self.awards.pop()
            return True
        sections = [self.experience, self.projects]
        top = max([item.score for section in sections for item in section if item.score < 100] or [1.0]) or 1.0
        candidates: list[tuple[float, str, list, _Item, int]] = []
        for section in sections:
            for item in section:
                norm = min(1.0, item.score / top) if item.score < 100 else 1.0 + item.score / 1000
                kept = [i for i in item.ranked_bullets if i in item.keep]
                if len(kept) > 1:
                    worst = kept[-1]
                    rel = item.bullet_scores[worst]
                    best = max(item.bullet_scores) or 1.0
                    candidates.append((0.6 * min(1.0, rel / best) + 0.4 * norm, "bullet", section, item, worst))
                if len(section) > 1 and not item.source.pinned:
                    candidates.append((norm + 0.25, "item", section, item, -1))
        if len(self.skills) > 12:
            candidates.append((0.55, "skills", [], None, -1))
        if not candidates:
            return False
        _, kind, section, item, bullet = min(candidates, key=lambda c: c[0])
        if kind == "bullet":
            item.keep.discard(bullet)
        elif kind == "item":
            section.remove(item)
        else:
            del self.skills[-4:]
        return True

    def compact(self) -> bool:
        """Once ranking has nothing left to drop, remove optional extras one at a time."""
        if self.awards:
            self.awards.pop()
            return True
        if len(self.skills) > 8:
            del self.skills[-4:]
            return True
        if self.summary:
            sentences = re.split(r"(?<=[.!?])\s+", self.summary)
            self.summary = sentences[0] if len(sentences) > 1 else ""
            return True
        # Coursework, concentration, and activities are kept until nothing else is left.
        details = [(_score_text(text, self.weights), n, i) for n, e in enumerate(self.education)
                   for i, text in enumerate(e.details)]
        if details:
            _, n, i = min(details)
            e = self.education[n]
            self.education[n] = e.model_copy(update={"details": e.details[:i] + e.details[i + 1:]})
            return True
        return False


# ------------------------------------------------------------------ rendering
# Body text shares one left edge, dates sit flush right, and section titles carry
# a thin rule. Standard PDF fonts keep the text selectable and the file small.
PAGE_MARGIN_X = 0.55 * inch
PAGE_MARGIN_Y = 0.5 * inch
INK, MUTED, RULE = "#111111", "#111111", "#111111"
MIN_FONT_SCALE = 10 / 11
_MONTHS = {"january": "Jan", "february": "Feb", "march": "Mar", "april": "Apr", "may": "May", "june": "Jun",
           "july": "Jul", "august": "Aug", "september": "Sep", "sept": "Sep", "october": "Oct",
           "november": "Nov", "december": "Dec"}


_SERIF_FAMILY = "Times-Roman"
_SERIF_BOLD = "Times-Bold"
_SERIF_ITALIC = "Times-Italic"
_SERIF_BOLD_ITALIC = "Times-BoldItalic"


def _styles(scale: float = 1.0) -> dict:
    def style(name: str, font: str, size: float, leading: float, color: str = INK, **extra) -> ParagraphStyle:
        return ParagraphStyle(name, fontName=font, fontSize=size * scale, leading=leading * scale,
                               textColor=color, alignment=extra.pop("alignment", TA_LEFT),
                               **{key: value * scale for key, value in extra.items()})

    return {
        "name": style("name", _SERIF_BOLD, 20, 24, alignment=TA_CENTER),
        "contact": style("contact", _SERIF_FAMILY, 9.5, 12, spaceBefore=3, alignment=TA_CENTER),
        "head": style("head", _SERIF_ITALIC, 9.5, 12, spaceBefore=2, alignment=TA_CENTER),
        "h2": style("h2", _SERIF_BOLD, 11, 14, spaceBefore=9),
        "item": style("item", _SERIF_BOLD, 10.5, 13),
        "right": style("right", _SERIF_FAMILY, 9.5, 13, alignment=TA_RIGHT),
        "sub": style("sub", _SERIF_ITALIC, 9.5, 12),
        "subright": style("subright", _SERIF_ITALIC, 9.5, 12, alignment=TA_RIGHT),
        "bullet": style("bullet", _SERIF_FAMILY, 10, 13, leftIndent=13, bulletIndent=1),
        "body": style("body", _SERIF_FAMILY, 10, 13),
    }


def _when(value: str) -> str:
    """``August 2023`` → ``Aug 2023``; anything else is kept as written."""
    return re.sub(r"\b(" + "|".join(_MONTHS) + r")\b\.?", lambda m: _MONTHS[m.group(1).lower()],
                  (value or "").strip(), flags=re.I)


def _dates(start: str, end: str) -> str:
    parts = [_when(x) for x in (start, end) if (x or "").strip()]
    return " – ".join(dict.fromkeys(parts))


def _join(*parts: str, sep: str = " · ") -> str:
    return sep.join(p for p in parts if p)


def _story(plan: ResumePlan, scale: float = 1.0) -> list:
    profile, styles = plan.profile, _styles(scale)
    S: list = []
    S.append(Paragraph(_esc(profile.full_name or "Resume"), styles["name"]))
    contact = _join(profile.email, profile.phone and _fmt_phone(profile.phone), profile.location,
                    _short(profile.website), _short(profile.linkedin), _short(profile.github), sep=" | ")
    if contact:
        S.append(Paragraph(_esc(contact), styles["contact"]))
    if plan.headline:
        S.append(Paragraph(_esc(plan.headline), styles["head"]))

    def heading(text: str) -> None:
        S.append(Paragraph(text, styles["h2"]))
        S.append(HRFlowable(width="100%", thickness=0.5, color=RULE,
                            spaceBefore=1 * scale, spaceAfter=3 * scale))

    width = letter[0] - 2 * PAGE_MARGIN_X

    def right_width(right: str, style: ParagraphStyle) -> float:
        if not right:
            return 0.0
        return min(width * 0.48, max(0.9 * inch, stringWidth(right, style.fontName, style.fontSize) + 10 * scale))

    def one_line(markup: str, style: ParagraphStyle, avail: float) -> bool:
        para = Paragraph(markup, style)
        para.wrap(avail, 10_000)
        return len(para.blPara.lines) <= 1

    def row(left: str, right: str, sub: bool = False, left_style: ParagraphStyle | None = None) -> None:
        """One line of an entry: markup on the left, a date or place flush right."""
        default_left, right_style = (styles["sub"], styles["subright"]) if sub else (styles["item"], styles["right"])
        left_style = left_style or default_left
        if not right:
            S.append(Paragraph(left, left_style))
            return
        table = Table([[Paragraph(left, left_style), Paragraph(_esc(right), right_style)]],
                      colWidths=["*", right_width(right, right_style)], hAlign="LEFT")
        table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                   ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        S.append(table)

    def bullets(lines: list[str]) -> None:
        for line in lines:
            S.append(Paragraph(_esc(line), styles["bullet"], bulletText="•"))

    def gap() -> None:
        S.append(Spacer(1, 3.5 * scale))

    def tech(values: list[str]) -> list[str]:
        return sorted(values, key=lambda value: -_score_text(value, plan.weights))[:6]

    def entry(main: str, extra: str, right: str, details: list[str], sub_right: str = "",
              sep: str = " · ") -> None:
        """An entry's header, each line kept to one line. ``main`` (bold) and ``extra`` share
        the first line with ``right``; when they don't fit, ``extra`` leads the italic line
        below, and that line keeps only the most relevant ``details`` that fit."""
        head_avail = width - right_width(right, styles["right"])
        head, lead = _join(main, extra, sep=sep), ""
        if extra and not one_line(head, styles["item"], head_avail):
            head, lead = main, extra
        head_style = styles["item"]
        for shrink in (0.96, 0.92, 0.88):
            if one_line(head, head_style, head_avail):
                break
            head_style = ParagraphStyle(f"item{shrink}", parent=styles["item"],
                                        fontSize=styles["item"].fontSize * shrink)
        row(head, right, left_style=head_style)
        sub_avail = width - right_width(sub_right, styles["subright"])
        shown = list(details)
        while shown and not one_line(_join(lead, _esc(", ".join(shown))), styles["sub"], sub_avail):
            shown.pop()
        sub = _join(lead, _esc(", ".join(shown)))
        if sub or sub_right:
            row(sub, sub_right, sub=True)

    if plan.education:
        heading("Education")
        for n, e in enumerate(plan.education):
            if n:
                gap()
            raw_field = (e.field or "").strip()
            clean_field = raw_field.split("|")[0].strip() if "|" in raw_field else raw_field
            if clean_field and clean_field.lower() not in (e.degree or "").lower():
                deg = f"{e.degree} in {clean_field}" if e.degree else clean_field
            else:
                deg = e.degree or clean_field
            date_str = _when(e.end) or _when(e.start)
            r1_left = f"<b>{_esc(e.school)}</b>" if e.school else ""
            if e.location:
                r1_right = e.location
                r2_right = date_str
                r2_left = _esc(deg) + (f" · GPA {e.gpa}" if e.gpa else "")
            else:
                r1_right = date_str
                r2_right = f"GPA: {e.gpa}" if e.gpa else ""
                r2_left = _esc(deg)
            row(r1_left, r1_right)
            if r2_left or r2_right:
                row(r2_left, r2_right, sub=True)
            for text in sorted(e.details, key=lambda text: -_score_text(text, plan.weights)):
                label, sep, rest = text.partition(":")
                markup = (f"<b>{_esc(label)}:</b>{_esc(rest)}" if sep and rest.strip() and len(label) <= 40
                          else _esc(text))
                S.append(Paragraph(markup, styles["bullet"], bulletText="•"))

    if plan.summary and not (plan.experience or plan.projects):
        heading("Summary")
        S.append(Paragraph(_esc(plan.summary), styles["body"]))

    def emit(section: str, items: list[_Item], parts, sep: str = " · ") -> None:
        if not items:
            return
        heading(section)
        for n, item in enumerate(items):
            if n:
                gap()
            entry(*parts(item.source), sep=sep)
            bullets(item.bullets)

    emit("Projects", plan.projects,
         lambda x: (f"<b>{_esc(x.name)}</b>", _esc(x.role), _short(x.link) or _dates(x.start, x.end),
                    tech(x.tech), _dates(x.start, x.end) if x.link else ""))
    emit("Experience", plan.experience,
         lambda x: (_join(f"<b>{_esc(x.title)}</b>" if x.title else "", _esc(x.company), sep=", "),
                    _esc(x.location), _dates(x.start, x.end), tech(x.tech), ""), sep=" - ")
    if plan.skills:
        heading("Skills")
        S.append(Paragraph(_esc(", ".join(plan.skills)), styles["body"]))
    if plan.awards:
        heading("Awards & Activities")
        bullets(plan.awards)
    return S


def _document(plan: ResumePlan, target) -> BaseDocTemplate:
    profile = plan.profile
    doc = BaseDocTemplate(target, pagesize=letter, leftMargin=PAGE_MARGIN_X, rightMargin=PAGE_MARGIN_X,
                          topMargin=PAGE_MARGIN_Y, bottomMargin=PAGE_MARGIN_Y,
                          title=f"{profile.full_name} Resume", author=profile.full_name)
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="page",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="resume", frames=[frame])])
    return doc


def _render(plan: ResumePlan, target, fit: bool = False) -> int:
    """Write the PDF and return its page count. ``fit`` scales the whole page down
    if the content is still too long, so the result is always exactly one page."""
    doc = _document(plan, target)
    story = _story(plan, plan.font_scale)
    if fit:
        story = [KeepInFrame(doc.width, doc.height, story, mode="shrink")]
    doc.build(story)
    return int(doc.page)


def plan_resume(profile: Profile, job: Job, settings: dict | None = None,
                selection: dict | None = None) -> ResumePlan:
    """Rank the resume library for ``job`` and trim it to one page.

    Try 11-point body text, then resize down to 10 points before removing low-value
    content. The final fallback scales protected content without cutting sentences."""
    from io import BytesIO
    plan = ResumePlan(profile, job, settings, selection)
    if _render(plan, BytesIO()) <= 1:
        return plan
    plan.font_scale = MIN_FONT_SCALE
    for _ in range(400):
        if _render(plan, BytesIO()) <= 1:
            break
        if not plan.shrink() and not plan.compact():
            break
    # Use the largest readable size that fits the selected material.
    for scale in (1.0, 0.975, 0.95, 0.925):
        plan.font_scale = scale
        if _render(plan, BytesIO()) <= 1:
            return plan
    plan.font_scale = MIN_FONT_SCALE
    return plan


def build_resume(profile: Profile, job: Job, out_path: Path, use_llm: bool = False,
                 settings: dict | None = None, selection: dict | None = None) -> Path:
    """Write a role-specific PDF built only from the candidate's saved material."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plan = plan_resume(profile, job, settings, selection)
    _render(plan, str(out_path), fit=True)
    config.secure_file(out_path)
    return out_path


# ------------------------------------------------------------------ role-specific resume files
RESUME_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("auto", "Automatic"),
    ("software", "Software engineering"),
    ("data", "Data science & analytics"),
    ("finance", "Financial analysis"),
    ("ml", "Machine learning & AI"),
    ("quant", "Quant & trading"),
    ("product", "Product & program"),
    ("general", "General"),
)
_CATEGORY_TERMS = {
    "software": (r"software|\bswe\b|developer|backend|back-end|frontend|front-end|full[- ]?stack|platform|infrastructure|"
                 r"\bsre\b|reliability|mobile|ios|android|systems engineer|devops|security engineer|embedded", 3.0),
    "data": (r"data scien|\bds\b|data analy|analytics|analyst|statistic|experimentation|a/b|business intelligence|"
             r"\bbi\b|data engineer|insights", 3.0),
    "ml": (r"machine learning|\bml\b|\bai\b|artificial intelligence|deep learning|\bllm|nlp|computer vision|"
           r"research (?:scientist|engineer)|applied scientist|perception", 3.2),
    "quant": (r"quant|trading|trader|fintech|payments|risk|hedge|market maker|market making|"
              r"capital markets|banking|crypto|portfolio|securities|derivatives", 3.1),
    "finance": (r"financ(?:e|ial)|\bfp\s*&?\s*a\b|accounting|valuation|budget|forecast|investment analy|"
                r"equity research|credit analy|cash flow", 3.1),
    "product": (r"product manager|\bpm\b|program manager|product owner|technical program", 3.4),
}

_TITLE_CATEGORIES = (
    ("product", r"product manager|program manager|product owner|\bpm\b"),
    ("ml", _CATEGORY_TERMS["ml"][0]),
    ("data", r"data scien|data analy|data engineer|analytics|\bds\b|business intelligence|statistic"),
    ("quant", r"quant|\btrader\b|market mak"),
    ("finance", r"financ(?:e|ial) (?:analy|associate|manager)|\bfp\s*&?\s*a\b|investment analy|equity research|credit analy"),
    ("software", _CATEGORY_TERMS["software"][0]),
    ("finance", r"financ(?:e|ial)|\bfp\s*&?\s*a\b|account(?:ant|ing)|investment analy|equity research|credit analy"),
)


def classify_role(job: Job) -> str:
    """The resume role type that best fits a posting; ``general`` when unclear."""
    return _classify_role_text(job.title, job.description)


def _classify_role_text(title: str, body: str = "") -> str:
    title = (title or "").casefold().replace("_", " ")
    body = (body or "")[:6000].casefold()
    # The actual role takes precedence over an employer's industry or shared skills.
    for key, pattern in _TITLE_CATEGORIES:
        if re.search(pattern, title):
            return key
    best, best_score = "general", 0.0
    for key, (pattern, weight) in _CATEGORY_TERMS.items():
        score = weight * len(re.findall(pattern, title)) + 0.25 * min(8, len(re.findall(pattern, body)))
        if score > best_score:
            best, best_score = key, score
    return best if best_score >= 1.0 else "general"


def category_label(key: str) -> str:
    return dict(RESUME_CATEGORIES).get(key, key.replace("_", " ").title())


_TEXT_CACHE: dict[tuple[str, float], str] = {}


def _cached_extract_text(path: str | Path) -> str:
    """Extract text from a resume PDF with in-memory caching by path and mtime."""
    try:
        if not Path(path).is_file():
            return ""
        key = (str(path), os.path.getmtime(path))
        if key in _TEXT_CACHE:
            return _TEXT_CACHE[key]
        from .profile import extract_text

        text = extract_text(path)
        _TEXT_CACHE[key] = text
        return text
    except Exception:
        return ""


def resume_file_category(record: dict) -> str:
    """Infer a bank PDF's intended role locally, unless the candidate chose one."""
    category = str(record.get("category") or "auto")
    if category not in {"auto", "general"} and category in dict(RESUME_CATEGORIES):
        return category
    path = str(record.get("path") or "")
    # A renamed bank entry is the candidate's clearest statement of its purpose.
    for name in (str(record.get("label") or ""), Path(path).stem):
        detected = _classify_role_text(name.replace("-", " "))
        if detected != "general":
            return detected
    text = _cached_extract_text(path)
    # A professional headline is more useful than an isolated keyword deep in a PDF.
    heading = _classify_role_text(" ".join(text.splitlines()[:5]))
    return heading if heading != "general" else _classify_role_text("", text)


def choose_resume_file(job: Job, files: list[dict], fallback: str = "") -> tuple[str, str]:
    """Pick the most relevant uploaded resume file for ``job`` from the resume bank."""
    usable = [f for f in files if f.get("enabled", 1) and not f.get("removed", 0) and Path(str(f.get("path") or "")).is_file()]
    if not usable:
        if any(db._same_path(str(f.get("path") or ""), fallback) for f in files):
            return "", "No enabled resumes in Resume bank"
        return fallback, "Main resume"
    if len(usable) == 1:
        return str(usable[0]["path"]), f"{usable[0].get('label') or 'Resume'} · default resume"

    weights = _job_weights(job)
    category = classify_role(job)
    title_tokens = set(_words(job.title or ""))

    best_record = None
    best_score = (-1, -1.0, -1)
    best_relevance = 0.0

    for record in usable:
        text = _cached_extract_text(str(record.get("path") or ""))
        text_score = _score_text(text, weights) if text else 0.0

        role_match = int(category != "general" and resume_file_category(record) == category)

        label_tokens = set(_words(str(record.get("label") or "")))
        label_bonus = 1.5 * len(label_tokens & title_tokens)
        relevance = text_score + label_bonus
        total_score = (role_match, relevance, int(bool(record.get("is_default"))))

        if total_score > best_score:
            best_score = total_score
            best_relevance = role_match + relevance
            best_record = record

    best = best_record or usable[0]
    if best_relevance > 0.0:
        job_title = job.title or "role"
        note = f"{best.get('label') or 'Resume'} · matched {job_title} from resume bank"
    else:
        note = f"{best.get('label') or 'Resume'} · default resume"

    return str(best["path"]), note


def _fmt_phone(p: str) -> str:
    d = re.sub(r"\D", "", p)
    if len(d) == 11 and d.startswith("1"): d = d[1:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else p


def _short(u: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", u or "").rstrip("/")


def resume_choice(profile: Profile, job: Job, settings: dict, out_dir: Path | None = None,
                  selection: dict | None = None) -> tuple[str, str]:
    """Return ``(path, description)`` for the resume to upload for this job."""
    strategy = settings.get("resume_strategy", "tailored")
    if strategy in ("library", "bank"):
        path, note = choose_resume_file(job, db.list_resume_files(include_removed=True), profile.resume_path)
        return path, note
    rich = len(profile.projects) + len(profile.experience) >= 2 and profile.education
    if strategy == "tailored" and rich:
        directory = Path(out_dir) if out_dir else config.TAILORED
        out = directory / f"{_safe(profile.last_name or 'resume')}_{_safe(job.company)}_{job.id}.pdf"
        try:
            if selection is None and (settings.get("resume_builder") or {}).get("luna_select"):
                try:
                    from .luna import select_resume_content
                    selection = select_resume_content(profile, job, settings)
                except Exception as error:  # noqa: BLE001
                    db.add_event(f"Luna could not choose resume content; using local ranking: {error}", "warn", job.id)
                    selection = None
            build_resume(profile, job, out, use_llm=False, settings=settings, selection=selection)
            db.add_event(f"Tailored resume built for {job.company} / {job.title}", "info", job.id)
            return str(out), "One-page resume generated from your resume builder" + (" · chosen by Luna" if selection else "")
        except Exception as e:  # noqa: BLE001
            db.add_event(f"Tailoring failed, using original resume: {e}", "warn", job.id)
    return profile.resume_path, "Main resume"


def resume_for_job(profile: Profile, job: Job, settings: dict) -> str:
    """Return the resume path to upload for this job (generated, matched file, or original)."""
    return resume_choice(profile, job, settings)[0]


def snapshot_resume_for_application(resume_path: str, application_id: int) -> str:
    """Create or reuse a private, immutable resume copy for one application.

    Tailored drafts are stored at a deterministic per-job path and can be
    regenerated later. Copying into an application-specific path before browser
    work keeps the exact uploaded bytes available with the application history.
    An existing snapshot is never replaced, including on a retry using the same
    application ID.
    """
    if type(application_id) is not int or application_id <= 0:
        raise ValueError("A valid application ID is required for a resume snapshot")
    snapshot_dir = config.TAILORED / "applications"
    if snapshot_dir.is_symlink():
        raise ValueError("The application resume snapshot directory is not safe")
    snapshot_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    if os.name != "nt":
        snapshot_dir.chmod(0o700)
    destination = snapshot_dir / f"application-{application_id}.pdf"
    if destination.is_symlink():
        raise ValueError("The application resume snapshot is not a regular file")
    if destination.exists():
        if not destination.is_file():
            raise ValueError("The application resume snapshot is not a regular file")
        config.secure_file(destination)
        return str(destination)

    source = Path(resume_path)
    if source.is_symlink() or not source.is_file():
        raise FileNotFoundError("The resume PDF is missing or is not a regular file")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        config.secure_file(destination)
        if os.name != "nt":
            directory_fd = os.open(snapshot_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return str(destination)


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", s)[:24] or "x"


def profile_preview_resume(profile: Profile, settings: dict | None = None, out_path: Path | None = None) -> Path:
    """Build a one-page preview PDF generated directly from the candidate's profile."""
    settings = settings or {}
    targets = [t for t in (settings.get("target_titles") or []) if t and str(t).strip()]
    title = " / ".join(targets) if targets else (profile.headline or "Profile Preview")
    job = Job(
        id="preview",
        source="profile",
        company="Profile Preview",
        title=title,
        url="https://starployment.local/preview",
        description=" ".join(targets),
    )
    if out_path is None:
        out_dir = config.TAILORED / "previews"
        out_dir.mkdir(parents=True, exist_ok=True)
        name = _safe(profile.last_name or profile.first_name or "resume")
        out_path = out_dir / f"{name}_preview.pdf"
    return build_resume(profile, job, out_path, use_llm=False, settings=settings)


def cover_letter_pdf(profile: Profile, job: Job, settings: dict | None = None) -> str:
    """A one-page cover letter PDF for a form that requires one.

    The text is ``answers.cover_letter``: a short letter built from the
    candidate's own resume wording, nothing invented.
    """
    from .answers import cover_letter

    text = cover_letter(profile, job, settings or {})
    folder = config.TAILORED / "cover_letters"
    folder.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9]+", "_", f"{profile.full_name or 'Candidate'}_Cover_Letter_{job.company}").strip("_")
    path = folder / f"{name}.pdf"
    style = ParagraphStyle("letter", fontName=_SERIF_FAMILY, fontSize=11, leading=15, alignment=TA_LEFT, spaceAfter=10)
    doc = SimpleDocTemplate(str(path), pagesize=letter, leftMargin=inch, rightMargin=inch,
                            topMargin=inch, bottomMargin=inch, title=f"Cover letter: {job.company}")
    doc.build([Paragraph(_esc(part).replace("\n", "<br/>"), style) for part in text.split("\n\n") if part.strip()])
    config.secure_file(path)
    return str(path)
