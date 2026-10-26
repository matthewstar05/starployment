# JARVIS

Settings → JARVIS has three modes. Save settings to activate a mode.

- **OFF** uses your ordinary automatic-application preferences.
- **NEW** considers suitable postings published in the last 24 hours, newest first, and sends using saved answers. If a posting has no publication date, its discovery date is used. Required unknown answers stop the application.
- **ALL** balances newest postings and highest match scores across new jobs and unfinished prepared drafts, of any age, as long as a scan found the posting still listed in the last three days (old but still hiring). It prepares applications, runs AI Autofill, authorizes the resulting answers, and sends without a separate review. Turning on ALL is standing permission to send those AI answers, including labeled assumptions. They stay with that application and never enter your saved answer bank.

Both modes, like ordinary automatic applications, skip an employer once it has `company_cap.count` attempts in `company_cap.days` (default 3 in 30) and alternate role families (software, data, quant, finance, accounting, business, risk, economics) so one kind of role or one large employer cannot take every overnight send. Change the mode from the JARVIS badge in the sidebar or in Settings.

Both modes respect the minimum match score, eligibility checks, priority companies, and explicit stars. JARVIS excludes Workday and unsupported forms. It never retries a job with an existing application attempt, never solves security challenges, and records success only after employer confirmation. Incomplete applications remain available for review. Practice mode still prevents submission.

While active, JARVIS halves saved send, preparation, and per-application agent concurrency (rounded down, minimum one), halves the global agent-process ceiling, and halves the scan interval (minimum five minutes). Source and board concurrency are also reduced. Your saved values are preserved for OFF. Scans and new sends defer when the resource governor reports high load or low available memory, for at most ten minutes in a row (`jarvis.MAX_DEFER_SECONDS`); after that they go ahead at the halved concurrency, so a Mac that stays loaded slows JARVIS down but never stops it. Running browser actions finish normally; resource limits reduce contention but cannot guarantee zero impact from the browser or other apps.

JARVIS uses background browsers and keeps the Mac from idle sleeping while Starployment is open. The display may sleep. Closing the app, shutting down, or closing the laptop lid can stop overnight work. The existing applications-per-run cap and one-employer-per-cycle limit remain in force. The activity log records JARVIS authorizations and confirmed outcomes.
