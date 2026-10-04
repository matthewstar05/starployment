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

from reportlab.lib.enums import TA_LEFT, TA_RIGHT
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
    base = profile.headline or ""
    title = re.sub(r"\s*[-–|(].*$", "", job.title).strip()
    if title and title.lower() not in base.lower():
        return f"{title} Candidate | " + base if base else title
    return base


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


class _Item:
    """A ranked resume entry and the bullets currently selected for it."""

    def __init__(self, kind: str, index: int, source, score: float, bullet_scores: list[float]):
        self.kind, self.index, self.source, self.score = kind, index, source, score
        self.bullet_scores = bullet_scores
        order = sorted(range(len(source.bullets)), key=lambda i: (-bullet_scores[i], i))
        self.ranked_bullets = order
        self.keep: set[int] = set()

    @property
    def bullets(self) -> list[str]:
        return [b for i, b in enumerate(self.source.bullets) if i in self.keep]  # original order


class ResumePlan:
    def __init__(self, profile: Profile, job: Job, settings: dict | None = None,
                 selection: dict | None = None):
        settings = settings or {}
        builder = settings.get("resume_builder") or {}
        self.profile, self.job = profile, job
        self.weights = _job_weights(job)
        self.reason = "Selected by Luna" if selection else "Ranked by relevance to this role"
        self.headline = tailored_headline(profile, job)
        self.summary = tailored_summary(profile, job)
        self.education = list(profile.education[:3])
        self.max_skills = int(builder.get("max_skills", 28) or 28)
        self.experience = self._rank("experience", profile.experience, selection, 4)
        self.projects = self._rank("projects", profile.projects, selection, 3)
        matched = [s for s in profile.skills if _tokens(s) & set(self.weights)]
        rest = [s for s in profile.skills if s not in matched]
        chosen = (selection or {}).get("skills") or []
        chosen = [s for s in chosen if s in profile.skills]
        ordered = list(dict.fromkeys(chosen + matched + rest))
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
            score = _score_text(text, self.weights)
            if kind == "experience" and newest:
                year = _year(entry.end)
                score += 0.4 if year == 9999 else max(0.0, 0.3 - 0.1 * max(0, min(newest, 2100) - year))
            if index in picked:
                score += 100.0 + (len(picked) - picked.index(index))
            elif picked and not entry.pinned:
                continue  # Luna left it out; pinned entries always stay
            if entry.pinned:
                score += 1000.0
            bullet_scores = [_score_text(b, self.weights) for b in entry.bullets]
            chosen_bullets = ((selection or {}).get("bullets") or {}).get(f"{kind}:{index}") or []
            for i in chosen_bullets:
                if isinstance(i, int) and 0 <= i < len(bullet_scores):
                    bullet_scores[i] += 50.0
            item = _Item(kind, index, entry, score, bullet_scores)
            item.keep = set(item.ranked_bullets[:bullets_each])
            items.append(item)
        items.sort(key=lambda item: (-item.score, item.index))
        return items[:6 if kind == "experience" else 5]

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
        if len(self.education) > 1:
            candidates.append((0.9, "education", [], None, -1))
        if not candidates:
            return False
        _, kind, section, item, bullet = min(candidates, key=lambda c: c[0])
        if kind == "bullet":
            item.keep.discard(bullet)
        elif kind == "item":
            section.remove(item)
        elif kind == "skills":
            del self.skills[-4:]
        else:
            self.education.pop()
        return True

    def compact(self) -> bool:
        """Once ranking has nothing left to drop, remove optional extras one at a time."""
        if self.awards:
            self.awards.pop()
            return True
        for education in self.education:
            if education.details:
                self.education = [e.model_copy(update={"details": []}) for e in self.education]
                return True
        if len(self.skills) > 8:
            del self.skills[-4:]
            return True
        if self.summary:
            sentences = re.split(r"(?<=[.!?])\s+", self.summary)
            self.summary = sentences[0] if len(sentences) > 1 else ""
            return True
        return False


# ------------------------------------------------------------------ rendering
# Everything sits on one left edge (the frame has no padding), entry rows span the full
# text width with dates flush right, and section titles carry a thin rule. Standard PDF
# fonts keep the file small and readable by every applicant tracking system.
PAGE_MARGIN_X = 0.55 * inch
PAGE_MARGIN_Y = 0.45 * inch
INK, MUTED, RULE = "#111111", "#4D4D4D", "#9A9A9A"
_MONTHS = {"january": "Jan", "february": "Feb", "march": "Mar", "april": "Apr", "may": "May", "june": "Jun",
           "july": "Jul", "august": "Aug", "september": "Sep", "sept": "Sep", "october": "Oct",
           "november": "Nov", "december": "Dec"}


