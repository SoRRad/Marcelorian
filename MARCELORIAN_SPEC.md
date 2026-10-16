# Marcelorian — Build Specification (v2)
*May the Force Hire You*

You are building **Marcelorian**, a Windows desktop application that helps one applicant (an MD) find US ophthalmology principal investigators (PIs), assess fit, draft personalized emails, and manage a 4-email outreach sequence through the applicant's own Gmail.

Every email is approved individually by the applicant. AI researches and drafts. Ordinary, tested code controls approval, sending, follow-ups, limits, and stopping. The AI never has authority over recipients, approvals, settings, or sending.

This is a **pilot for one applicant**, monitored by the project owner. Build for that. Keep the code clean enough to add specialties and users later, but build no multi-user features now.

The application promises **duplicate-execution protection and conservative handling of uncertainty**. It does **not** promise exactly-once delivery, perfect reply interception, or that every reply is detected instantly.

---

## 0. How to work

- Build in the phases in §18. **Stop at the end of each phase.** Summarize what was built, list what is unverified, and wait for my go-ahead.
- Before writing code in P0, list any ambiguities or contradictions you find in this spec and ask me. Never silently resolve anything affecting sending, caps, data sharing, or AI inputs.
- Write tests with the code. Scheduling, caps, state transitions, and the send gateway must be unit-tested against a fake Gmail transport and an injected clock **before** any real Gmail send.
- Recheck official provider documentation (Google, OpenAlex, NCBI, NIH RePORTER, Anthropic) when implementing an integration. Record the date checked in code comments or docs. Mocked tests do not prove a live integration.
- Never commit secrets or real applicant data. Never contact a real PI during development.
- Prefer boring, well-maintained, pinned libraries. Do not add agent frameworks, a cloud queue, a service account, an admin dashboard, or new paid services.

---

## 1. Locked decisions (do not reopen)

| Area | Decision |
|---|---|
| Name / slogan | **Marcelorian** / **May the Force Hire You**. Text only. No franchise logos, characters, fonts, or imagery in the app or the emails. |
| Users | One applicant on a Windows laptop. The applicant enters their own profile; nothing applicant-specific is hardcoded. No hosted or multi-user platform in v1. |
| Roles | Owner: owns the Google Cloud project and a dedicated, spend-limited Anthropic pilot credential, and monitors a consented report. Applicant: authorizes their own Gmail and approves every message. |
| Specialty | Ophthalmology, all subspecialties, via a config file. New specialty = new config file. |
| Geography | US institutions only. |
| Subject | Email 1: `Research Position Application – Ophthalmology` (specialty display name from config). Emails 2–4 reply in the same thread with one normalized `Re:` prefix. No personalized subjects. |
| Sequence | 4 emails total. Email 2 is due 7×24 h after Email 1 **actually sent**. Email 3 is due 7×24 h after Email 2 actually sent. Email 4 is due 1 calendar month after Email 3 actually sent. A setting allows N days for the last interval. |
| Approval | Every message, including each follow-up, is approved individually. Approval authorizes one immutable snapshot (§9.1). |
| Sending | A tray worker starts at Windows login (with explicit setup consent). Approved messages are queued and dispatched in the next permitted window. If approved inside a window, they dispatch promptly once all checks and pacing allow. |
| Window | Tue–Thu, 08:00 (inclusive) to 11:00 (exclusive), in the PI's **verified** IANA time zone. |
| Caps | 2 pending PIs per institution; 1 per lab; 3 newly contacted PIs per institution per rolling 30 days; 25 app-originated real Gmail submissions per rolling 24 hours. All adjustable. |
| Slot release | A genuine reply or a successfully sent Email 4 frees the pending slot. Auto-replies keep the slot. Confirmed bounces stop the sequence. Manual closures are auditable. |
| Funding | Paid first. Unpaid stays eligible, with a flag. Unknown proceeds normally and is marked. All messages still need applicant approval. The unpaid flag is informational for the owner, not an extra approval step. |
| Records | Local SQLite is authoritative. Two CSVs are exports. A consented, applicant-owned Google Sheet is a reporting copy for the owner and never a source of instructions. |
| AI inputs | Gmail content (bodies, snippets, attachments, thread history) is **never** AI input. |
| v2 (out of scope) | Job-page monitoring, learning from edits, cross-applicant coordination. |

---

## 2. Tech stack and local security

### Stack
- **Python 3.12**; **FastAPI** + **Jinja2** server-rendered pages + **HTMX**. No SPA build step.
- Clean dark theme with a subtle sci-fi accent color. Original generic icon.
- **SQLite** (WAL) via **SQLAlchemy 2.x**; migrations with **Alembic**. SQLite is the **durable queue**; scheduler memory is never authoritative.
- **APScheduler** (in-process) plus a dispatcher that can be woken immediately on approval. Polling every 5 minutes is the fallback.
- **pystray** for the tray icon. **win10toast**/**windows-toasts** (or similar maintained library) for notifications.
- **google-auth-oauthlib** (InstalledAppFlow, Desktop client, loopback redirect) and **google-api-python-client**. **Disable automatic retries for `messages.send`** in the client and in any wrapper.
- **anthropic** SDK. The model is configurable, default `claude-sonnet-5-5`; validate access with the pilot credential at setup.
- **keyring** with Windows Credential Manager for all secrets and tokens. **No plaintext fallback**: if keyring is unavailable, setup fails clearly.
- **pdfplumber**/**pypdf** for local CV text extraction. **httpx** for public APIs.
- **tzdata** bundled (Windows may lack the IANA database).
- **PyInstaller** (onedir) + **Inno Setup**. **pytest**. **GitHub Actions** on `windows-latest` runs tests and builds the installer artifact.

