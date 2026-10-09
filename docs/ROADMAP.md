# Starployment product map

Starployment's first useful loop is local setup, discovery, ranking, truthful resume tailoring, supported form filling, and a durable application history. The native app should make the next action obvious: review a missing fact, inspect a matched role, or see an employer-confirmed submission. A score is a ranking aid, not a probability of getting hired.

## What the prototype already does

| Area | Current state |
| --- | --- |
| Desktop | Native PySide6 workspace and setup wizard in an Apple-style glass design: floating sidebar with native macOS window controls, animated navigation, segmented filters, switches, toasts, keyboard shortcuts, and instant light/dark switching that honors Reduce Motion. |
| Setup | Resume PDF import, parsed-fact review, target titles and locations, salary preferences, candidate-owned answer bank. |
| Discovery | Scheduled searches from about 200 bundled public ATS boards plus boards discovered from other listings (rotated per scan), Simplify, RemoteOK, and JobSpy; bounded rotation across saved JobSpy target cities; direct supported posting import. Jobs show when each role was posted, filter to the last 24 hours, 3 days, week, or month, and sort by any column. |
| Matching | Explainable job score using title, skills, experience, location, compensation, and recency. |
| Applications | Role-specific PDF with extractive summary from saved facts, ordinary Greenhouse/Lever/Ashby form filling, duplicate control, and employer-confirmation checks. |
| History | SQLite run and application records with screenshots, unanswered questions, source diagnostics, and private notes editable from a selected application. |
| Apply flow | Overview → Apply prepares the top matches for review, optionally only roles posted in the last 24 hours or week and newest first: form questions read ahead from Greenhouse, Lever, and Ashby, answers from saved facts, Luna drafts for open questions, a resume per role. Approve and send one at a time or in a batch; reset what was never sent. Priority (FAANG+) companies and starred applications are prepared but only sent one by one. Applications sort, filter by star, and can be marked as sent when applied elsewhere. |
| Resume library | Unlimited roles, projects, schools, skills, and awards; a one-page resume generated per role by ranking and trimming the candidate's own wording; resume files by role type. |
| Luna | Optional drafting assistant on the candidate's Claude or ChatGPT plan (or an API key), run tool-free; every draft needs approval; sensitive questions never reach her. |
| Tracker | Sortable spreadsheet of sent applications with stages, salary and location stats, notes, manual entries, CSV export, and read-only inbox tracking of assessments, interviews, offers, and rejections. |
| Packaging | A tested macOS arm64 app bundle, portable source archive, and native build recipes for macOS, Windows, and Linux. |

## Next milestones, in priority order

1. **Make submission readiness visible before a browser attempt.** Show why a job is ready, skipped, or likely to need an answer. Extend required-question preflight where a source exposes reliable public metadata, then validate on the live form. Use this to spend the hourly attempt limit on forms with a realistic chance of completion.
2. **Finish email verification without losing certainty.** Automatic email verification is implemented for both one-time codes (including segmented and leading-zero codes) and verification links across supported ATS flows (Greenhouse, Ashby, Lever). Message matching verifies candidate recipient, timing freshness, IMAP UIDVALIDITY integrity, and employer context. Verification links enforce strict destination host validation and token redaction, maintaining browser session and cookie continuity. Applications are recorded as applied only after true employer confirmation.
3. **Improve setup from encountered questions.** Group repeat question wording, explain which saved fact will answer each variant, and show the exact value for review. Add clear controls for role-specific answers and legal acknowledgements that should never be inferred from a resume.
4. **Build and smoke-test native releases on each platform.** Run the supplied workflow on macOS Intel, Windows x64, and Linux x64. Test first-run setup, file selection, Chromium, credential storage, scheduling while awake, and upgrade migration on each OS before distributing a binary.
5. **Make the desktop feel more at home on each OS.** macOS now has native window chrome, a menu bar, keyboard shortcuts, clear empty states, contrast checks, and motion that honors Reduce Motion. Next: true behind-window vibrancy on macOS, Windows 11 Mica and snap layouts, VoiceOver and Narrator passes over the custom-painted controls, and a compact table density option.

## Expansion ideas

- **Broader discovery:** user-managed company lists, more employer ATS adapters, saved search presets, stale-posting detection, and source reliability summaries. LinkedIn and Indeed listings can lead to employer sites without claiming universal scrape coverage.
- **More form adapters:** Workday, SmartRecruiters, iCIMS, and site-specific forms, each with fixtures, live practice checks, and confirmation rules. Login walls and challenge flows need their own explicit support path.
- **Application strategy:** configurable company frequency limits, salary floors, remote and office attendance preferences, saved-job queues, and a reasoned comparison of career growth, compensation, location, and role fit. Never label a heuristic as an admission probability.
- **Candidate materials:** the experience library and role-specific resume files now exist. Next: cover letters grounded in the library and a review diff showing every selected claim.
- **Follow-up workspace:** stages, notes, CSV export, and inbox tracking now exist. Next: contacts, deadlines, reminders, and Gmail OAuth as an alternative to app passwords. No outbound message should be sent without an explicit user action or an authorized rule.
- **Distribution:** signed and notarized macOS releases, Windows signing, Linux packages, automatic updates, and a first-run guide. User profiles, browser state, credentials, resumes, and screenshots stay outside release artifacts.

The unattended path depends on accurate candidate-owned facts and supported employer forms. Unknown eligibility, personal narratives, office attendance, and legal consent remain unanswered until the candidate supplies them. The app should report exactly what it attempted and what the employer confirmed.