def _styles(scale: float = 1.0) -> dict:
    def style(name: str, font: str, size: float, leading: float, color: str = INK, **extra) -> ParagraphStyle:
        return ParagraphStyle(name, fontName=font, fontSize=size * scale, leading=leading * scale,
                              textColor=color, alignment=extra.pop("alignment", TA_LEFT), **extra)

    return {
        "name": style("name", "Helvetica-Bold", 19, 22),
        "contact": style("contact", "Helvetica", 9, 11.5, MUTED, spaceBefore=2),
        "head": style("head", "Helvetica", 9.4, 11.8, MUTED, spaceBefore=1.5),
        "h2": style("h2", "Helvetica-Bold", 10, 12, spaceBefore=8),
        "item": style("item", "Helvetica", 9.7, 12),
        "right": style("right", "Helvetica", 9, 12, MUTED, alignment=TA_RIGHT),
        "sub": style("sub", "Helvetica-Oblique", 9, 11.4, MUTED),
        "subright": style("subright", "Helvetica-Oblique", 9, 11.4, MUTED, alignment=TA_RIGHT),
        "bullet": style("bullet", "Helvetica", 9.3, 11.6, leftIndent=11, bulletIndent=2),
        "body": style("body", "Helvetica", 9.3, 11.6),
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
                    _short(profile.website), _short(profile.linkedin), _short(profile.github))
    if contact:
        S.append(Paragraph(_esc(contact), styles["contact"]))
    if plan.headline:
        S.append(Paragraph(_esc(plan.headline), styles["head"]))

    # The date column is as wide as the widest date or place, so titles get the rest.
    right_texts = [_dates(e.start, e.end) for e in plan.education] + [e.location for e in plan.education]
    right_texts += [_dates(i.source.start, i.source.end) for i in plan.experience + plan.projects]
    right_texts += [i.source.location for i in plan.experience]
    widest = max((stringWidth(t, "Helvetica", 9 * scale) for t in right_texts if t), default=0)
    right_width = min(2.3 * inch, max(0.9 * inch, widest + 8))

    def heading(text: str) -> None:
        S.append(Paragraph(text.upper(), styles["h2"]))
        S.append(HRFlowable(width="100%", thickness=0.6, color=RULE, spaceBefore=1.5, spaceAfter=3.5))

    def row(left: str, right: str, sub: bool = False) -> None:
        """One line of an entry: markup on the left, a date or place flush right."""
        left_style, right_style = (styles["sub"], styles["subright"]) if sub else (styles["item"], styles["right"])
        if not right:
            S.append(Paragraph(left, left_style))
            return
        table = Table([[Paragraph(left, left_style), Paragraph(_esc(right), right_style)]],
                      colWidths=["*", right_width], hAlign="LEFT")
        table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                   ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        S.append(table)

    def bullets(lines: list[str]) -> None:
        for line in lines:
            S.append(Paragraph(_esc(line), styles["bullet"], bulletText="•"))

    def gap() -> None:
        S.append(Spacer(1, 3.5 * scale))

    def tech(values: list[str]) -> str:
        return _esc(", ".join(values[:6]))

    def role_line(title: str, extra: str) -> str:
        """The second line of an entry: the role in ink, supporting detail muted."""
        return _join(f"<font color='{INK}'>{_esc(title)}</font>" if title else "", extra)

    if plan.summary:
        heading("Summary")
        S.append(Paragraph(_esc(plan.summary), styles["body"]))

    if plan.education:
        heading("Education")
        for n, e in enumerate(plan.education):
            if n:
                gap()
            degree = e.degree + (f" in {e.field}" if e.field and e.field.lower() not in e.degree.lower() else "")
            school = e.school or degree
            row(f"<b>{_esc(school)}</b>", e.location)
            detail = role_line(degree if e.school else "", _esc(f"GPA {e.gpa}") if e.gpa else "")
            if detail or _dates(e.start, e.end):
                row(detail, _dates(e.start, e.end), sub=True)
            bullets(e.details[:2])

    def emit(section: str, items: list[_Item], first, second) -> None:
        if not items:
            return
        heading(section)
        for n, item in enumerate(items):
            if n:
                gap()
            x = item.source
            left, right = first(x)
            row(left, right)
            left, right = second(x)
            if left or right:
                row(left, right, sub=True)
            bullets(item.bullets)

    # Experience reads like education: employer and place, then role and dates.
    emit("Experience", plan.experience,
         lambda x: (f"<b>{_esc(x.company or x.title)}</b>", x.location),
         lambda x: (role_line(x.title if x.company else "", tech(x.tech)), _dates(x.start, x.end)))
    emit("Projects", plan.projects,
         lambda x: (_join(f"<b>{_esc(x.name)}</b>", _esc(x.role)), _dates(x.start, x.end)),
         lambda x: (_join(tech(x.tech), _esc(_short(x.link))), ""))
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
    story = _story(plan)
    if fit:
        story = [KeepInFrame(doc.width, doc.height, story, mode="shrink")]
    doc.build(story)
    return int(doc.page)