### Paths
- App data: `%LOCALAPPDATA%\Marcelorian\` (DB, logs, backups, CV versions).
- Exports: the Windows Documents known folder (resolved via the Windows API, not a hardcoded path) `\Marcelorian\exports\`. Warn if this folder is synced by OneDrive.

### Local UI security
- Bind to `127.0.0.1` on a free port. Loopback alone is **not** the access control.
- The tray opens the browser with a one-time bootstrap token URL, which is exchanged for an HttpOnly, SameSite=Strict session cookie. The token is then invalidated.
- Enforce Host and Origin checks and a CSRF token on all POSTs. No state-changing GET requests. Escape all untrusted text.
- Never log bootstrap tokens, OAuth codes/tokens, API keys, email bodies, raw CV text, or full email addresses (mask at INFO level).
- A single-instance lock ensures exactly one tray worker and one dispatcher per data directory.

### Outbound fetching (SSRF guard)
- HTTP(S) only. Resolve the hostname and reject private, loopback, link-local, and multicast IPs. Re-check after every redirect, with a max of 5 redirects.
- Response size limit (e.g., 3 MB), timeouts, and per-host rate limits. Respect robots.txt.

---

## 3. Data model and state machines

### Core tables (names can be adapted; the distinctions cannot)
`applicant_profile`, `profile_fact` (with confirmation status and version), `cv_version` (file, SHA-256, uploaded_at), `institution` (ROR ID, name, city, state, IANA tz, tz_verified, group_id), `lab_group` (verified flag), `pi` (OpenAlex ID, ORCID, name, salutation, salutation_verified, institution, lab_group, current_role_verified + evidence), `pi_email` (address, confidence A/B/C, source, ownership_verified, verified_by, verified_at, invalid flag), `evidence` (pi_id, type, URL, title, excerpt, retrieved_at), `grant_evidence`, `opportunity` (posted jobs), `assessment`, `campaign`, `message`, `approval_snapshot`, `send_attempt`, `inbound_event`, `reservation`, `budget_ledger`, `task` (referrals, contact-later), `audit_event`, `worker_heartbeat`, `sync_state`, `settings`, `consent`.

Every row that relates to sending carries a **`mode`** (`sandbox`, `transport_test`, `live`). Modes never share campaigns, approvals, threads, budgets, exports, or reports, except for the real-submission budget (§11).

### Campaign lifecycle (one per applicant × PI × mode)

| State | Meaning | Holds pending slot? |
|---|---|---|
| `candidate` | No outreach yet | No |
| `outreach_pending` | Email 1 approved (slot reserved) or any email sent and awaiting reply | **Yes** |
| `paused_review` | Reason: auto_reply, delivery_notice, manual_applicant_message, unknown_participant, ambiguous_thread, send_uncertain, sync_gap, prior_contact_found, missed_window | **Yes** |
| `conversation` | Genuine reply received. Sequence stopped. Still visible. | No |
| `sequence_complete` | Email 4 sent. Reply monitoring continues; a later reply moves it to `conversation`. | No |
| `closed` | Outcome: bounce_confirmed, no_opening, do_not_contact, applicant_closed (reason required), cancelled_before_first_send | No |

**Invariants**
- A campaign whose follow-up is in any message state keeps its slot. Drafting never releases a slot.
- Only these release a slot: a genuine reply, Email 4 sent, or a `closed` outcome. Cancelling an unsent Email 1 releases its reservation. Revoking a follow-up's approval does not.
- `sequence_complete` stops sending, not monitoring.

### Message lifecycle (one logical message per `(campaign_id, step, mode)`, unique)
`draft` → `awaiting_approval` → `approved_queued` → `sending` → `sent`

Side states:
- `failed_definite`: Gmail definitively rejected the message before acceptance. It may be re-approved.
- `uncertain`: the outcome is unknown. Never auto-retried.
- `cancelled`
- `needs_reapproval`: snapshot invalidated or assigned window missed.

Revisions create new `approval_snapshot` versions, not new logical messages. Every transition writes an `audit_event` (timestamp, actor, reason).

### Identity and duplicate protection
- Re-discovery, PI merges, or a second campaign must never bypass do-not-contact or duplicate-outreach checks.
- Merging PIs preserves all contact and sending history.
- Homonymous authors are never auto-merged; merges need the applicant's confirmation.

---

## 4. Applicant profile and AI input boundaries

### Profile (wizard + editable)
- **Identity and training:** full name, preferred name for signature, degree(s), medical school, graduation year, `current_role_phrase` (e.g., "a research fellow at X"), US training status (USMLE steps, ECFMG, any US residency), visa status/need (category + free text, shown factually, never interpreted), earliest start date, desired duration, preferred US states (optional), signature block.
- **Preferences:** funding requirement (`paid only` / `paid preferred, unpaid acceptable` — pilot default is the latter), subspecialty interests (default: all), research interests, methods/skills offered, tone preference (e.g., formal / warm-formal / concise).
- **CV:** PDF versions with SHA-256.
- **Publications:** imported from PubMed by name and/or ORCID; the applicant confirms each.
- **Other:** "recent news" field (used only in Email 3); do-not-contact list.

### Facts
Each `profile_fact` is `proposed` or `confirmed`, with a profile version. **Only confirmed facts can appear in drafts.**

### AI input rules
1. **CV extraction (optional, separately consented).** Extract text locally. The applicant reviews it and can redact before any Claude request. Only the selected text is sent. Results are `proposed` facts. Manual fact entry without AI must always be available. The wizard validates the Claude credential **before** this step.
2. **Assessment and drafting** receive only confirmed applicant facts and permitted public research evidence (abstracts, titles, public page excerpts).
3. **Never AI input:** Gmail bodies, snippets, attachments, quoted text, thread history, inbox-derived summaries, writing samples of emails. This also applies to debug telemetry.
4. Public web text and abstracts are **untrusted data**. Wrap them in delimited blocks and instruct the model to ignore any instructions inside. Prompt delimiters are not the security boundary: the model has no tools, no sending ability, and no access to settings or recipients.

---

## 5. Specialty configuration

`specialties/ophthalmology.yaml`:
- Display name (used in the subject).
- MeSH terms and keyword queries per subspecialty: retina; glaucoma; cornea/external disease; oculoplastics; neuro-ophthalmology; pediatric ophthalmology/strabismus; uveitis/ocular immunology; ocular oncology; cataract/refractive; ophthalmic imaging and AI.
- NIH institute filter: NEI.
- A maintained seed list of US academic ophthalmology departments: name, ROR ID, official department/faculty-directory URL(s).
- Title vocabulary for manual job entry and search: research fellow, clinical research fellow, postdoctoral research fellow, research trainee, research scholar, visiting research scholar, research associate, pre-residency research fellowship.

---

## 6. Discovery

Discovery is applicant-triggered, runs in the background with progress, is resumable, and never delays Gmail safety work (§9.5).

### Sources
1. **PubMed E-utilities.** Last 3 years; specialty queries × applicant interests/methods. Parse the author → affiliation linkage. Optional NCBI API key; respect rate limits.
2. **OpenAlex.** Configure an **API key** per current OpenAlex documentation (the `mailto` polite pool is deprecated; verify the current auth and usage limits at build time). Handle pagination, quota exhaustion, and missing or inactive keys without losing other sources' results.
3. **NIH RePORTER API v2.** Active NEI projects: PI, organization, project number, title, end date.
4. **Faculty/lab pages.** Start from the config seed URLs and applicant-pasted URLs. Bounded same-site navigation (e.g., depth ≤ 2, ≤ 30 pages per site). Never invent URLs and never use a paid search service. When a page cannot be fetched, the applicant can verify manually.
5. **ClinicalTrials.gov API v2:** optional, best-effort enrichment. It must never block or delay sources 1–4.
6. **Posted opportunities (v1 = manual entry):** URL, title, institution, PI if named, compensation (explicitly paid / explicitly unpaid / not stated), requirements, deadline, and the formal application route. That route is tracked separately; an email never replaces a required formal application.

### Identity and affiliation
- Deduplicate by OpenAlex ID / ORCID, then name + institution, with applicant confirmation for uncertain merges.
- Publication and OpenAlex affiliations are **evidence of affiliation, not proof of current employment.** Mark `current_role_verified` only from a current institutional page. Show uncertainty otherwise.
- Being an author does not establish that someone can supervise or hire. Show the evidence of their faculty/PI role.
- Institutions are normalized to ROR. An `institution_group` setting lets institutions be grouped or split for caps.
- **Lab groups:** each PI is their own group by default. Same-institution PIs with ≥ 3 shared papers in 3 years get an **unverified suggestion** of a shared lab. Before a second same-institution contact where an unverified suggestion exists, the applicant must review (merge or dismiss).

### Email confidence (source quality) + ownership verification (separate fields)
- **A:** published on the PI's current institutional/lab page.
- **B:** PubMed affiliation email ≤ 2 years old, **explicitly attributable to this investigator** (not just any author or affiliation on the paper), with an affiliation matching the current institution.
- **C:** anything else.
- Send-ready requires A or B with ownership attributable. Upgrading C requires recording **new verification evidence** (source + who + when); a checkbox alone is not enough.
- **Never generate guessed addresses.**

### Time zone
Derive the IANA zone from the institution city/state. If it is ambiguous (multi-zone states, missing city), **scheduling is blocked** until the applicant verifies it. The applicant's zone is never a fallback.

---

## 7. Assessment and funding

Shown as separate dimensions, never one score:
- **Research fit:** Strong / Moderate / Weak. LLM, with evidence citations (validated as in §8.3).
- **Eligibility:** Meets / Unclear / Likely not. Rule-based against known requirements (e.g., a posting requiring a US ACGME residency vs. the applicant's training). Never makes visa or immigration determinations; shows the requirements and uncertainty factually.
- **Opportunity:** `Posted opening` / `No opening verified`.
- **Contact:** confidence A/B/C + ownership verified Y/N + current role verified Y/N.
- **Appointment funding:** `Paid – explicitly documented` / `Unpaid – explicitly documented` / `Unknown`. A posting proves an opening, not compensation; compensation must be explicitly stated. Do not assume an observership is an unpaid research appointment.
- **Research funding evidence (separate field):** `Active grant evidence` with source and dates, or none. It can raise priority but never clears the funding-unknown flag or implies salary availability.

Honor `paid only` by excluding non-paid appointments from drafting (they remain visible, with the reason). Default sort: appointment funding (Paid > Unknown > Unpaid), then grant evidence, fit, and contact quality. One-line summary per candidate, e.g., *"Strong methods match (OCT image analysis). Current role verified. No opening verified. Appointment funding unknown; active NEI grant evidence."*

---

## 8. Drafting

### 8.1 Default templates (editable in Settings)

**Email 1. Subject:** `Research Position Application – {specialty_display_name}`
```
Dear {salutation},

