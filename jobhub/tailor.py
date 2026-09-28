"""Per-job resume drafts: prioritize saved profile facts and render a PDF.

Truthfulness guard: summary wording and factual bullets come from the saved profile. Job text is
used only to rank that material, never as a source of candidate claims.
Falls back to the original resume PDF whenever the structured profile is too thin to render well.
"""
from __future__ import annotations

import re
from pathlib import Path

from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Table, TableStyle

from . import config, db
from .models import Job, Profile


STOP = {"and", "the", "with", "for", "code", "data", "team", "work", "using", "our", "you", "will", "that", "this",
        "from", "have", "are", "your", "all", "can", "web", "new", "build", "tools", "suite", "studio", "engineer",
        "engineering", "software", "systems", "system", "experience"}


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9+#.]{2,}", (s or "").lower()) if t not in STOP}


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


def build_resume(profile: Profile, job: Job, out_path: Path, use_llm: bool = False) -> Path:
    jd = _tokens(job.title + " " + (job.description or ""))
    styles = {
        "name": ParagraphStyle("name", fontName="Helvetica-Bold", fontSize=17, leading=20, spaceAfter=1),
        "contact": ParagraphStyle("contact", fontName="Helvetica", fontSize=9, leading=11, textColor="#333333"),
        "head": ParagraphStyle("head", fontName="Helvetica-Oblique", fontSize=9.5, leading=12, spaceBefore=2),
        "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=10.5, leading=13, spaceBefore=7, spaceAfter=1),
        "item": ParagraphStyle("item", fontName="Helvetica-Bold", fontSize=9.6, leading=12),
        "sub": ParagraphStyle("sub", fontName="Helvetica-Oblique", fontSize=9, leading=11, textColor="#333333"),
        "date": ParagraphStyle("date", fontName="Helvetica", fontSize=9, leading=12, alignment=2, textColor="#333333"),
        "bullet": ParagraphStyle("bullet", fontName="Helvetica", fontSize=9.3, leading=11.6, leftIndent=12,
                                 bulletIndent=2, alignment=TA_LEFT),
        "body": ParagraphStyle("body", fontName="Helvetica", fontSize=9.3, leading=11.6),
    }
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(out_path), pagesize=letter, leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                            topMargin=0.5 * inch, bottomMargin=0.5 * inch, title=f"{profile.full_name} Resume",
                            author=profile.full_name)
    S: list = []
    S.append(Paragraph(_esc(profile.full_name), styles["name"]))
    contact = " | ".join(x for x in [profile.email, profile.phone and _fmt_phone(profile.phone), profile.location,
                                     _short(profile.website), _short(profile.linkedin), _short(profile.github)] if x)
    S.append(Paragraph(_esc(contact), styles["contact"]))
    S.append(Paragraph(_esc(tailored_headline(profile, job)), styles["head"]))
    S.append(HRFlowable(width="100%", thickness=0.7, color="#222222", spaceBefore=4, spaceAfter=2))

    summary = tailored_summary(profile, job)
    if summary:
        S.append(Paragraph("SUMMARY", styles["h2"]))
        S.append(Paragraph(_esc(summary), styles["body"]))

    def row(left: str, right: str, style_l="item"):
        t = Table([[Paragraph(_esc(left), styles[style_l]), Paragraph(_esc(right), styles["date"])]],
                  colWidths=[5.2 * inch, 2.1 * inch])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        S.append(t)

    # Education
    if profile.education:
        S.append(Paragraph("EDUCATION", styles["h2"]))
        for e in profile.education[:3]:
            row(f"{e.degree}{(' in ' + e.field) if e.field and e.field.lower() not in e.degree.lower() else ''}",
                " - ".join(x for x in (e.start, e.end) if x))
            sub = e.school + (f"  |  GPA {e.gpa}" if e.gpa else "")
            S.append(Paragraph(_esc(sub), styles["sub"]))

    # Skills, matched first
    if profile.skills:
        S.append(Paragraph("SKILLS", styles["h2"]))
        matched = [s for s in profile.skills if _tokens(s) & jd]
        rest = [s for s in profile.skills if s not in matched]
        S.append(Paragraph(_esc(", ".join((matched + rest)[:45])), styles["body"]))

    # Tailoring ranks and selects source wording. It never rewrites achievements.
    def select(items: list, max_items: int, max_bul: int) -> list[tuple[object, list[str]]]:
        ranked = sorted(items, key=lambda x: -_relevance(" ".join(x.bullets), jd))[:max_items]
        out = []
        for x in ranked:
            top = sorted(x.bullets, key=lambda b: -_relevance(b, jd))[:max_bul]
            out.append((x, [b for b in x.bullets if b in top]))  # keep original order
        return out

    exp_sel = select(profile.experience, 4, 3)
    proj_sel = select(profile.projects, 4, 3)
    all_bullets = [b for _, bl in exp_sel + proj_sel for b in bl]
    polished = polish_bullets(all_bullets, job, profile) if use_llm else all_bullets
    it = iter(polished)
    exp_sel = [(x, [next(it) for _ in bl]) for x, bl in exp_sel]
    proj_sel = [(x, [next(it) for _ in bl]) for x, bl in proj_sel]

    def emit(section: str, sel, name_of, sub_of, dates_of):
        if not sel:
            return
        S.append(Paragraph(section, styles["h2"]))
        for x, bl in sel:
            row(name_of(x), dates_of(x))
            if sub_of(x):
                S.append(Paragraph(_esc(sub_of(x)), styles["sub"]))
            for b in bl:
                S.append(Paragraph(_esc(b), styles["bullet"], bulletText="•"))

    emit("EXPERIENCE", exp_sel, lambda x: f"{x.title} - {x.company}" if x.company else x.title,
         lambda x: x.location, lambda x: " - ".join(v for v in (x.start, x.end) if v))
    emit("PROJECTS", proj_sel, lambda x: x.name,
         lambda x: "  |  ".join(v for v in (x.role, ", ".join(x.tech[:8])) if v),
         lambda x: " - ".join(v for v in (x.start, x.end) if v))
    doc.build(S)
    config.secure_file(out_path)
    return out_path


def _fmt_phone(p: str) -> str:
    d = re.sub(r"\D", "", p)
    if len(d) == 11 and d.startswith("1"): d = d[1:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else p


def _short(u: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", u or "").rstrip("/")


def resume_for_job(profile: Profile, job: Job, settings: dict) -> str:
    """Return the resume path to upload for this job (tailored PDF or the original)."""
    strategy = settings.get("resume_strategy", "tailored")
    rich = len(profile.projects) + len(profile.experience) >= 2 and profile.education
    if strategy == "tailored" and rich:
        out = config.TAILORED / f"{_safe(profile.last_name or 'resume')}_{_safe(job.company)}_{job.id}.pdf"
        try:
            build_resume(profile, job, out, use_llm=False)
            db.add_event(f"Tailored resume built for {job.company} / {job.title}", "info", job.id)
            return str(out)
        except Exception as e:  # noqa: BLE001
            db.add_event(f"Tailoring failed, using original resume: {e}", "warn", job.id)
    return profile.resume_path


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", s)[:24] or "x"