def plan_resume(profile: Profile, job: Job, settings: dict | None = None,
                selection: dict | None = None) -> ResumePlan:
    """Rank the resume library for ``job`` and trim it to one page.

    Generated resumes are always one page: the least relevant material goes first, then
    optional extras (awards, coursework, a long summary). ``build_resume`` scales any
    remainder onto the page, so nothing is cut mid-sentence."""
    from io import BytesIO
    plan = ResumePlan(profile, job, settings, selection)
    for _ in range(400):
        if _render(plan, BytesIO()) <= 1:
            break
        if not plan.shrink() and not plan.compact():
            break
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
    ("software", "Software engineering"),
    ("data", "Data science & analytics"),
    ("ml", "Machine learning & AI"),
    ("quant", "Fintech & quant"),
    ("product", "Product & program"),
    ("general", "General"),
)
_CATEGORY_TERMS = {
    "software": (r"software|developer|backend|back-end|frontend|front-end|full[- ]?stack|platform|infrastructure|"
                 r"\bsre\b|reliability|mobile|ios|android|systems engineer|devops|security engineer|embedded", 3.0),
    "data": (r"data scien|data analy|analytics|analyst|statistic|experimentation|a/b|business intelligence|"
             r"\bbi\b|data engineer|insights", 3.0),
    "ml": (r"machine learning|\bml\b|\bai\b|artificial intelligence|deep learning|\bllm|nlp|computer vision|"
           r"research (?:scientist|engineer)|applied scientist|perception", 3.2),
    "quant": (r"quant|trading|trader|fintech|payments|finance|financial|risk|hedge|market maker|market making|"
              r"capital markets|banking|crypto|portfolio|securities|derivatives", 3.1),
    "product": (r"product manager|\bpm\b|program manager|product owner|technical program", 3.4),
}


def classify_role(job: Job) -> str:
    """The resume role type that best fits a posting; ``general`` when unclear."""
    title = (job.title or "").lower()
    body = (job.description or "")[:6000].lower()
    best, best_score = "general", 0.0
    for key, (pattern, weight) in _CATEGORY_TERMS.items():
        score = weight * len(re.findall(pattern, title)) + 0.25 * min(8, len(re.findall(pattern, body)))
        if score > best_score:
            best, best_score = key, score
    return best if best_score >= 1.0 else "general"


def category_label(key: str) -> str:
    return dict(RESUME_CATEGORIES).get(key, key.replace("_", " ").title())


def choose_resume_file(job: Job, files: list[dict], fallback: str = "") -> tuple[str, str]:
    """Pick the uploaded resume whose role type matches ``job``; else the default, else ``fallback``."""
    usable = [f for f in files if Path(str(f.get("path") or "")).is_file()]
    category = classify_role(job)
    for record in usable:
        if record.get("category") == category:
            return str(record["path"]), f"{record.get('label') or 'Resume'} · matched {category_label(category).lower()}"
    default = next((f for f in usable if f.get("is_default")), usable[0] if usable else None)
    if default:
        return str(default["path"]), f"{default.get('label') or 'Resume'} · default resume"
    return fallback, "Main resume"


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
    if strategy == "library":
        path, note = choose_resume_file(job, db.list_resume_files(), profile.resume_path)
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
    style = ParagraphStyle("letter", fontName="Helvetica", fontSize=11, leading=15, alignment=TA_LEFT, spaceAfter=10)
    doc = SimpleDocTemplate(str(path), pagesize=letter, leftMargin=inch, rightMargin=inch,
                            topMargin=inch, bottomMargin=inch, title=f"Cover letter: {job.company}")
    doc.build([Paragraph(_esc(part).replace("\n", "<br/>"), style) for part in text.split("\n\n") if part.strip()])
    config.secure_file(path)
    return str(path)