My name is {name}, and I am {current_role_phrase}. I am writing to ask whether your group expects an opening for a research fellow or similar position starting {availability}.

{personalized_sentence}

My background includes {skills_line}. I would be glad to contribute to {contribution_line}.

{funding_line}

I have attached my CV for your reference. Thank you for your time and consideration.

Best regards,
{signature}
```

**Email 2** (reply in thread)
```
Dear {salutation},

I am following up briefly on my note below about research fellow opportunities in your group. Your work on {pi_topic} remains of strong interest to me, and I would welcome any guidance on openings.

Best regards,
{name}
```

**Email 3** (reply in thread)
```
Dear {salutation},

I am writing once more in case my earlier email was missed. {optional_update}

If there is no opening at present, I would be grateful if you could suggest a colleague who may be recruiting.

Best regards,
{name}
```

**Email 4** (reply in thread, with CV attached)
```
Dear {salutation},

I am reaching out a final time about research opportunities in your group. I have attached {cv_phrase} for your reference. If the timing is better later, I would be glad to reconnect then.

Thank you for your consideration.

Best regards,
{name}
```

### 8.2 Field rules
- **LLM-filled only:** `personalized_sentence`, `skills_line`, `contribution_line`, `pi_topic`. The model returns strict JSON with, per field, the evidence IDs and confirmed fact IDs used. The prompt is guided by the applicant's tone preference.
- **Rule-based:**
  - `salutation`: default `Dr. {last_name}`, flagged until verified or edited.
  - `funding_line`: empty by default; never volunteers unpaid willingness unless the applicant enters it.
  - `optional_update`: only from "recent news"; the line is removed if empty.
  - `cv_phrase`: "my updated CV" **only** if the attached version is newer than the one sent in Email 1, otherwise "my CV".
- The model must not claim the applicant read a paper, imply the PI is recruiting without a posted opening, invent qualifications, or add unverified updates.
- The review screen shows each claim next to its supporting excerpt and source link/date. Citations never appear in the outgoing email.

### 8.3 Validation (runs on AI output **and again on the final applicant-edited text**)
- Every cited evidence ID exists, **belongs to this PI**, and is relevant to the field. Every cited fact ID belongs to this applicant, is `confirmed`, and is from the current profile version. Fields match their schema and length limits. Any failure means no draft and an error is shown.
- Blocking checklist:
  - No `{` or `}` remains.
  - Salutation present; PI name and institution correct.
  - Recipient email send-ready.
  - Attachment present whenever the text says "attached".
  - `updated CV` wording only when justified.
  - Subject exact.
  - No do-not-contact match.
  - The applicant's funding preference is respected.
- Structural validation checks provenance, not semantic truth. **Applicant review is the final factual check**, and the UI says so.

---

## 9. Approval, scheduling, and the send gateway

### 9.1 Immutable approval snapshot
Approval binds a snapshot containing: campaign ID, step, mode, connected sender account (primary Gmail identity; no send-as aliases), exactly one `To` (no Cc/Bcc in v1), intended recipient (and actual recipient in transport_test), normalized subject, fully rendered body, attachment file bytes + SHA-256 + version, and thread ID / Message-IDs for follow-ups. The SHA-256 of the canonical snapshot is stored.

- **Frozen:** the latest CV is never substituted, the signature never changes, personalization is never regenerated, and the recipient/sender never change. Any relevant change → `needs_reapproval`.
- A new CV upload offers to re-draft **unsent** messages; it never alters approved ones.
- Each queued approval has a **Cancel / revoke** action.

### 9.2 Buttons and expectations
- Labels: **Approve and queue** (outside the window) or **Approve and send** (inside the window).
- Show the earliest expected dispatch time in **both** PI and applicant time zones. It is conditional on: the laptop awake, the tray running, network, caps, pacing, and a passing final Gmail check.
- The UI explains that closing the browser does not stop the tray, but quitting the tray, sleep, logout, or shutdown prevents dispatch.

### 9.3 Timing
- Store `due_at` (UTC), `eligible_window_start/end` (UTC), the PI's IANA zone, and `sent_at` (UTC) separately.
- Steps 2 and 3: due = previous `sent_at` + 7×24 h.
- Step 4: compute in the PI's IANA zone as previous `sent_at` + 1 calendar month, clamped to the end of the month (Jan 31 → Feb 28/29). Convert to UTC. Handle DST correctly.
- When a step becomes due, its draft is generated and placed in `awaiting_approval`. Only the **next** step is ever generated.
- Show the original due date and the window it will be sent in.
- **Optional blackouts (off by default):** US federal holidays and a Dec 20–Jan 5 range, both configurable. When enabled, they defer the eligible window; history and due dates are never rewritten.

### 9.4 Window, pacing, and recovery
- A message never **starts** sending at or after 11:00 PI local time.
- Within a window, sends are spaced 2–6 minutes apart (random) and limited by caps. Deferral for caps or pacing during normal operation rolls to the next valid window automatically, with no catch-up bursts.
- **Missed-window rule (proposed default):** if a message's assigned window passed while the worker was unavailable (no heartbeat covering the window), the message goes to `needs_reapproval`. Messages whose window hasn't passed remain queued. Ordinary restarts preserve approvals.
- **Startup / resume / network recovery:**
  1. Reconcile any `sending`/in-flight attempts (§9.6).
  2. Complete the Gmail sync (§10).
  3. Apply the missed-window rule.
  4. Only then resume dispatch.
- **Persistent pause** survives restarts.

### 9.5 Worker priorities
The tray worker processes in this order:
1. Reconciliation
2. Gmail sync
3. Due follow-up generation
4. Dispatch
5. Exports/report
6. Discovery/AI jobs

Slow discovery or AI work never delays Gmail safety checks or dispatch. A heartbeat is recorded every cycle.

### 9.6 Single send gateway
**All** sending paths (UI approval, scheduler tick, recovery) call one gateway. There is no other code path to `messages.send`.

1. **Preflight** (no DB lock held across network):
   - Gmail token valid and the scopes and account match the snapshot.
   - Sync is current with no unresolved gap.
   - **For follow-ups:** refresh the exact campaign thread and messages from the PI's known addresses since the last send.
   - **For Email 1:** check prior-contact records and Gmail metadata for messages to/from the PI's addresses. Any hit → `paused_review(prior_contact_found)`. A bounded scan does not prove no prior contact; the UI says so.
   - Mode/recipient validation: live = the campaign's verified PI address only; transport_test = allowlist only; sandbox = fake transport only.
   - Window, pause, and caps.
   - Log the check time.
2. **Atomic claim** (one short transaction):
   - Conditionally move the message `approved_queued → sending` only if it is still `approved_queued` and the snapshot hash matches.
   - Validate or consume the campaign's existing reservation (§11).
   - Reserve the 24-hour budget.
   - Insert a `send_attempt` (status `in_flight`) holding the attempt ID, app Message-ID, `X-Marcelorian-Id`, mode, sender, recipient, and snapshot hash.
   - A unique partial index allows only one unresolved attempt per message.
   - Commit.
3. **Submit:** build MIME from the snapshot bytes only and call `messages.send` once, with no retries.
4. **Definite success:** in one transaction, mark the attempt and message `sent`, store the Gmail message and thread IDs, transition the campaign, record budget usage, and compute the next `due_at`. **Commit before** any export or Sheet work; export failure never causes a resend.
5. **Definite rejection** (Gmail error proving non-acceptance): mark `failed_definite`, release the budget and the attempt, and show the error. Re-approval is required to try again.
6. **Ambiguous** (timeout, connection loss, 5xx, crash): reconcile.
   - Search Sent by `rfc822msgid:` and corroborate by recipient, time, and thread.
   - Found → `sent`.
   - Not found or search failed → `uncertain`, with the reservation and budget **held** and the campaign moved to `paused_review(send_uncertain)`. **"Not found" alone never makes the message eligible for retry.**
   - The applicant resolves it explicitly and auditably as: confirmed sent / evidence of definite non-submission (re-approval required) / still unresolved.
7. **Crash after Gmail accepted but before the commit:** on startup, an `in_flight` attempt goes through reconciliation (step 6), never a fresh send.

Verify in P0 that Gmail preserves the custom Message-ID and header; if it does not, correlate using the returned IDs and metadata and document the behavior.

### 9.7 Threading
Follow-ups use the original campaign `threadId`, set `In-Reply-To` and `References` from the actual RFC Message-IDs, and use one subject chain (`Re: Research Position Application – Ophthalmology`). Never merge distinct PIs because they share a subject. Test the en-dash subject and `Re:` normalization on real accounts in P0.

---

## 10. Gmail synchronization and inbound classification

### Scopes
`gmail.send` and `gmail.readonly` only.

### Metadata only
Use `format=metadata` with an explicit `metadataHeaders` allowlist and a `fields` mask that excludes `snippet`, payload bodies, and attachments.

Header allowlist: From, To, Cc, Date, Subject, Message-ID, In-Reply-To, References, Auto-Submitted, X-Autoreply, X-Autorespond, Precedence, Content-Type.

**Never request** `full` or `raw`, and never fetch attachments. The applicant opens Gmail to read the content.

### History sync
- Use the `history` API. Persist the cursor **only after every page in the range has been processed**.
- If the cursor is expired or invalid: reconcile **all tracked campaign threads and known contact addresses** from the last fully covered timestamp, paginating fully and including spam/trash where the API permits. A fixed "last N days" query is not acceptable.
- Sending stays **blocked** (`sync_gap`) until the gap is closed.
- Record `last_gmail_coverage_at`. It advances only after a full successful sync.

### Matching
- Match using thread ID, exact known contact identities, and `References`/`In-Reply-To`.
- **Never** match by subject or university domain alone.
- A thread mapping to several campaigns → pause all of them for review.
- An unknown participant or new address in a tracked thread → `paused_review(unknown_participant)` until the applicant confirms the relationship.
- Ignore the app's own sent messages by stored Gmail IDs.

### Classification (rule-based heuristics, not proof)
- **Possible delivery report** (`mailer-daemon`, `postmaster`, or `multipart/report; report-type=delivery-status`): `paused_review(delivery_notice)`. The slot is **held**. The applicant checks Gmail and confirms one of: bounce/invalid address (→ `closed: bounce_confirmed`, address invalid), delay/other (resume), or policy rejection (closed, address **not** marked invalid). Do not fetch bodies to decide.
- **Auto-reply** (`Auto-Submitted` ≠ `no`, `X-Autoreply`, `X-Autorespond`, `Precedence: auto_reply|bulk|junk`, or subject patterns like "Out of Office" / "Automatic reply"): `paused_review(auto_reply)`. The slot is held. The applicant sets a resume date or closes. A resolved event is stored and never re-pauses the campaign.
- **Message from the applicant in the thread not sent by the app:** `paused_review(manual_applicant_message)`.
- **Clearly matched non-automatic message from the PI's known identity:** genuine reply. Stop the sequence, cancel queued and not-yet-generated future messages, set `conversation`, free the slot, and send a toast.
- **Anything doubtful:** pause and hold the slot.
- Replies after `sequence_complete` still update the campaign.

### Reply tags (applicant)
Positive / Requests info / No opening / Referral / Contact later (date) / Do not contact.

- **Referral** and **Contact later** create manual `task`s. The applicant enters the referred person's details; the app never claims to extract them.
- **Do not contact** adds the PI to the do-not-contact list.

### Known limit
The final preflight reduces but cannot eliminate the race with a reply arriving just before submission. The UI and docs state this.

---

## 11. Caps, reservations, and budgets

All counts are per mode, except the real-submission budget.

- **Pending slots** (institution ≤ 2, lab group ≤ 1): a campaign holds a slot while in `outreach_pending` or `paused_review`.
  - **Approving Email 1 atomically reserves** the institution and lab slots and the projected 30-day contact.
  - At dispatch, the gateway **validates the campaign's existing reservation** without double-counting. A campaign occupying the last allowed slot can send.
  - Follow-ups never acquire new slots or count as new contacts.
- **Rolling 30-day new contacts** (≤ 3 per institution): count successful Email 1 `sent_at` timestamps plus reserved-but-unsent Email 1s (projected). The final check happens at dispatch. Replies or completion never erase history.
- **Rolling 24-hour budget** (≤ 25): counts every real Gmail submission by the app (live + transport_test, including reminders), plus `in_flight` and `uncertain` attempts (held conservatively until resolved). Sandbox sends use a separate logical budget. These limits do not count mail the applicant sends manually and do not replace Google's own limits.
- **Concurrency:** reservations happen in transactions so parallel approvals/sends can never oversubscribe.
- **Boundaries:** exactly 24 h and 30 days, inclusive/exclusive as defined in code and tested.
- **Lowering a limit or merging groups below current occupancy:** keep history, block new reservations, show "over capacity," and never cancel existing conversations or double-count.
- **Lab conversation warning:** before reserving a slot for a PI in a lab group with a `conversation`, show its status; the applicant must confirm.
- **Optional stricter setting (off by default):** hold the slot 14 days after Email 4.
- Caps apply to this applicant's local records only.

---

## 12. Modes and testing levels

1. **Automated tests:** fake Gmail transport + injected clock. No network.
2. **`sandbox` mode:** the full workflow runs on an isolated dataset with the **same** caps, states, scheduling, approvals, and gateway, using a fake transport. Buttons simulate PI reply / auto-reply / delivery notice / manual applicant message. It does not consume live capacity because its records are isolated, not because checks are disabled.
3. **`transport_test` mode:** real Gmail, but every To address is validated against an **explicit allowlist** of consenting test addresses (e.g., the applicant and the owner). The intended PI and the actual test recipient are both recorded. Any test banner is part of the approved snapshot. A self-send tests connectivity only; a reply from a **second** controlled address is required to test real reply classification and cancellation.

**Mode switching:**
- Pauses the worker and requires explicit confirmation.
- Nothing crosses modes: no test-approved message, debug schedule (shortened intervals are allowed only in sandbox/transport_test), or test thread ever becomes live.
- Converting a candidate to live creates a fresh live campaign with fresh approval and duplicate-contact checks.

The 20–30-candidate pilot evaluates discovery, evidence, drafting, and sandbox behavior. It does not need 80–120 real test emails. Turning live mode on requires completing a **Live readiness checklist** (P0 transport checks passed, Gmail reconnect tested, consent set, caps reviewed).

---

## 13. Records, exports, and the owner report

### CSV exports (live mode; sandbox exports go to a separate subfolder)
- Updated after every committed change via atomic replace. UTF-8 with BOM for Excel; properly quoted multiline fields.
- If Excel locks the file: no crash, no change to sending state. Show "export stale" and retry independently.
- **Formula-injection protection:** prefix any cell beginning with `= + - @ \t \r` with `'`. This is documented and reversible. SQLite keeps the exact text. Test exact round-trip reconstruction.

