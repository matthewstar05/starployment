# JobHub product map

JobHub's first useful loop is local setup, discovery, ranking, truthful resume tailoring, supported form filling, and a durable application history. The native app should make the next action obvious: review a missing fact, inspect a matched role, or see an employer-confirmed submission. A score is a ranking aid, not a probability of getting hired.

## What the prototype already does

| Area | Current state |
| --- | --- |
| Desktop | Native PySide6 workspace and setup wizard in an Apple-style glass design: floating sidebar with native macOS window controls, animated navigation, segmented filters, switches, toasts, keyboard shortcuts, and instant light/dark switching that honors Reduce Motion. |
| Setup | Resume PDF import, parsed-fact review, target titles and locations, salary preferences, candidate-owned answer bank. |
| Discovery | Scheduled searches from configured public ATS feeds, Simplify, RemoteOK, and JobSpy; bounded rotation across saved JobSpy target cities; direct supported posting import. |
| Matching | Explainable job score using title, skills, experience, location, compensation, and recency. |
| Applications | Role-specific PDF with extractive summary from saved facts, ordinary Greenhouse/Lever/Ashby form filling, duplicate control, and employer-confirmation checks. |
| History | SQLite run and application records with screenshots, unanswered questions, source diagnostics, and private notes editable from a selected application. |
| Packaging | A tested macOS arm64 app bundle, portable source archive, and native build recipes for macOS, Windows, and Linux. |

## Next milestones, in priority order

1. **Make submission readiness visible before a browser attempt.** Show why a job is ready, skipped, or likely to need an answer. Extend required-question preflight where a source exposes reliable public metadata, then validate on the live form. Use this to spend the hourly attempt limit on forms with a realistic chance of completion.
2. **Finish email verification without losing certainty.** Test the opt-in Greenhouse IMAP flow against a consenting live mailbox and employer form. Keep a durable pre-click marker, accept only one fresh code, and require the employer's confirmation page before calling it applied. Other verification methods should remain review cases until independently supported.
3. **Improve setup from encountered questions.** Group repeat question wording, explain which saved fact will answer each variant, and show the exact value for review. Add clear controls for role-specific answers and legal acknowledgements that should never be inferred from a resume.
4. **Build and smoke-test native releases on each platform.** Run the supplied workflow on macOS Intel, Windows x64, and Linux x64. Test first-run setup, file selection, Chromium, credential storage, scheduling while awake, and upgrade migration on each OS before distributing a binary.
5. **Make the desktop feel more at home on each OS.** macOS now has native window chrome, a menu bar, keyboard shortcuts, clear empty states, contrast checks, and motion that honors Reduce Motion. Next: true behind-window vibrancy on macOS, Windows 11 Mica and snap layouts, VoiceOver and Narrator passes over the custom-painted controls, and a compact table density option.

## Expansion ideas

- **Broader discovery:** user-managed company lists, more employer ATS adapters, saved search presets, stale-posting detection, and source reliability summaries. LinkedIn and Indeed listings can lead to employer sites without claiming universal scrape coverage.
- **More form adapters:** Workday, SmartRecruiters, iCIMS, and site-specific forms, each with fixtures, live practice checks, and confirmation rules. Login walls and challenge flows need their own explicit support path.
- **Application strategy:** configurable company frequency limits, salary floors, remote and office attendance preferences, saved-job queues, and a reasoned comparison of career growth, compensation, location, and role fit. Never label a heuristic as an admission probability.
- **Candidate materials:** multiple verified resume variants, a source-of-truth experience library, optional cover letters grounded in that library, and a review diff showing every selected claim.
- **Follow-up workspace:** interview stages, contact notes, deadlines, exportable application history, and reminders. No outbound message should be sent without an explicit user action or an authorized rule.
- **Distribution:** signed and notarized macOS releases, Windows signing, Linux packages, automatic updates, and a first-run guide. User profiles, browser state, credentials, resumes, and screenshots stay outside release artifacts.

The unattended path depends on accurate candidate-owned facts and supported employer forms. Unknown eligibility, personal narratives, office attendance, and legal consent remain unanswered until the candidate supplies them. The app should report exactly what it attempted and what the employer confirmed.
