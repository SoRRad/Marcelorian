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
- Write tests with the code. Scheduling, caps, state transitions, and the send gateway must be unit-tested against a fake Gmail transport and an injected clock **before** any real Gmail send. These pure safety rules are therefore built in P0, not P2 (§18; decision B1).
- Recheck official provider documentation (Google, OpenAlex, NCBI, NIH RePORTER, Anthropic) when implementing an integration. Record the date checked in code comments or docs. Mocked tests do not prove a live integration.
- Never commit secrets or real applicant data. Never contact a real PI during development.
- Prefer boring, well-maintained, pinned libraries. Do not add agent frameworks, a cloud queue, a service account, an admin dashboard, or new paid services.
- **Repository publication is conditional (A1–A2).** Inspect the actual remote, branches, permissions, and visibility first. Publish to the remote only when it is confirmed private and writable; otherwise keep working in local Git and report the condition. Never force-push, overwrite branches, change visibility or branch protection, or merge.
- **Development is not Windows acceptance (A3).** The cross-platform core may be developed and unit-tested on Linux. Windows-specific adapters sit behind explicit interfaces. Windows CI results, and interactive desktop behavior (OAuth, tray, toasts, sleep/resume, Credential Manager), are reported separately; mocks never establish desktop acceptance.
- Decisions taken after the v2 review are recorded in `docs/DECISIONS.md` with their IDs (A1–D5). This spec remains the single source of rules.

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
| Sequence | 4 emails total. Email 2 is due 7×24 h after Email 1 **actually sent**. Email 3 is due 7×24 h after Email 2 actually sent. Email 4 is due 1 calendar month after Email 3 actually sent. A setting allows N days for the last interval (calendar days in the PI's time zone; §9.3, C8). |
| Approval | Every message, including each follow-up, is approved individually. Approval authorizes one immutable snapshot (§9.1). |
| Sending | A tray worker starts at Windows login (with explicit setup consent). Approved messages are queued and dispatched in the next permitted window. If approved inside a window, they dispatch promptly once all checks and pacing allow. |
| Window | Tue–Thu, 08:00 (inclusive) to 11:00 (exclusive), in the PI's **verified** IANA time zone. |
| Caps | 2 pending PIs per institution; 1 per lab; 3 newly contacted PIs per institution per rolling 30 days; 25 app-originated real Gmail submissions per rolling 24 hours. All adjustable (validated; increases above these defaults need explicit confirmation and are audited; §11, B16). |
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
- **anthropic** SDK. The model is configurable, default `claude-sonnet-5-5`. At local setup, validate credential/model access with an authenticated model-metadata lookup and **no generation request**; label it as credential/model-access validation, not proof of inference billing, remaining budget, or successful generation (D3).
- **keyring** with Windows Credential Manager for all secrets and tokens. **No plaintext fallback**: if keyring is unavailable, setup fails clearly. Tests may inject an in-memory secret store; production code can never discover or select it, and no live credentials are used in development or CI (A3).
- **pdfplumber**/**pypdf** for local CV text extraction. **httpx** for public APIs.
- **tzdata** bundled (Windows may lack the IANA database).
- **PyInstaller** (onedir) + **Inno Setup**. **pytest**, **hypothesis** property tests for timing and cap boundaries, **ruff**, and strict typing (mypy) for safety-critical modules (D4).
- **GitHub Actions** runs ordinary Ubuntu and `windows-latest` test jobs with least-privilege `permissions`, job timeouts, and cancellation of superseded runs; no applicant secrets and no live service calls. Recheck current GitHub billing documentation and the account's allowance before enabling runs; never enable paid overages or alter budgets. The installer artifact build is added in P5 (A4).

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

Every row that relates to sending carries a **`mode`** (`sandbox`, `transport_test`, `live`). Modes never share campaigns, approvals, logical messages, thread mappings, institution/lab capacity, projected contacts, exports, or reports. **Documented exceptions (C9):** mailbox synchronization (one mailbox-level cursor/coverage record) and the real Gmail submission/pacing ledger are shared by `live` and `transport_test`; `sandbox` shares neither (§10, §11). Real profile/PI/evidence/assessment records may be reused across modes; genuine edits to them are visible everywhere and follow the re-approval rules (§9.1). Synthetic identities and simulated state changes can never alter live records or be silently promoted into them.

### Campaign lifecycle (one per applicant × PI × mode)

| State | Meaning | Holds pending slot? |
|---|---|---|
| `candidate` | No outreach yet | No |
| `outreach_pending` | Email 1 approved (slot reserved) or any email sent and awaiting reply | **Yes** |
| `paused_review` | Reason: auto_reply, delivery_notice, manual_applicant_message, unknown_participant, ambiguous_thread, send_uncertain, sync_gap, prior_contact_found, missed_window | **Yes** |
| `conversation` | Genuine reply received. Sequence stopped. Still visible. | No |
| `sequence_complete` | Email 4 sent. Reply monitoring continues; a later reply moves it to `conversation`. | No |
| `closed` | Outcome: bounce_confirmed, no_opening, do_not_contact, applicant_closed (reason required) | No |

**Invariants**
- A campaign whose follow-up is in any message state keeps its slot. Drafting never releases a slot.
- Only these release a slot: a genuine reply, Email 4 sent, or a `closed` outcome. Cancelling an unsent Email 1 releases its reservation. Revoking a follow-up's approval does not.
- **Cancel before first send (B3):** cancelling an unsent Email 1 returns the campaign to `candidate`, preserves the logical message and all audit history (audited as `cancelled_before_first_send`), and releases its capacity and projected-contact reservations. Re-drafting creates a new revision and requires fresh approval. This is allowed only when the campaign has no sent message and no in-flight or uncertain attempt; a missing `sent_at` is not proof of non-submission. Cancellation never resets uncertainty and never lets another Email 1 bypass reconciliation, do-not-contact, or prior-contact checks.
- **Reservations survive failure (B4):** a campaign that legitimately owns reservations keeps them while its Email 1 is `failed_definite` or `needs_reapproval`, until re-approval or explicit cancellation/closure. The UI shows that capacity remains reserved. Moving into a review state never creates a reservation.
- **Follow-up revoke and stop (B5):** *Revoke* returns an approved, unclaimed follow-up to `awaiting_approval` and keeps the slot. *Stop this follow-up sequence* disables further generation and dispatch and keeps the slot until the applicant closes with a reason or deliberately re-drafts the next unsent step. An already-sent step is never re-drafted.
- **Independent pause reasons (B13):** pause reasons are tracked individually; resolving one never clears another. Recovery or a late result never restarts a stopped sequence, never forces `outreach_pending`, and never overwrites a newer `conversation` or `closed` state.
- A genuine reply frees the unanswered pending slot but never erases an unresolved submission attempt, its budget hold, or the new-contact ledger entry (B13).
- `sequence_complete` stops sending, not monitoring.

### Message lifecycle (one logical message per `(campaign_id, step, mode)`, unique)
`draft` → `awaiting_approval` → `approved_queued` → `sending` → `sent`

Side states:
- `failed_definite`: Gmail definitively rejected the message before acceptance. It may be re-approved.
- `uncertain`: the outcome is unknown. Never auto-retried. A Cancel button never clears it (B5).
- `cancelled`
- `needs_reapproval`: snapshot invalidated (a dependency or the message's own content changed, §9.1) or assigned window missed (§9.4).

Cancelling a claimed (`sending`) message is not a recall: the UI says submission may already have started, future steps stop, and the current attempt is reconciled (B5, §9.6).

Revisions create new `approval_snapshot` versions, not new logical messages. Every transition writes an `audit_event` (timestamp, actor, reason).

### Identity and duplicate protection
- Re-discovery, PI merges, or a second campaign must never bypass do-not-contact or duplicate-outreach checks.
- **Do-not-contact matching (C7):** verified PI identity and aliases, confirmed merged records, exact known addresses, and reliable ORCID/OpenAlex associations. Name + institution alone produces a review warning, never an automatic identity match. Simulated do-not-contact events (sandbox, transport_test) never edit the applicant's live do-not-contact list.
- Merging PIs preserves all contact and sending history, and all restrictions.
- Homonymous authors are never auto-merged; merges need the applicant's confirmation.

---

## 4. Applicant profile and AI input boundaries

### Profile (wizard + editable)
- **Identity and training:** full name, preferred name for signature, degree(s), medical school, graduation year, `current_role_phrase` (e.g., "a research fellow at X"), US training status (USMLE steps, ECFMG, any US residency), visa status/need (category + free text, shown factually, never interpreted), earliest start date, desired duration, preferred US states (optional), signature block.
- **Preferences:** funding requirement (`Exclude explicitly unpaid opportunities; unknown funding allowed` / `paid preferred, unpaid acceptable` — pilot default is the latter; the first option was formerly mislabelled "paid only", C1), subspecialty interests (default: all), research interests, methods/skills offered, tone preference (e.g., formal / warm-formal / concise).
- **CV:** PDF versions with SHA-256.
- **Publications:** imported from PubMed by name and/or ORCID; the applicant confirms each.
- **Other:** "recent news" field (used only in Email 3); do-not-contact list.

### Facts
Each `profile_fact` is `proposed` or `confirmed`, with its own version (incremented on every change) and a provenance (`applicant_entered`, `ai_extracted`, `imported`). **Only confirmed facts can appear in drafts.**
- Manual facts are confirmed only through an explicit **Save and confirm** action, with provenance `applicant_entered`. Autosave never confirms a proposed fact. AI-extracted and externally imported facts require applicant confirmation. Self-reported qualifications are never labelled externally verified (C2).
- Approvals record the exact fact versions and profile fields they use, so an edit invalidates only the approvals that depend on it (B18, §9.1).

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
Derive the IANA zone from the institution city/state. If it is ambiguous (multi-zone states, missing city), **approval and scheduling are blocked** until the applicant verifies it (B20). The applicant's zone is never a fallback.

---

## 7. Assessment and funding

Shown as separate dimensions, never one score:
- **Research fit:** Strong / Moderate / Weak. LLM, with evidence citations (validated as in §8.3).
- **Eligibility:** Meets / Unclear / Likely not. Rule-based against known requirements (e.g., a posting requiring a US ACGME residency vs. the applicant's training). Never makes visa or immigration determinations; shows the requirements and uncertainty factually.
- **Opportunity:** `Posted opening` / `No opening verified`.
- **Contact:** confidence A/B/C + ownership verified Y/N + current role verified Y/N.
- **Appointment funding:** `Paid – explicitly documented` / `Unpaid – explicitly documented` / `Unknown`. A posting proves an opening, not compensation; compensation must be explicitly stated. Do not assume an observership is an unpaid research appointment.
- **Research funding evidence (separate field):** `Active grant evidence` with source and dates, or none. It can raise priority but never clears the funding-unknown flag or implies salary availability.

Honor `Exclude explicitly unpaid opportunities; unknown funding allowed` by excluding **explicitly unpaid** appointments from drafting (they remain visible, with the reason). Unknown funding stays eligible and marked. Neither grant evidence nor a listing without stated compensation makes an appointment explicitly paid (C1). Default sort: appointment funding (Paid > Unknown > Unpaid), then grant evidence, fit, and contact quality. One-line summary per candidate, e.g., *"Strong methods match (OCT image analysis). Current role verified. No opening verified. Appointment funding unknown; active NEI grant evidence."*

---

## 8. Drafting

### 8.1 Default templates (editable in Settings)

Body templates are editable; the subject is fixed from the specialty config (exact en dash, one normalized `Re:` prefix for Emails 2–4) and is not editable (C5). Bodies are **plain text** with no tracking; applicant-approved professional URLs (e.g., ORCID or a publications profile) are allowed. There is no automatic quoting of earlier messages in v1 (B15). Emails 1 and 4 always carry the CV attachment, frozen in the approved snapshot (C5).

**Email 1. Subject:** `Research Position Application – {specialty_display_name}`
```
Dear {salutation},

My name is {full_name}, and I am {current_role_phrase}. I am writing to ask whether your group expects an opening for a research fellow or similar position starting {availability}.

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

I am following up briefly on my earlier email about research fellow opportunities in your group. Your work on {pi_topic} remains of strong interest to me, and I would welcome any guidance on openings.

Best regards,
{signature_name}
```

**Email 3** (reply in thread)
```
Dear {salutation},

I am writing once more in case my earlier email was missed. {optional_update}

If there is no opening at present, I would be grateful if you could suggest a colleague who may be recruiting.

Best regards,
{signature_name}
```

**Email 4** (reply in thread, with CV attached)
```
Dear {salutation},

I am reaching out a final time about research opportunities in your group. I have attached {cv_phrase} for your reference. If the timing is better later, I would be glad to reconnect then.

Thank you for your consideration.

Best regards,
{signature_name}
```

### 8.2 Field rules
- **LLM-filled only:** `personalized_sentence`, `skills_line`, `contribution_line`, `pi_topic`. The model returns strict JSON with, per field, the evidence IDs and confirmed fact IDs used. The prompt is guided by the applicant's tone preference.
- **Rule-based:**
  - `salutation`: default `Dr. {last_name}`, flagged until verified or edited.
  - `funding_line`: empty by default; never volunteers unpaid willingness unless the applicant enters it.
  - `optional_update`: only from "recent news"; the line is removed if empty.
  - `cv_phrase`: "my updated CV" **only** if the attached version is newer than the one sent in Email 1, otherwise "my CV".
  - `full_name`: the applicant's full name (Email 1 introduction) (C3).
  - `signature_name`: the applicant's explicit signature-name preference for the closings of Emails 2–4, defaulting to the full name; never an automatically derived informal first name (C3).
  - `availability`: Month YYYY from the confirmed earliest start date; the duration is included only when confirmed. A date or duration is never invented (C3).
- The model must not claim the applicant read a paper, imply the PI is recruiting without a posted opening, invent qualifications, or add unverified updates.
- The review screen shows each claim next to its supporting excerpt and source link/date. Citations never appear in the outgoing email.
- **Applicant edits (C4):** evidence links stay only on claims they still support. An edited claim is marked applicant-written/review-required until the applicant confirms it; a stale evidence-verified badge never stays attached to changed text. Whole-body editing never changes the recipient or subject.

### 8.3 Validation (runs on AI output **and again on the final applicant-edited text**)
- Every cited evidence ID exists, **belongs to this PI**, and is relevant to the field. Every cited fact ID belongs to this applicant, is `confirmed`, and its cited version is that fact's current version (B18).
- Revalidation of applicant-edited text reruns the deterministic checks only. The final edited email is **never** sent back to the AI for validation or restyling (C4). Fields match their schema and length limits. Any failure means no draft and an error is shown.
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
- **Relevant change = an actual dependency (B18).** Approval records the dependencies the message actually uses: each cited fact and its version, the signature and role-phrase fields, the recipient address and its send-ready status, the salutation, the institution time zone, the sender account, and the attachment versions. Changing any of these while the message is unsent and approved → `needs_reapproval`. Unrelated edits do not invalidate it. Changing the message's own body or attachment always requires re-approval.
- **CV versions (B19):** uploading a new CV never changes or invalidates an attachment already frozen in an approved snapshot. The UI shows the queued version and offers revoke/re-draft. Unapproved drafts may be re-drafted. Replacing the attachment **of that specific message** invalidates its approval.
- **Approval prerequisites (B20):** a verified time zone, a send-ready exact recipient (live: the campaign's verified PI address; transport_test: an allowlisted address), and a reviewed salutation. Role/affiliation uncertainty and eligibility/funding flags are shown honestly. These mutable prerequisites are rechecked at dispatch (§9.6).
- **Transport-test schedule override (B2):** only in `transport_test`, and only after explicit P0.3 authorization, a snapshot may bind an override (window always open and/or shortened follow-up intervals). The snapshot then visibly records the override, the actual allowlisted recipient, the fake PI identity, and the shortened timing. The real gateway, approval, exact-recipient checks, uncertainty handling, the shared real-send budget, and pacing still apply. Live mode rejects any snapshot that carries an override. There is no global "always open" setting.
- Each queued approval has a **Cancel / revoke** action:
  - **Revoke:** an approved, unclaimed message returns to `awaiting_approval`; reservations and the slot are kept.
  - **Cancel (Email 1, unsent):** see §3 (B3).
  - **Stop this follow-up sequence:** see §3 (B5).
  - Neither action recalls a claimed message or clears an `uncertain` attempt (§9.6).

### 9.2 Buttons and expectations
- Labels: **Approve and queue** (outside the window) or **Approve and send** (inside the window).
- Show the earliest expected dispatch time in **both** PI and applicant time zones. It is conditional on: the laptop awake, the tray running, network, caps, pacing, and a passing final Gmail check.
- The UI explains that closing the browser does not stop the tray, but quitting the tray, sleep, logout, or shutdown prevents dispatch.

### 9.3 Timing
- Store `due_at` (UTC), `eligible_window_start/end` (UTC), the PI's IANA zone, and `sent_at` (UTC) separately.
- Steps 2 and 3: due = previous `sent_at` + 7×24 h.
- Step 4: compute in the PI's IANA zone as previous `sent_at` + 1 calendar month, clamped to the end of the month (Jan 31 → Feb 28/29). Convert to UTC. Handle DST correctly.
- Step 4 with the N-day setting (C8): the same local wall-clock time N **calendar days** later in the PI's IANA zone, converted to UTC. DST handling for both step-4 modes: a local time that does not exist (spring-forward gap) moves forward by the length of the gap; an ambiguous local time (fall-back) uses its first occurrence. Steps 2 and 3 remain exactly 7×24 h.
- `sent_at` is the established actual submission time, with its provenance (gateway response, Gmail reconciliation, or applicant-reported). It is never replaced by a later reconciliation time; uncertain timestamp evidence requires review, not an invented timestamp (B13).
- In `transport_test` only, an approved schedule override (§9.1, B2) may shorten the intervals for that campaign.
- When a step becomes due, its draft is generated and placed in `awaiting_approval`. Only the **next** step is ever generated.
- Show the original due date and the window it will be sent in.
- **Optional blackouts (off by default):** US federal holidays and a Dec 20–Jan 5 range, both configurable. When enabled, they defer the eligible window; history and due dates are never rewritten.

### 9.4 Window, pacing, and recovery
- A message never **starts** sending at or after 11:00 PI local time. If preflight crosses 11:00, nothing is submitted (B10).
- Within a window, sends are spaced 2–6 minutes apart (random) and limited by caps. Deferral for caps or pacing during normal operation rolls to the next valid window automatically, with no catch-up bursts.
- **Pacing ledger (B8):** spacing is serialized and persisted across actual Gmail submissions in both `live` and `transport_test`, regardless of the PI's time zone. Sandbox uses its own simulated clock and pacing. A restart or mode switch never resets real-send spacing. A transport-test override never bypasses pacing.
- **Missed-window rule (B7):** re-approval (`needs_reapproval`, campaign `paused_review(missed_window)`) is required only when an assigned eligible window passes with **no functioning dispatch/safety evaluation** of the message during the portion in which it could have been considered, because the worker or required connectivity/sync was unavailable.
  - The relevant portion starts at the later of the window start and the approval time. It excludes deliberate persistent-pause intervals and intervals when another mode was active.
  - Every evaluation is recorded with its outcome and deferral reason. It counts as *functioning* only if the worker ran with valid Gmail authorization and a current sync. A process heartbeat alone is not proof of Gmail coverage.
  - Missing or untrustworthy evaluation evidence requires review. A successful check is never fabricated.
  - Ordinary brief restarts, pacing/cap deferrals, deliberate persistent pause, and deliberate mode switches do not by themselves invalidate approval; such messages roll to the next eligible window.
  - Messages whose window hasn't passed remain queued.
- **Modes (B9):** one mode is active for dispatch and follow-up generation. Switching mode is an intentional pause, not a missed-window event, and dispatch resumes only when the applicant explicitly resumes it for the selected mode. Switching never changes a stored message's mode.
- **Startup / resume / network recovery:**
  1. Reconcile any `sending`/in-flight attempts (§9.6).
  2. Complete the Gmail sync (§10).
  3. Apply the missed-window rule.
  4. Only then resume dispatch.
- **Persistent pause** survives restarts and continues until explicitly resumed.

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
   - An **incremental sync completes immediately before sending**. Any unresolved mailbox-level coverage gap blocks **all** real sends. A proven thread-specific problem pauses only that campaign; a mailbox or credential failure is never labelled thread-specific (B10).
   - **For follow-ups:** refresh the exact campaign thread and messages from the PI's known addresses since the last send.
   - **For Email 1 (B14):** reconcile the app's own attempts first. Then check prior-contact records and search Gmail metadata for messages to/from the PI's exact known addresses, with full pagination for the declared scope, spam/trash included, and drafts excluded. IDs may locate hits; only minimal permitted metadata and an *Open in Gmail* action are shown, never bodies or snippets.
     - Any unacknowledged hit → `paused_review(prior_contact_found)`.
     - The applicant either **acknowledges these reviewed messages and proceeds**, or **closes**. Acknowledgment is scoped to the mailbox, the campaign, and the exact Gmail IDs reviewed. It never ignores future mail from that PI, bypasses do-not-contact, or permits a duplicate app sequence. New unacknowledged hits trigger review again.
     - An incomplete search is not a clean result: dispatch is deferred.
     - A bounded scan does not prove no prior contact; the UI says so.
   - Mode/recipient validation: live = the campaign's verified PI address only; transport_test = allowlist only; sandbox = fake transport only. Live rejects any transport-test override (B2). The recipient's send-ready status, the verified time zone, the reviewed salutation, and do-not-contact status are rechecked (B20).
   - Window, pause, and caps.
   - Log the check time.
2. **Atomic claim** (one short transaction):
   - After the network preflight, **re-read** the current message, approval snapshot, campaign stop/review status, active mode, persistent pause, do-not-contact status, reservations, and budget. A reply, revocation, cancellation, pause, or mode switch recorded during preflight prevents submission (B10).
   - Conditionally move the message `approved_queued → sending` only if it is still `approved_queued` and the snapshot hash matches.
   - Recheck the due time, the applicable window (including the 11:00 limit), and pacing at this point. This claim is the final authorization immediately before Gmail is invoked (B10).
   - Validate or consume the campaign's existing reservation (§11).
   - Reserve the 24-hour budget.
   - Insert a `send_attempt` (status `in_flight`) holding the attempt ID, app Message-ID, `X-Marcelorian-Id`, mode, sender, recipient, and snapshot hash.
   - A unique partial index allows only one unresolved attempt per message.
   - Commit. **This commit is the documented point beyond which recall is not promised.** Cancellation and claim are serialized: a cancellation committed before it prevents submission; one committed after it is recorded as a stop request, and the attempt is reconciled (B5, B10).
3. **Submit:** build MIME (plain text) from the snapshot bytes only and call `messages.send` once, with no retries. The MIME is built before the claim, so only an in-memory hand-off separates the claim commit from the call.
   - **Identifiers (B12):** the RFC Message-ID is a unique opaque value at the verified sender's email domain, never the laptop hostname. A distinct value is used for each separately authorized attempt, kept throughout that attempt's reconciliation, and never regenerated because a search returned nothing.
   - `X-Marcelorian-Id` carries an opaque value with no applicant name, file path, or other personal data. It is a visible correlation header, not a secret or credential.
   - Message-ID is useful evidence, not a provider idempotency guarantee.
4. **Definite success:** in one transaction, mark the attempt and message `sent`, store the Gmail message and thread IDs, transition the campaign, record budget usage, and compute the next `due_at`. **Commit before** any export or Sheet work; export failure never causes a resend. A success that arrives after the campaign moved to `conversation` or `closed` is recorded without overwriting that newer state (B13).
5. **Definite rejection** (Gmail error proving non-acceptance): mark `failed_definite`, release the budget and the attempt, and show the error. Re-approval is required to try again.
6. **Ambiguous** (timeout, connection loss, 5xx, crash): reconcile.
   - Search Sent by `rfc822msgid:` and corroborate by recipient, time, and thread.
   - Found → `sent`, using the established actual submission time (§9.3).
   - Not found or search failed → `uncertain`, with the reservation and budget **held** and the campaign moved to `paused_review(send_uncertain)`. **"Not found" alone never makes the message eligible for retry.**
   - The applicant resolves it explicitly and auditably as: confirmed sent / evidence of definite non-submission (re-approval required) / still unresolved.
   - **Resolution rules (B13):**
     - The budget hold is released only when definite non-submission is supported and recorded, never because a Sent search is empty.
     - Resolving the attempt never forces the campaign to `outreach_pending` and never restarts a stopped sequence. A reply, do-not-contact instruction, closure, or other unresolved review reason that arrived meanwhile stays in effect.
     - Pending capacity is retained only when the current campaign still requires it.
7. **Crash after Gmail accepted but before the commit:** on startup, an `in_flight` attempt goes through reconciliation (step 6), never a fresh send.

Verify in P0.3 that Gmail preserves the custom Message-ID and `X-Marcelorian-Id` header, using metadata only (never bodies or raw data). If it does not, correlate using the returned IDs and metadata and document the behavior.

### 9.7 Threading
Follow-ups use the original campaign `threadId`, set `In-Reply-To` and `References` from the actual RFC Message-IDs, and use one subject chain (`Re: Research Position Application – Ophthalmology`). Earlier messages are not quoted automatically (B15). Never merge distinct PIs because they share a subject. Test the en-dash subject and `Re:` normalization on real accounts in P0.3.

---

## 10. Gmail synchronization and inbound classification

### Scopes
`gmail.send` and `gmail.readonly` only.

### Metadata only
Use `format=metadata` with an explicit `metadataHeaders` allowlist and a `fields` mask that excludes `snippet`, payload bodies, and attachments.

Header allowlist: From, To, Cc, Date, Subject, Message-ID, In-Reply-To, References, Auto-Submitted, X-Autoreply, X-Autorespond, Precedence, Content-Type, X-Marcelorian-Id (B12; needed for the P0.3 preservation test and reconciliation).

**Never request** `full` or `raw`, and never fetch attachments. The applicant opens Gmail to read the content.

### History sync
- Use the `history` API. Persist the cursor **only after every page in the range has been processed**.
- If the cursor is expired or invalid: reconcile **all tracked campaign threads and known contact addresses** from the last fully covered timestamp, paginating fully and including spam/trash where the API permits. A fixed "last N days" query is not acceptable.
- Sending stays **blocked** (`sync_gap`) until the gap is closed. A mailbox-level gap blocks all real sends; it is not recorded as a per-campaign problem unless one campaign's thread is proven to be the cause (B10).
- Record `last_gmail_coverage_at`. It advances only after a full successful sync.
- **Mailbox scope (B9, C9):** synchronization and reconciliation run for **all** real (`live` and `transport_test`) campaigns of the connected mailbox, including those of the inactive mode, whenever authorization permits. The single mailbox-level cursor/coverage record advances only when the full relevant range has been processed for both real modes. Inbound events and their state effects stay with the correctly matched campaign and its mode.

### Matching
- Match using thread ID, exact known contact identities, and `References`/`In-Reply-To`.
- **Never** match by subject or university domain alone.
- A thread mapping to several campaigns → pause all of them for review.
- An unknown participant or new address in a tracked thread → `paused_review(unknown_participant)` until the applicant confirms the relationship.
- A possible reply from a known PI address in a **new** thread → `paused_review(ambiguous_thread)` for applicant confirmation; it never frees capacity automatically (B11).
- In `transport_test`, the actual allowlisted recipient stands in for the PI **only inside that campaign's transport_test mapping**. Test addresses are never promoted into live contact identities (B11).
- Ignore the app's own sent messages by stored Gmail IDs.

### Classification (rule-based heuristics, not proof)
- **Possible delivery report** (`mailer-daemon`, `postmaster`, or `multipart/report; report-type=delivery-status`): `paused_review(delivery_notice)`. The slot is **held**. The applicant checks Gmail and confirms one of: bounce/invalid address (→ `closed: bounce_confirmed`, address invalid), delay/other (resume), or policy rejection (closed, address **not** marked invalid). Do not fetch bodies to decide.
- **Auto-reply** (`Auto-Submitted` ≠ `no`, `X-Autoreply`, `X-Autorespond`, `Precedence: auto_reply|bulk|junk`, or subject patterns like "Out of Office" / "Automatic reply"): `paused_review(auto_reply)`. The slot is held. The applicant sets a resume date or closes. A resolved event is stored and never re-pauses the campaign.
- **Message from the applicant in the thread not sent by the app:** `paused_review(manual_applicant_message)`.
- **Clearly matched non-automatic message from the PI's known identity in the campaign thread:** genuine reply. Stop the sequence, cancel queued and not-yet-generated future messages, set `conversation`, free the slot, and send a toast. An already-claimed attempt is not recalled; it is reconciled, and its budget hold and contact-ledger entry remain until resolved (B13).
- **Anything doubtful:** pause and hold the slot.
- Replies after `sequence_complete` still update the campaign.

### Reply tags (applicant)
Positive / Requests info / No opening / Referral / Contact later (date) / Do not contact.

- **Referral** and **Contact later** create manual `task`s. The applicant enters the referred person's details; the app never claims to extract them.
- **Do not contact** adds the PI to the do-not-contact list. A simulated reply tag in sandbox (or a transport_test tag on a synthetic PI) affects only that mode's scope and never edits the live list (C7).

### Known limit
The final preflight reduces but cannot eliminate the race with a reply arriving just before submission. The atomic claim (§9.6 step 2) is the last check; a genuinely new reply arriving after it cannot stop that submission. The UI and docs state this.

---

## 11. Caps, reservations, and budgets

All counts are per mode, except the real-submission budget and pacing ledger, which `live` and `transport_test` share; `sandbox` has its own (B8, B17, C9).

- **Pending slots** (institution ≤ 2, lab group ≤ 1): a campaign holds a slot while in `outreach_pending` or `paused_review`.
  - **Approving Email 1 atomically reserves** the institution and lab slots and the projected 30-day contact.
  - At dispatch, the gateway **validates the campaign's existing reservation** without double-counting. A campaign occupying the last allowed slot can send.
  - Follow-ups never acquire new slots or count as new contacts.
  - Reservations already owned survive `failed_definite` and `needs_reapproval` of Email 1 until re-approval or explicit cancellation/closure; re-approval validates the existing reservation instead of creating a second one (B4).
- **Rolling 30-day new contacts** (≤ 3 per institution): count successful Email 1 `sent_at` timestamps plus reserved-but-unsent Email 1s (projected). An Email 1 whose attempt is `in_flight` or `uncertain` stays counted. The final check happens at dispatch. Replies or completion never erase history.
- **Rolling 24-hour budget** (≤ 25): counts every real Gmail submission by the app (live + transport_test, including reminders, i.e. Emails 2–4), plus `in_flight` and `uncertain` attempts (held conservatively until resolved). Sandbox sends use a separate logical budget. These limits do not count mail the applicant sends manually and do not replace Google's own limits.
  - **Each attempt counts once (B17):** an in-flight hold converts to sent usage at the established submission time; it is never hold plus usage.
  - Definite non-acceptance releases the hold. Uncertainty keeps it until definite non-submission is supported and recorded (B13).
  - A lowered 24-hour budget can defer follow-ups as well as initials.
- **Concurrency:** reservations happen in transactions so parallel approvals/sends can never oversubscribe.
- **Boundaries:** an event at time `t` counts in a rolling window of length `W` evaluated at `now` iff `now − W < t ≤ now`. An event exactly 24 h (or exactly 30 days) old no longer counts. Held budget entries count regardless of age until resolved. Tested in code.
- **Lowering a limit or merging groups below current occupancy (B6):** keep history, block new reservations, show "over capacity," and never cancel existing conversations or double-count.
  - New limits apply to new initial submissions, **including** initials that are reserved but not yet submitted. They are not silently grandfathered: their approvals and records are kept, but they are marked `over_capacity` and blocked at dispatch.
  - The UI explains that this can need applicant action: cancel an excess unsent campaign, restore the limit, or wait for an already-contacted campaign to release capacity. It does not claim that rolling to another window will resolve it.
  - Example (required test): two reserved but unsent initials under a newly lowered limit of one may both be blocked. Cancelling either releases its reservations and permits the other. There is no hidden permanent lock and no automatic cancellation.
  - Existing follow-ups need no new pending or new-contact slot. They still obey the current 24-hour budget, pacing, windows, approval, and reply checks.
  - A cap-only deferral is never a missed-window/re-approval event (§9.4).
- **Adjustable limits (B16):** defaults are 25 real submissions per rolling 24 hours, 2 pending per institution, 1 per lab, and 3 new contacts per institution per rolling 30 days.
  - There are no hardcoded ceilings. Values must be finite, sensible integers.
  - Any increase above a default needs explicit warning/confirmation and is audited.
  - Pacing stays positive (default 2–6 minutes, minimum ≤ maximum).
  - No setting can disable approval, recipient validation, uncertainty handling, or reply checking. Debug settings are impossible in live mode.
- **Lab conversation warning:** before reserving a slot for a PI in a lab group with a `conversation`, show its status; the applicant must confirm.
- **Optional stricter setting (off by default):** hold the slot 14 days after Email 4.
- Caps apply to this applicant's local records only.

---

## 12. Modes and testing levels

1. **Automated tests:** fake Gmail transport + injected clock. No network.
2. **`sandbox` mode:** the full workflow runs on an isolated dataset with the **same** caps, states, scheduling, approvals, and gateway, using a fake transport. Buttons simulate PI reply / auto-reply / delivery notice / manual applicant message. It does not consume live capacity because its records are isolated, not because checks are disabled.
   - **Simulated time (C6):** sandbox uses an injected simulated clock with an advance-time control, so the real timing and state rules run instead of a separate simplified scheduler.
   - **Canned drafts (D5):** sandbox may use clearly labelled canned synthetic drafts, so practice and tests need no AI spending. They are not validated AI output and are never live-send-ready. No credentials are needed for offline or canned sandbox practice.
3. **`transport_test` mode:** real Gmail, but every To address is validated against an **explicit allowlist** of consenting test addresses (e.g., the applicant and the owner). The intended PI and the actual test recipient are both recorded. Any test banner is part of the approved snapshot. A self-send tests connectivity only; a reply from a **second** controlled address is required to test real reply classification and cancellation.
   - **Allowlist rules (B11):** known real-PI addresses are rejected from the allowlist. Only one transport_test campaign may be active per actual allowlisted recipient. The test address stands in for the PI only inside that campaign's transport_test mapping and is never promoted into a live identity.
   - **Schedule override (B2):** an explicit, approved, snapshot-bound override (§9.1) is the only way to shorten intervals or open the window, and it exists only in transport_test.

**Mode switching:**
- Pauses the worker and requires explicit confirmation. It is an intentional pause, not a missed-window event. Dispatch resumes only when explicitly resumed for the selected mode (B9).
- Nothing crosses modes: no test-approved message, schedule override (transport_test only, §9.1; sandbox uses its simulated clock), or test thread ever becomes live. Switching never changes a stored message's mode.
- Gmail sync and reconciliation continue for inactive-mode real campaigns (§10, B9).
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
- The owner report covers **live** mode only by default (C9).
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
   2. Claude key (credential/model-access validation via metadata lookup, no generation request; D3)
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
   - Reject (with reason), Revoke, Cancel (unsent Email 1), and **Stop this follow-up sequence**. The wording states that a claimed message cannot be recalled and that an uncertain attempt is not cleared by cancelling (B3, B5).
   - Shows the frozen attachment version of a queued message and offers revoke/re-draft after a new CV upload (B19).
6. **Campaigns:** per-PI timeline (message states, due dates, eligible windows, attempts).
7. **Replies & reviews:** inbound events and pause reasons to resolve, with links to open in Gmail. No AI summaries.
8. **Tasks:** referrals, contact-later.
9. **Posted opportunities:** manual entries, deadlines, formal application route.
10. **Settings:**
    - Caps, window, pacing, last-interval mode (increases above defaults need explicit confirmation and are audited; B16).
    - Optional blackouts (off), optional 14-day hold (off).
    - Model, templates, tone.
    - Institution/lab grouping, time-zone overrides.
    - Allowlist (transport_test), mode switch.
    - Owner email and consent.
    - Delete all data.

---

## 17. Packaging, distribution, and guides

- PyInstaller onedir + Inno Setup. Per-user install, no admin. Start menu and desktop shortcuts. Optional Startup entry for the tray (consented in the wizard).
- Bundle pinned dependencies, `tzdata`, templates, specialty config, Alembic migrations, and the **Desktop OAuth client config**. Installed-app client IDs are not confidential mailbox credentials (the config is not a mailbox access token and is never described as one), but keep the config untracked in every repository, public or private, and inject it at build time from a GitHub secret (A4).
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
  2. Fake transport, recipient allowlist, approval snapshot binding, the **single send gateway** with atomic claim, reconciliation, and the uncertain state. **Also the pure safety rules moved forward from P2 (B1):** window/due-date calculations, calendar-month and N-day handling, blackouts, campaign and message transitions with the pause-reason policy, reservation and cap rules (including cap reduction), the 24-hour budget ledger, pacing, the missed-window rule, and mode isolation. Unit tests with a fake transport and an injected clock. One production gateway throughout; no spike-only sender.
  3. **Then, with my explicit go-ahead,** a Windows Gmail spike in `transport_test`: self-send (connectivity); a two-address thread where the second address replies; verify threading, the en-dash subject and `Re:`, Message-ID/`X-Marcelorian-Id` preservation (metadata only), metadata-only fetch, and history sync. A small owner-assisted Windows spike launcher and plain-language runbook are prepared for this gate (not the P5 installer). The operator, Windows build, sender, second consenting test address, and OAuth client configuration are collected on the local test machine at the gate, never assumed and never committed. Synthetic message content only; never the applicant's real CV (A3).

  No real PI contact. Report untested items. Do not declare the 7-day token survival passed until 7+ days have actually elapsed.
- **P1 — Profile and discovery.** Wizard steps through publications; consented CV extraction and manual facts; PubMed, OpenAlex (API key), RePORTER, seed-URL faculty pages (optional ClinicalTrials.gov); identity/affiliation/email/tz verification; evidence; assessment; candidates UI.
- **P2 — Drafting, review, and rules.** Templates, LLM fill + validation, the approval queue, and drafting/review integration on top of the P0 scheduling/caps/reservation/state logic (extended with tests as needed). Sandbox mode end to end. Reuse the P0 gateway; no temporary sender.
- **P3 — Automation.** Tray worker, dispatcher priorities, wiring the P0 window/pacing/missed-window rules into tray automation, restart/sleep/resume recovery, history sync with gap recovery, inbound classification, the reviews and tasks screens, toasts.
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
6. Lowering limits below occupancy blocks new reservations without cancelling anything; reserved-but-unsubmitted initials are marked `over_capacity` (not grandfathered); merged groups don't double-count (B6).
7. An unverified lab suggestion blocks a second same-institution contact until reviewed; the lab conversation warning appears.
8. Cancelling an unsent Email 1 returns the campaign to `candidate`, keeps the message and audit history, and releases its reservations; it is refused while an attempt is in flight or uncertain; revoking a follow-up does not release the slot (B3, B5).

**Approval and snapshot**
9. Changing the mode, sender, recipient, body, or the attachment of that specific message invalidates the approval. Uploading a new CV version does not (B19). Changing a fact or profile field the message uses invalidates it; an unrelated edit does not (B18).
10. The latest CV is never substituted into an approved message.
11. A sandbox or transport_test message never becomes live; switching modes pauses the worker and never changes a stored message's mode (B9).

**Timing**
12. Calendar-month math including Jan 31 and DST transitions, computed in the PI zone from the actual `sent_at`; the N-day alternative uses calendar days in the PI zone with the documented DST handling (C8).
13. Due Tuesday, approved Thursday → sent Thursday; the next interval counts from Thursday.
14. An unknown/ambiguous time zone blocks scheduling.
15. No send starts at or after 11:00 local.
16. Pacing; cap deferral rolls to the next window with no burst.
17. No functioning evaluation during the considerable portion of the assigned window (worker asleep or sync/connectivity unavailable) → `needs_reapproval`; an ordinary restart, pacing/cap deferral, deliberate persistent pause, or mode switch preserves the approval and rolls the window; approval near a window's end is judged only on the remaining portion; persistent pause; cancellation (B7).
18. Optional blackouts are off by default; when on, they defer without rewriting due dates.

**Gateway and recovery**
19. Double-click + scheduler tick at the same moment → exactly one submission.
20. Process dies before submission → the message is reclaimable only via defined recovery, never duplicated.
21. Process dies **after** Gmail accepted → reconciliation finds it → `sent`, with no resend.
22. Reconciliation "not found" or failed search → `uncertain`, budget and slot held, never auto-retried, never forgotten; explicit, audited manual resolution.
23. A definite Gmail rejection → `failed_definite`, budget released.
24. Export failure after a successful send → no resend; stale export indicator.

**Inbound**
25. A reply arriving before the final preflight cancels the send; a reply recorded during preflight prevents submission at the atomic claim; the documented race boundary is noted (B10).
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
40. Explicitly paid vs. explicitly unpaid postings; `Exclude explicitly unpaid opportunities; unknown funding allowed` excludes explicitly unpaid appointments while unknown funding stays eligible and marked (C1).
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

**Added by the pre-P0 review (B-decisions; automated, P0)**
54. Cancellation committed during preflight prevents submission; cancellation after the claim commit is recorded as a stop request, stops future steps, and the attempt is reconciled; a Cancel action never clears an uncertain attempt (B5, B10).
55. A preflight that crosses 11:00 PI local time does not submit; the message stays approved and rolls to the next window without being treated as missed (B10).
56. A reply, revocation, persistent pause, mode switch, or do-not-contact entry recorded during preflight prevents submission at the atomic claim (B10).
57. Concurrent claims (UI approval, scheduler tick, recovery) yield exactly one Gmail call; the unique unresolved-attempt index rejects a second unresolved attempt (B10).
58. Every held-budget transition: hold → used at the established submission time; hold → released on definite rejection; hold retained while uncertain; uncertain → released only on recorded definite non-submission; uncertain → used on confirmed send; each attempt counted exactly once (B13, B17).
59. Cap-reduction recovery: two reserved, unsent initials under a newly lowered limit of one are both blocked as `over_capacity`; cancelling either releases its reservations and permits the other; a lowered 24-hour budget defers follow-ups too; a cap-only deferral is not a missed window (B6, B17).
60. Mode-switch isolation: switching pauses dispatch until explicitly resumed; inactive-mode messages are neither dispatched nor treated as missed; stored modes never change; real-send pacing is not reset (B8, B9).
61. A delayed send result arriving after a genuine reply is recorded without overwriting `conversation`; the reply freed the pending slot, but the attempt, budget, and contact-ledger entries remain (B13).
62. Uncertain-send recovery never restarts a stopped or closed campaign and never clears an unrelated pause reason (B13).
63. A transport-test schedule override is rejected in live (at approval and at the gateway); in transport_test it still obeys pacing and the shared real-send budget (B2, B8).
64. Prior-contact acknowledgment covers only the exact reviewed Gmail IDs; an incomplete search defers dispatch; a new unacknowledged hit pauses again; drafts are excluded (B14).
65. RFC Message-IDs use the verified sender's domain (never the hostname), are unique per attempt, and are unchanged through reconciliation; `X-Marcelorian-Id` is opaque (B12).
66. Production secret storage rejects fail/null/plaintext/unknown keyring backends and never selects the in-memory test store (A3).
67. Settings increases above defaults require confirmation and are audited; non-finite, negative, or non-positive pacing values are rejected (B16).
68. Approval is blocked by an unverified time zone, a non-send-ready recipient, or an unreviewed salutation, and the same prerequisites are rechecked at dispatch (B20).
69. The transport_test allowlist rejects known real-PI addresses and allows only one active campaign per actual test recipient (B11).

**LIVE (Windows 11, transport_test, allowlisted addresses only, explicit authorization)**
- L1: Self-send; a two-address thread with a reply from the second address; verify threading/subject.
- L2: Reconnect flow after revoking the token.
- L3: Token still valid after 7+ days (reported when elapsed).
- L4: Installer install/update/uninstall, with data preserved on update.

---

## 20. Traceability (safety requirement → component → tests)

| Requirement | Enforcing component | Tests |
|---|---|---|
| No duplicate execution | Send gateway atomic claim + unique unresolved-attempt index | 19–22, 54, 57 |
| No blind retry under uncertainty | Gateway reconciliation + `uncertain` state | 21–23, 58, 62 |
| Exact approved content only | Approval snapshot + hash check at claim + dependency invalidation | 9–11, 38, 63, 68 |
| Caps not oversubscribed or self-blocking | Reservation service (transactional) | 3–8, 59 |
| Stop on reply | Sync + matcher + preflight + claim re-read | 25, 29–31, 55, 56, 61 |
| Pacing and windows respected | Claim-time window/due/pacing check + persisted pacing ledger | 15–17, 55, 60, 63 |
| No missed replies after history expiry | Gap reconciliation + `sync_gap` block | 31 |
| Conservative bounce handling | Inbound classifier (metadata) + review | 32 |
| No Gmail content to AI | AI input builder + metadata-only fetch | 33, 37 |
| Claims grounded in evidence | Draft validator (ownership + confirmation) | 34–36, 38 |
| Funding labels truthful | Assessment rules | 39–40 |
| Test traffic never live | Mode partitioning + gateway recipient validation + allowlist rules | 11, 43–44, 60, 63, 69 |
| No duplicate prior contact | Prior-contact search + scoped acknowledgments | 64 |
| Secrets never in plaintext | Secret store backend allowlist | 66 |
| Correct timing | Scheduler (UTC + IANA, tzdata) | 12–18 |
| Report is read-only + consented | Report service (allowlisted file ID, RAW) | 47–48 |
| Audit integrity | Exports (lossless, formula-safe) + backups | 49–51 |
| Local UI protected | Session bootstrap + Host/Origin/CSRF | 52 |

---

## 21. Proposed defaults (not previously decided by the owner; implement as settings)

- **Missed-window recovery:** require re-approval only when the assigned eligible window passed with no functioning dispatch/safety evaluation during the portion in which the message could have been considered (worker or connectivity/sync unavailable); deliberate pause, mode switches, brief restarts, and cap/pacing deferrals do not invalidate approval (§9.4, B7).
- **Blackouts** (federal holidays, Dec 20–Jan 5): available, **off** by default.
- **Sandbox** is the default mode at first run; live requires the readiness checklist.
- **Tone preferences** replace AI writing samples.
- Pilot acceptance targets **Windows 11**.
- **ClinicalTrials.gov** is optional enrichment only.

No other open decisions. If implementation reveals one, stop and ask.