**`outreach_contacts.csv`** (one row per campaign): campaign_id, mode, pi_name, salutation, title, institution, institution_state, lab_group (verified Y/N), pi_email, email_confidence, ownership_verified, current_role_verified, sources, subspecialty, research_fit, eligibility, opportunity, appointment_funding, unpaid_flag, funding_unknown_flag, grant_evidence, campaign_state, pause_reason, email1–4 sent_at, reply_detected, reply_tag, next_action, next_due_at, next_eligible_window, cap_block_reason, last_gmail_coverage_at, notes.

**`email_log.csv`** (one row per logical message and attempt): message_id, campaign_id, mode, step, approval_version, snapshot_hash, intended_recipient, actual_recipient, subject, body (exact approved text, escaped as above), attachment name/version/SHA-256, approved_at, attempt_id, app_message_id, x_marcelorian_id, sent_at, gmail_message_id, gmail_thread_id, outcome (sent / failed_definite / uncertain / cancelled), error.

### Owner report (Google Sheet)
- **Consent** is separate from the Gmail connection and optional.
- The Sheet is created in the **applicant's** Drive with the `drive.file` scope. That scope covers app-created or explicitly authorized files, not "only one file," so the app stores the report spreadsheet ID and restricts **all** report operations to that ID. Shared with the owner as **Viewer**; the applicant remains the owner. No public sharing, no service account.
- **Default column allowlist:** campaign_id, pi_name, institution, institution_state, subspecialty, appointment_funding, unpaid_flag, funding_unknown_flag, grant_evidence, campaign_state, pause_reason, email1–4 sent_at, reply_detected, reply_tag, next_action, next_due_at, cap_block_reason. A header block shows `last_gmail_coverage_at` and `last_successful_sheet_sync_at`; failed syncs do not advance these.
- **Never shared by default:** notes, CV content, visa details, or correspondence. Full outgoing text needs separate explicit consent. Inbound content is never shared.
- Values are written with `valueInputOption=RAW`. The app never reads Sheet cell edits as commands. "Write-only" describes data flow; the app may read permissions to verify sharing.
- Changing the owner's email requires renewed consent.
- **Revoking consent:** stop writes immediately and attempt to remove the owner's permission while credentials exist. Report failures and give manual steps. Withdrawing full-text consent removes previously published full-text values where possible. The UI states that copies the owner already downloaded cannot be recalled.
- **Authorization:** verify the granted scopes and account after each consent. Do not assume incremental authorization works like a web-server flow; implement and document a tested re-consent flow when optional scopes change, without expanding Gmail scopes.

---

## 14. Backups, deletion, and privacy statements

- **Backups:** daily, plus before every migration, using the SQLite **online backup API** (never a raw file copy of a live WAL DB). Keep 14. Test restoring.
- **A restored backup starts with sending paused** and requires full Gmail reconciliation before any queued message can dispatch.
- **"Delete all my data"** runs in order:
  1. Stop the worker and cancel all queued approvals.
  2. Attempt to remove the Sheet sharing and revoke the Google token.
  3. Remove local credentials (including the local copy of the Claude key).
  4. Remove the DB + WAL/SHM, all backups, exports, CV versions, caches, logs, and sessions.

  Then show what remains outside the app: sent Gmail messages, the Sheet in the applicant's Drive, owner copies, OS/OneDrive backups. Explain that the owner revokes the Claude key from the Anthropic console.
- **Disclosures:** SQLite files are not encrypted by default and may be copied by Windows/OneDrive backup. Never claim "all data stays on the laptop" when CV extraction (Claude) or monitoring (Sheet) is enabled. The README notes Google's API Services User Data Policy.

---

## 15. Error handling

- **Gmail auth failure, scope mismatch, or account mismatch:** halt all dispatch. Show a "Gmail needs reconnecting" banner and toast with a reconnect button. Production publishing status avoids the Testing-mode 7-day token expiry, but tokens can still be revoked or expire, so this state is permanent and expected.
- **Sync failure:** dispatch stays blocked; the banner shows `last_gmail_coverage_at`.
- **Public API errors:** backoff and retry (read-only calls only). Keep partial results.
- **LLM errors or invalid output:** no draft is created; offer a retry. Never fall back to unvalidated text.
- **Claude key invalid, model inaccessible, or spend limit reached:** clear status message; drafting is disabled and everything else continues.
- **Export/Sheet failure:** stale indicator plus independent retry; never touches sending state.

---

## 16. Screens

1. **Setup wizard:**
   1. Welcome + slogan
   2. Claude key (validate model access)
   3. Profile
   4. CV upload → optional consented AI extraction (review/redact first) or manual facts → confirm facts
   5. Publications
   6. Tone
   7. Connect Gmail (verify account + scopes)
   8. Optional monitoring consent + owner email → create Sheet (Viewer share)
   9. Auto-start consent
   10. Mode = **sandbox** by default
2. **Dashboard:**
   - Counts by state; next dispatches.
   - Attention items: reconnect, sync gap, uncertain sends, paused reviews, needs re-approval, over capacity.
   - `last_gmail_coverage_at`, export/Sheet freshness.
   - Local AI cost estimate, labeled as an estimate (not the provider bill).
   - Current mode banner.
3. **Discover:** options, progress, per-source status.
4. **Candidates:** filters (funding, grant evidence, fit, subspecialty, state, institution, contact quality). The detail view shows the evidence with excerpts, sources and dates, assessment dimensions, cap/reservation status, and lab-group suggestions. Actions: Draft · Skip · Do not contact · Merge/dismiss lab suggestion · Verify email/tz/salutation/role.
5. **Approval queue:**
   - Snapshot preview with claims beside excerpts.
   - Checklist results and the "applicant review is the final factual check" note.
   - Inline edit; edits trigger revalidation.
   - Approve and queue / Approve and send, with expected time in both zones.
   - Reject (with reason) and Cancel.
6. **Campaigns:** per-PI timeline (message states, due dates, eligible windows, attempts).
7. **Replies & reviews:** inbound events and pause reasons to resolve, with links to open in Gmail. No AI summaries.
8. **Tasks:** referrals, contact-later.
9. **Posted opportunities:** manual entries, deadlines, formal application route.
10. **Settings:**
    - Caps, window, pacing, last-interval mode.
    - Optional blackouts (off), optional 14-day hold (off).
    - Model, templates, tone.
    - Institution/lab grouping, time-zone overrides.
    - Allowlist (transport_test), mode switch.
    - Owner email and consent.
    - Delete all data.

---

## 17. Packaging, distribution, and guides

- PyInstaller onedir + Inno Setup. Per-user install, no admin. Start menu and desktop shortcuts. Optional Startup entry for the tray (consented in the wizard).
- Bundle pinned dependencies, `tzdata`, templates, specialty config, Alembic migrations, and the **Desktop OAuth client config**. Installed-app client IDs are not confidential mailbox credentials, but keep the config out of the public repo and inject it at build time from a GitHub secret.
- **Never bundle** refresh tokens or the owner's Anthropic key. The key is entered in the wizard and stored in Credential Manager. The docs state plainly that a key used on the tester's laptop cannot be hidden from that laptop's controller; the **provider-enforced spend limit** bounds the owner's exposure.
- **Updates:** new installer; data in `%LOCALAPPDATA%\Marcelorian\` is preserved; back up, then migrate.
- **Distribution:** owner-controlled channel (private GitHub release) with a published SHA-256 checksum. The unsigned-build SmartScreen prompt ("More info → Run anyway") is explained as **not** being a safety guarantee. Never instruct disabling Defender or SmartScreen, or bypassing organizational controls.
- **Target environment:** pilot acceptance on a supported **Windows 11** release. Windows 10 may work, but the docs note that ordinary Windows 10 support ended Oct 14, 2025. Record exactly which Windows builds were tested.

**`docs/OWNER_SETUP.md`:**
- Create the Google Cloud project; enable the Gmail, Sheets, and Drive APIs.
- Consent screen: External, minimal scopes, publishing status **In production**. Explain the unverified-app warning and that the personal-use verification exemption applies only to the developer and a few personally known users; Testing mode issues 7-day refresh tokens.
- Create the Desktop OAuth client and the GitHub secret.
- Anthropic: dedicated workspace, pilot key, spend limit, and how to revoke it.
- OpenAlex API key and NCBI API key (optional).
- Build/release with checksum.

**`docs/APPLICANT_GUIDE.md`** (plain language, short steps, screenshot placeholders): verify the checksum and install; the SmartScreen prompt; the wizard; sandbox first; reading the dashboard; approving; what "expected time" depends on; reconnecting Gmail; resolving pauses; going live via the checklist; deleting data.

---

## 18. Build phases (stop after each)

- **P0 — Safe foundation + Gmail spike.**
  1. Repo skeleton, settings, DB with the full campaign/message/snapshot/attempt/reservation/budget model, migrations, keyring, local UI session security.
  2. Fake transport, recipient allowlist, approval snapshot binding, the **single send gateway** with atomic claim, reconciliation, and the uncertain state. Unit tests.
  3. **Then, with my explicit go-ahead,** a Windows Gmail spike in `transport_test`: self-send (connectivity); a two-address thread where the second address replies; verify threading, the en-dash subject and `Re:`, Message-ID/header preservation, metadata-only fetch, and history sync.

  No real PI contact. Report untested items. Do not declare the 7-day token survival passed until 7+ days have actually elapsed.
- **P1 — Profile and discovery.** Wizard steps through publications; consented CV extraction and manual facts; PubMed, OpenAlex (API key), RePORTER, seed-URL faculty pages (optional ClinicalTrials.gov); identity/affiliation/email/tz verification; evidence; assessment; candidates UI.
- **P2 — Drafting, review, and rules.** Templates, LLM fill + validation, the approval queue, and the pure scheduling/caps/reservation/state logic with full tests. Sandbox mode end to end. Reuse the P0 gateway; no temporary sender.
- **P3 — Automation.** Tray worker, dispatcher priorities, windows and pacing, missed-window/restart/sleep/resume recovery, history sync with gap recovery, inbound classification, the reviews and tasks screens, toasts.
- **P4 — Records and report.** CSVs (atomic, formula-safe, lock-tolerant), consented Sheet with Viewer sharing and revocation, dashboard freshness, cost estimate, backups/restore, delete-all-data.
- **P5 — Packaging and docs.** Installer, auto-start, CI release with checksum, both guides.
- **P6 — Acceptance.** All automated tests (§19), plus a separately reported, explicitly authorized live checklist on Windows 11.

---

## 19. Acceptance tests (automated with fake Gmail + injected clock unless marked LIVE)

**States and reservations**
1. Email 1 dispatches from `outreach_pending` with no prior thread.
2. Follow-up drafting keeps the slot.
3. A campaign holding the last institution slot can still send its own Email 1.
4. A 3rd pending PI at an institution is blocked; a 4th new contact within 30 days is blocked even with free slots; reminders never count as new contacts; a reply does not erase 30-day history.
5. Concurrent approvals cannot oversubscribe; exact 24-hour and 30-day boundary behavior.
6. Lowering limits below occupancy blocks new reservations without cancelling anything; merged groups don't double-count.
7. An unverified lab suggestion blocks a second same-institution contact until reviewed; the lab conversation warning appears.
8. Cancelling an unsent Email 1 releases the reservation; revoking a follow-up does not.

**Approval and snapshot**
9. Changing the mode, sender, recipient, body, or CV invalidates the approval.
10. The latest CV is never substituted into an approved message.
11. A sandbox or transport_test message never becomes live; switching modes pauses the worker.

**Timing**
12. Calendar-month math including Jan 31 and DST transitions, computed in the PI zone from the actual `sent_at`.
13. Due Tuesday, approved Thursday → sent Thursday; the next interval counts from Thursday.
14. An unknown/ambiguous time zone blocks scheduling.
15. No send starts at or after 11:00 local.
16. Pacing; cap deferral rolls to the next window with no burst.
17. Worker asleep through the assigned window → `needs_reapproval`; an ordinary restart preserves the queue; persistent pause; cancellation.
18. Optional blackouts are off by default; when on, they defer without rewriting due dates.

**Gateway and recovery**
19. Double-click + scheduler tick at the same moment → exactly one submission.
20. Process dies before submission → the message is reclaimable only via defined recovery, never duplicated.
21. Process dies **after** Gmail accepted → reconciliation finds it → `sent`, with no resend.
22. Reconciliation "not found" or failed search → `uncertain`, budget and slot held, never auto-retried, never forgotten; explicit, audited manual resolution.
23. A definite Gmail rejection → `failed_definite`, budget released.
24. Export failure after a successful send → no resend; stale export indicator.

**Inbound**
25. A reply arriving before the final preflight cancels the send; the documented race boundary is noted.
26. An acknowledged auto-reply is not reprocessed forever.
27. An unknown participant pauses the campaign.
28. An unrelated same-domain email is ignored; the same subject across PIs is never merged.
29. A manual applicant reply pauses the campaign.
30. A late reply after Email 4 moves the campaign to `conversation`.
31. Expired history with a missed older reply is found by gap reconciliation; pagination failure or spam/trash coverage keeps sending blocked until resolved.
32. A delivery delay notice → review with the slot held; a confirmed bounce closes the campaign and invalidates the address; a policy rejection does not invalidate the address.
33. No inbound body, snippet, or attachment is ever requested, verified by inspecting the request parameters.

**AI and drafting**
34. A valid evidence ID belonging to a different PI is rejected; an unconfirmed fact is rejected.
35. Prompt-injection text in an abstract or page has no effect on recipients or settings.
36. "Attached" without an attachment is blocked; "updated CV" without a newer version is blocked.
37. No Gmail content appears in any AI request (request capture test).
38. Applicant-edited text is revalidated.

**Funding and identity**
39. Grant evidence keeps the appointment funding `Unknown`.
40. Explicitly paid vs. explicitly unpaid postings; `paid only` excludes non-paid appointments.
41. An ambiguous author-email attribution is classified C.
42. A homonym is not auto-merged.

**Modes**
43. Sandbox enforces caps internally and is isolated from live.
44. The transport_test allowlist is enforced at the gateway.
45. A no-reply 4-message sequence reaches Email 4.
46. A separate reply scenario stops before the later emails.

**Report, export, storage, security**
47. Viewer-only sharing; revocation success and failure paths; changed owner email requires new consent.
48. Default Sheet columns exclude sensitive free text; staleness is shown on failure; Sheet edits cannot trigger anything; values are written RAW.
49. Excel lock tolerance; formula-safe, lossless round trip.
50. Backup consistency; restore starts paused and reconciles before dispatch.
51. Delete-all-data removes all app-managed files, including backups, and reports what remains.
52. Local endpoints reject requests without a session, with a bad Origin/Host, or without CSRF; no state-changing GETs.
53. The SSRF guard blocks private IPs and redirects to them.

**LIVE (Windows 11, transport_test, allowlisted addresses only, explicit authorization)**
- L1: Self-send; a two-address thread with a reply from the second address; verify threading/subject.
- L2: Reconnect flow after revoking the token.
- L3: Token still valid after 7+ days (reported when elapsed).
- L4: Installer install/update/uninstall, with data preserved on update.

---

## 20. Traceability (safety requirement → component → tests)

| Requirement | Enforcing component | Tests |
|---|---|---|
| No duplicate execution | Send gateway atomic claim + unique unresolved-attempt index | 19–22 |
| No blind retry under uncertainty | Gateway reconciliation + `uncertain` state | 21–23 |
| Exact approved content only | Approval snapshot + hash check at claim | 9–11, 38 |
| Caps not oversubscribed or self-blocking | Reservation service (transactional) | 3–8 |
| Stop on reply | Sync + matcher + preflight | 25, 29–31 |
| No missed replies after history expiry | Gap reconciliation + `sync_gap` block | 31 |
| Conservative bounce handling | Inbound classifier (metadata) + review | 32 |
| No Gmail content to AI | AI input builder + metadata-only fetch | 33, 37 |
| Claims grounded in evidence | Draft validator (ownership + confirmation) | 34–36, 38 |
| Funding labels truthful | Assessment rules | 39–40 |
| Test traffic never live | Mode partitioning + gateway recipient validation | 11, 43–44 |
| Correct timing | Scheduler (UTC + IANA, tzdata) | 12–18 |
| Report is read-only + consented | Report service (allowlisted file ID, RAW) | 47–48 |
| Audit integrity | Exports (lossless, formula-safe) + backups | 49–51 |
| Local UI protected | Session bootstrap + Host/Origin/CSRF | 52 |

---

## 21. Proposed defaults (not previously decided by the owner; implement as settings)

- **Missed-window recovery:** require re-approval when the worker was unavailable for the whole assigned window (§9.4).
- **Blackouts** (federal holidays, Dec 20–Jan 5): available, **off** by default.
- **Sandbox** is the default mode at first run; live requires the readiness checklist.
- **Tone preferences** replace AI writing samples.
- Pilot acceptance targets **Windows 11**.
- **ClinicalTrials.gov** is optional enrichment only.

No other open decisions. If implementation reveals one, stop and ask.
