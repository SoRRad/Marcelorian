# Marcelorian — Decision Record (pre-P0 review)

This file records the owner's resolutions to the pre-P0 review questions
(A1–D5), plus later owner decisions (W1). The resolutions are folded into `MARCELORIAN_SPEC.md`, which stays the
**single specification**. This file explains *why* each patched sentence
changed and where it is enforced. If the two ever disagree, the spec wins and
the disagreement is a bug to report.

- Source: owner response "Marcelorian: owner response to the pre-P0 review" (2026-10-16).
- The original v2 spec is preserved unchanged in Git history (local `main`,
  commit `705e13b`, "Add Marcelorian build specification (v2), unchanged").
- Status meaning:
  - **P0** = implemented and unit-tested in P0.1–P0.2.
  - **Doc** = recorded in the spec now and implemented in the phase shown.
  - **Process** = applies to how the build is run, not to app behavior.

## Decision matrix

| ID | Topic | Resolution (summary) | Spec sections patched | Status / phase | Enforced by / tests |
|---|---|---|---|---|---|
| A1–A2 | Repository bootstrap | Inspect the remote first. Create `main` with the unchanged spec **only if** the remote is confirmed empty **and private**. Otherwise continue locally and report. Never force-push, change visibility or branch protection, or merge. | §0 | Process | Remote found **public** and empty on 2026-10-16, so work stayed in local Git and nothing was pushed. The owner made it private; GitHub reported `private: true` on 2026-10-16 17:06 UTC, and only then were `main` (unchanged spec) and the feature branch pushed, without force. The owner merged PRs #1 and #2 on 2026-10-17. On 2026-10-17 the owner made the repository public again and said that is acceptable until it is fully ready, then to continue as before. |
| A3 | Linux development ≠ Windows acceptance | Cross-platform core is developed on Linux. Windows adapters sit behind explicit interfaces. Tests may inject an in-memory secret store that production code can never discover or select. No plaintext fallback. No live credentials in development or CI. Windows CI results are reported separately from interactive desktop behavior (OAuth, tray, toast, sleep/resume, Credential Manager). The runbook, allowlist checklist and redacted report template are prepared for the P0.3 gate. **Deferral (recorded choice):** the spike launcher code and the real Gmail adapter it drives are written only after the separate P0.3 approval, so nothing that can send real email exists before it (spec §18 aligned). Operator details are collected on the test machine, never committed. | §0, §2, §12, §18 | P0 (interfaces, store) | `platform/secrets.py`, `tests/fakes/secrets.py`; `tests/security/test_platform_security.py` |
| A4 | CI and costs | Ubuntu + Windows test jobs only. No applicant secrets, no live calls, least-privilege `permissions`. Job timeouts and cancellation of superseded runs. Recheck billing docs before running. Never enable overages or change budgets. Desktop OAuth client config stays untracked, and it is **not** a mailbox token. Installer/release builds remain P5. | §2, §17 | P0 (workflow file only) | `.github/workflows/ci.yml` (supplied, not run: see handoff) |
| B1 | Pure safety rules move into P0 | Window/due-date math, calendar months, campaign/message transitions, reservations, and budget rules are built and tested in P0.1–P0.2 with a fake transport and an injected clock. P2 keeps drafting/review integration. P3 keeps tray automation. One production gateway throughout. | §0, §18 | P0 | `domain/*`, `services/*`; `tests/unit/`, `tests/service/` |
| B2 | Transport-test schedule override | Allowed **only** in `transport_test`, only after P0.3 authorization. The approved snapshot visibly binds the override, actual allowlisted recipient, fake PI identity, and shortened timing. Gateway, approval, exact-recipient checks, uncertainty handling, the shared real-send budget, and pacing still apply. Live rejects any override. No global "always open" flag. | §9.1, §9.4, §12 | P0 | `domain/snapshot.py`, `services/gateway.py`; `tests/service/test_modes.py`, `tests/unit/test_domain_pure.py` |
| B3 | Cancel before first send | An unsent Email 1 campaign returns to `candidate`. The logical message and audit history are kept. Capacity and projected-contact reservations are released. Re-drafting creates a new revision that needs fresh approval. Only allowed with no sent message and no in-flight or uncertain attempt. A missing `sent_at` is not proof of non-submission. Never resets uncertainty or bypasses reconciliation, do-not-contact, or prior-contact checks. | §3, §9.1, §11, §19 (8) | P0 | `services/approvals.py`; `tests/service/test_approvals.py`, `tests/service/test_reservations.py` |
| B4 | Reservations after failure or re-approval | A campaign that legitimately owns reservations keeps them through `failed_definite` and `needs_reapproval`, until re-approval or explicit cancellation/closure. The UI shows that capacity remains reserved. Moving into review never creates a reservation. | §3, §11 | P0 | `services/reservations.py`; `tests/service/test_reservations.py` |
| B5 | Follow-up revoke and stop | **Revoke**: approved, unclaimed follow-up → `awaiting_approval`; slot kept. **"Stop this follow-up sequence"**: no further generation/dispatch; slot kept until the applicant closes with a reason or deliberately re-drafts the next unsent step. A sent step is never re-drafted. Cancelling a claimed message is not a recall: future steps stop and the attempt is reconciled. A Cancel button never clears an uncertain attempt. | §3, §9.1, §16 | P0 (service); UI wording P2 | `services/approvals.py`; `tests/service/test_approvals.py`, `tests/service/test_gateway_race.py` |
| B6 | Lowering caps | New limits apply to new initial submissions, including already-reserved unsent initials. These are marked `over_capacity` (approvals and records kept). This may need applicant action: cancel an excess campaign, restore the limit, or wait for a contacted campaign to release capacity. Rolling windows are never promised to resolve it. Cancelling either of two blocked initials releases its reservations and unblocks the other. No hidden lock, no automatic cancellation. Follow-ups need no new slot but still obey the 24 h budget, pacing, windows, approval, and reply checks. A cap-only deferral is not by itself a missed window (a window whose only evaluations ran without a current sync still goes to review, §9.4). | §11, §19 (6) | P0 | `services/reservations.py`, `services/gateway.py`; `tests/service/test_cap_reduction.py` |
| B7 | Missed-window policy | **Superseded by S7 (2026-10-19): an unsent window now rolls with no re-approval; the text below is the original policy.** Re-approval is required when an assigned eligible window passes with **no functioning dispatch/safety evaluation** during the portion in which the approved message could have been considered, because the worker or required connectivity/sync was unavailable. Evaluations and their deferral reasons are recorded; a process heartbeat is not proof of Gmail coverage. Approval near a window's end counts only the remaining portion. Missing recovery evidence → review. Brief restarts, pacing/cap deferrals, deliberate persistent pause, and deliberate mode switches do not by themselves invalidate approval. | §9.4, §19 (17), §21 | P0 | `services/scheduling.py` (`apply_missed_window_rule`); `tests/service/test_missed_window.py` |
| B8 | Pacing | Spacing is serialized and persisted across actual Gmail submissions in live **and** transport_test, regardless of PI time zone. Sandbox has separate simulated pacing. Restarts and mode switches do not reset spacing. A test-window override does not bypass pacing. | §9.4, §11 | P0 | `services/pacing.py`, `pacing_state` table; `tests/service/test_gateway.py` (pacing), `tests/service/test_modes.py` (shared ledger); `tests/service/repair/test_m01_*.py` (pacing at actual submission) |
| B9 | Mode behavior | One active dispatch/generation mode. Gmail sync and reconciliation continue for all real campaigns of the connected mailbox, including inactive-mode ones. A mailbox-level cursor/coverage record is valid only after both real modes' full range is processed. A mode switch is an intentional pause, not a missed window. Dispatch resumes explicitly. Switching never changes a stored message's mode. | §3, §9.4, §12 | P0 (mode/pause state, gateway checks); sync P3 | `services/runtime.py`; `tests/service/test_modes.py` |
| B10 | Current sync and final authorization | An incremental sync runs right before sending. A global unresolved gap blocks all real sends. A thread-specific problem pauses only that campaign; mailbox/credential failures are never mislabeled thread-specific. After network preflight, the atomic claim re-reads message, approval, campaign stop/review status, active mode, persistent pause, do-not-contact, reservations, and budget. Due time, window, pause/cancellation, and pacing are rechecked immediately before Gmail is invoked; if preflight crosses the window end in PI local time (16:00 by default; W1), nothing is submitted. The cancel/submit hand-off is serialized. Recall is not promised after the claim commits. No DB transaction is held across network calls. | §9.6, §10 (Known limit), §19 (25) | P0 | `services/gateway.py`; `tests/service/test_gateway_race.py`; `tests/service/repair/test_m01_*.py` (bounded hand-off, note 28) |
| B11 | New-thread replies, test contacts | A possible reply from a known PI address in a **new** thread pauses for confirmation (`ambiguous_thread`) instead of freeing capacity. A test address stands in for the PI only inside its transport_test mapping. One active transport_test campaign per allowlisted recipient. Known real-PI addresses are rejected from the allowlist. Test addresses are never promoted into live identities. | §10, §12 | P0 (allowlist, mapping, uniqueness); classifier P3 | `services/recipients.py`, `domain/recipients.py`, `domain/mime.py`; `tests/unit/test_domain_pure.py`, `tests/service/test_modes.py`, `tests/service/test_review_round2.py` |
| B12 | Message identifiers | RFC Message-ID = unique opaque value + the verified sender's email domain, never the hostname. A distinct value per authorized attempt, kept through that attempt's reconciliation and never regenerated after an empty search. `X-Marcelorian-Id` is kept: opaque value, no personal data, visible correlation header (not a secret). It is added to the metadata-header allowlist for the P0.3 preservation test. Message-ID is evidence, not a provider idempotency guarantee. | §9.6, §10 | P0 | `domain/identifiers.py`, `domain/gmail_params.py`; `tests/unit/test_domain_pure.py`, `tests/service/test_gateway.py`; `tests/service/repair/test_m06_*.py` (provider-observed Message-ID, note 31) |
| B13 | Resolving uncertainty | Budget is released only when definite non-submission is supported and recorded, never because a Sent search is empty. A fresh approval is required before another attempt. Recovery never forces the campaign to `outreach_pending` or restarts a stopped sequence. A late successful result never overwrites a newer conversation/closed state. A genuine reply frees the pending slot but not an unresolved submission/contact ledger entry. One resolved pause reason never clears another. The established actual submission time drives accounting and intervals; reconciliation time and provenance are kept too; uncertain timestamp evidence → review. | §3, §9.6, §11 | P0 | `domain/transitions.py`, `services/campaigns.py`, `services/reconciliation.py`, `services/send_results.py`; `tests/service/test_recovery.py`; `tests/service/repair/test_m01_*.py`, `test_r3_recovery_snapshot_containment.py` |
| B14 | Prior-contact review | Options: "acknowledge these reviewed messages and proceed" or "close". Acknowledgment is scoped to mailbox + campaign + exact Gmail IDs. It never ignores future mail, bypasses do-not-contact, or permits a duplicate sequence. Search uses exact known addresses, full pagination, spam/trash included, drafts excluded. Hits show minimal metadata + "Open in Gmail"; no bodies or snippets. An incomplete search is not a clean result. New unacknowledged hits trigger review. The app's own attempts are reconciled first. | §9.6 | P0 (service, query); UI P2/P3 | `services/prior_contact.py`; `tests/service/test_inbound_and_contact.py`, `tests/service/test_review_round2.py`; `tests/service/repair/test_m03_*.py` (overlapping acknowledgments, note 27) |
| B15 | Email format | Plain-text body, no tracking. Applicant-approved professional URLs (e.g., ORCID) are allowed. Email 2 says "my earlier email" (not "my note below"). No automatic recursive quoting in v1. CV attachments required on Emails 1 and 4. | §8.1, §9.7 | P0 (plain-text MIME); templates P2 | `domain/mime.py`; `tests/unit/test_domain_pure.py` |
| B16 | Adjustable caps | No hardcoded ceilings. Defaults: 25 real submissions/rolling 24 h, 2 pending per institution, 1 per lab, 3 new contacts per institution/rolling 30 days. Values must be finite and sensible. Increases above defaults need explicit confirmation and are audited. Pacing stays positive (default 2–6 min). No control can disable approval, recipient validation, uncertainty handling, or reply checks. Debug settings are impossible in live. | §1, §11, §16 | P0 | `config/settings.py`; `tests/service/test_settings.py` |
| B17 | Budget meaning | "Reminders" = Emails 2–4. Live + transport_test share the real-submission ledger; sandbox never consumes it. Each attempt counts once (hold converts to usage). Definite non-acceptance releases the hold; uncertainty keeps it until resolved. A lowered 24 h budget can block follow-ups too. | §11 | P0 | `services/budget.py`; `tests/service/test_recovery.py` (held-budget transitions, 24 h boundary) |
| B18 | Profile edits | Changing a fact, signature field, role phrase, or other value used by an unsent approved message → re-approval. Unrelated edits do not. Per-dependency versions are recorded, not a global profile version check. Final contact/timezone checks also apply at dispatch. | §4, §8.3, §9.1 | P0 | `approval_dependency` table, `services/approvals.py`; `tests/service/test_approvals.py`; `tests/service/repair/test_m05_*.py` (withdrawal, note 29), `test_m02_*.py` (sender, note 32) |
| B19 | CV versions | A new CV upload never changes or invalidates an approved frozen attachment. The UI shows the queued version and offers revoke/re-draft. Replacing the attachment **of that message** invalidates its approval. Acceptance test 9 corrected. | §9.1, §19 (9, 10) | P0 | `services/approvals.py`; `tests/service/test_approvals.py` |
| B20 | Approval prerequisites | Approval requires a verified time zone, a send-ready exact recipient, and a reviewed salutation. Role/affiliation uncertainty and eligibility/funding flags are surfaced honestly. Mutable safety/contact prerequisites are rechecked at dispatch. | §6, §9.1, §9.6 | P0 | `services/approvals.py`, `services/gateway.py`; `tests/service/test_approvals.py` |
| C1 | Funding preference | Unknown funding stays eligible and marked. The preference formerly called "paid only" is renamed **"Exclude explicitly unpaid opportunities; unknown funding allowed."** Pilot default stays "paid preferred, unpaid acceptable". Neither a grant nor a listing without stated compensation makes an appointment explicitly paid. | §4, §7, §19 (40) | Doc (P1/P2) | — |
| C2 | Manual facts | Manual facts use an explicit "Save and confirm" action, with provenance `applicant_entered`. Autosave never confirms proposed facts. AI-extracted and imported facts require applicant confirmation. Self-reported facts are never labelled externally verified. | §4 | Doc (P1); provenance column exists in P0 schema | — |
| C3 | Names and availability | Email 1's introduction uses the full name. Closings of Emails 2–4 use the explicit signature-name preference, defaulting to the full name (never an automatic informal first name). Availability shows Month YYYY; duration only when confirmed. Never invent a date or duration. | §8.1, §8.2 | Doc (P2) | — |
| C4 | Applicant edits and evidence | Evidence links stay only on claims they still support. Edited claims are marked applicant-written/review-required until confirmed. Deterministic checks rerun. The edited email is never sent back to the AI. Whole-body editing cannot change recipient or subject. | §8.2, §8.3, §16 | Doc (P2) | — |
| C5 | Templates | Subject fixed from specialty config (exact en dash, normalized `Re:` chain). Body templates editable. CV required on Emails 1 and 4. Every attachment frozen in the snapshot. | §8.1, §16 | P0 (subject rules); templates P2 | `domain/subjects.py`; `tests/unit/test_domain_pure.py` |
| C6 | Sandbox time | Injected simulated clock with an advance-time control. The real timing/state rules run, not a simplified scheduler. Shortened intervals in transport_test only as an explicit approved override. | §12 | P0 (clock seam); UI P2 | `clock.py` |
| C7 | Do-not-contact | Matches verified PI identity and aliases, confirmed merged records, exact known addresses, and reliable ORCID/OpenAlex associations. Name + institution alone → review warning only. Merges keep restrictions and history. Simulated DNC events never edit the live DNC list. | §3, §4, §10 | P0 (check + scoping); UI P1/P3 | `services/dnc.py` (`identity_closure`); `tests/service/test_inbound_and_contact.py`, `tests/service/test_review_round2.py` |
| C8 | N-day final interval | The N-day alternative uses calendar days in the PI's time zone, with documented DST handling. Emails 2 and 3 stay 7×24 h. Calendar-month clamping unchanged. | §1, §9.3 | P0 | `domain/timing.py`; `tests/unit/test_timing.py` |
| C9 | Shared vs isolated mode data | Real profile/PI/evidence/assessment records may be reused across modes. Edits to shared data are visible and follow re-approval rules. Synthetic identities and simulated state cannot corrupt live records or be promoted. Campaigns, approvals, logical messages, thread mappings, institution/lab capacity, projected contacts, exports, and reports are mode-specific. **Exceptions:** mailbox synchronization and the real Gmail submission/pacing ledger are shared by live and transport_test. The owner report is live-only by default. | §3, §11, §12, §13 | P0 (partitioning) | `tests/service/test_modes.py` |
| D1 | Early follow-up drafting | Deferred. The next step is generated when due, as specified. | — | Not adopted | — |
| D2 | 14-day approval expiry | Deferred. v1 uses only the missed-window and content-change rules. | — | Not adopted | — |
| D3 | Claude key validation | Later local setup may do an authenticated model-metadata lookup with no generation request. It is labelled credential/model-access validation, not proof of billing, budget, or generation. P0 makes no live provider calls. | §2, §16 | Doc (P1) | — |
| D4 | Tooling | ruff, strict typing for safety-critical modules, property-based tests for timing and cap boundaries. Otherwise proportionate. | §2 | P0 | `pyproject.toml` (ruff, mypy); `hypothesis` tests in `tests/unit/test_timing.py` (timing) and `tests/service/test_handoff_coverage.py` (24 h / 30-day boundaries) |
| W1 | Send window adjustability | Owner answer to note 7 (2026-10-16): the window may be **widened** as well as narrowed, anywhere within **Mon–Fri 08:00–16:00** PI local time. The default was Tue–Thu 08:00–11:00 until the owner's answer of 2026-10-17: **the default is now Mon–Fri 08:00–16:00**, the outer bound itself. The window can only be narrowed, and re-widening a narrowed window stays within the default, so it needs no confirmation (the slot-based widening check remains for any future narrower default); every change is audited. Tests of window-edge mechanics pin the old narrow window explicitly (`World.narrow_window`, `@pytest.mark.narrow_window`). The window end replaces the fixed 11:00 limit in every check (claim, preflight crossing, missed-window portion). | §1, §9.4, §9.6, §16, §19 (15, 55) | P0 | `config/settings.py` (`WINDOW_OUTER_*`); `tests/service/test_settings.py`, `tests/service/test_gateway.py`, `tests/service/test_gateway_race.py` |
| D5 | Canned sandbox drafts | Clearly labelled canned synthetic drafts in sandbox, so practice and tests need no AI spend. They are not validated AI output or live-send-ready. No credentials needed for offline work. | §12 | Doc (P2) | — |
| S1 | Applicant-triggered sending | Owner answer (2026-10-19): "I want to press the send button myself." Nothing is sent without a click at that moment; the tray never dispatches on its own. Approval locks the text; the send button unlocks with a countdown when window, due time, pacing and caps allow, and every check runs at the click. Supersedes the auto-dispatch in the §1 Sending row. | §1, §9.2, §9.8 | P0 (rules, readiness); UI P2 | `services/handoff.py` (`readiness`); `tests/service/test_handoff.py` |
| S2 | Mailbox channels | Owner answers (2026-10-19): Outlook primary, Gmail secondary, switchable at setup. The university Outlook cannot get IT approval for an API app, so Outlook is a **hand-off channel** with no mailbox access. The M02 identity rule covers both channels (switching only before real-mode history). | §1, §9.8, §15 | P0 | `services/approvals.py` (`connect_handoff_mailbox`); `tests/service/test_handoff.py` |
| S3 | Hand-off methods | Owner tested on the work computer (2026-10-19) that both an Outlook-on-the-web compose link and an `X-Unsent` `.eml` file open as editable new messages; the owner chose to have both offered each time, plus copy buttons. | §9.8 | P0 (builders); UI P2 | `domain/handoff.py`; `tests/unit/test_handoff_package.py` |
| S4 | Hand-off tracking | Owner answer (2026-10-19): manual clicks. The applicant records **Sent** / **Not sent**, and before each hand-off confirms no prior contact (Email 1) or no reply (follow-ups); a PI reply can be reported any time. No Bcc copies to a personal mailbox (work-mail policy). | §9.8 | P0 | `services/handoff.py` |
| S5 | Open hand-offs | Owner answer (2026-10-19, asked about drafts): three at a time. At most three hand-offs may be unresolved at once. | §9.8 | P0 | `services/handoff.py` (`MAX_OPEN_HANDOFFS`) |
| S6 | Edits in Outlook | Owner answer (2026-10-19): small edits allowed. The approved snapshot stays the record; the Sent confirmation can note that edits were made. | §9.8 | P0 | `services/handoff.py` |
| S7 | Missed windows | Owner answer (2026-10-19): an approved message whose window passes unsent "waits, with no need for re-approval". Since S1 nothing is sent without a click, so the B7 evidence rule (re-approval when no functioning evaluation) no longer protects anything: every unsent window rolls to the next permitted window under the current policy. Only a snapshot failing its hash or a policy with no window invalidates the approval (per message). Notes 3, 16 and 24 describe the superseded B7 mechanics; evaluations are still recorded as an audit. | §9.4, §19 (17, 72) | P0 | `services/scheduling.py` (`apply_missed_window_rule`); `tests/service/test_missed_window.py` |
| S8 | Signature at first use | Owner answer (2026-10-19): no default signature; an option to add it when first using the app. The first hand-off waits for the signature step (`set_signature`: save one, or pass "" to skip); it can be changed later in Settings. | §9.8, §19 (87) | P0 (rule); UI P2 | `config/settings.py` (`set_signature`); `tests/service/test_handoff.py` |
| R1 | Reopening a negative finding | Owner answer (2026-10-19): allow reopen. A negative finding (fellow/trainee, left, not faculty, retired, wrong person) is never lifted automatically. When a later seed changes the record, it is flagged "new evidence since your finding" and enters the queue. One click reopens it to "not checked"; the old finding stays in the audit history. | docs/SEED_IMPORT.md | Research catalog | `services/research_seed.py` (`reopen`, `queue`); `tests/service/test_research_queue.py` |
| R2 | Verification queue order | Owner answer (2026-10-19): most sendable first. Tier 1 is faculty whose role and email were both seen on an official source; tier 2 is partial evidence; tier 3 is opportunities and program routes; tier 4 is claims only. Within a tier, flagged records come first, then seed priority band, then institution. Out-of-scope records and live do-not-contact matches are never queued. | docs/SEED_IMPORT.md | Research catalog | `domain/research.py` (`queue_tier`); `tests/unit/test_research_confidence.py` |
| R3 | Confirmation expiry | Owner answer (2026-10-19): 90 days. After that a "confirmed current" check counts as "official source seen" and returns to the queue to be confirmed again. Confirming requires saying where it was checked. | docs/SEED_IMPORT.md | Research catalog | `domain/research.py` (`CONFIRMATION_VALID_DAYS`); migration 0004 |
| R4 | Queue screen | Owner answer (2026-10-19): one record at a time, with source links, quick finding buttons, Skip and progress ("3 of 212"); saving moves to the next record. | docs/SEED_IMPORT.md | Research catalog | `ui/templates/research_queue.html`; `tests/security/test_research_queue_ui.py` |
| N1 | Same place | Owner answer (2026-10-19): no two applications to the same place at once. Default place = institution (institution group; group or split institutions to adjust), with a switch to department or lab. A narrower scope needs confirmation. A missing department counts as the whole institution. Supersedes the spec §11 lab-conversation confirmation (now a block). | §11, §16 | P0 rule; UI dashboard | `services/reservations.py` (`same_place_conflicts`), `config/settings.py` (`same_place_scope`), migration 0005 (`pi.department`); `tests/service/test_same_place.py` |
| N2 | When a place frees | Owner answer (2026-10-19): after a rejection ("no opening") or any other close, or 14 days after the last unanswered follow-up. A reply or open conversation keeps the place in use. | §11 | P0 rule | `services/reservations.py` (`occupies_place`); `tests/service/test_same_place.py` |
| N3 | Outreach status dashboard | Owner request (2026-10-19): contacted, responses and rate, pending, rejected, no reply, approved-not-sent, needs attention, places in use, verification progress. | §16 | Dashboard | `services/status.py`, `ui/templates/dashboard.html`; `tests/service/test_outreach_status.py` |
| N4 | Queue speed-ups | Owner request (2026-10-19): keyboard shortcuts (1–6 findings, S skip, P previous, O open source, R reopen; paused while typing), the next record from the same source page (else institution) after a finding, progress figures. | docs/SEED_IMPORT.md | Research catalog | `ui/static/app.js`, `services/research_seed.py` (`same_page_next`, `progress`); `tests/security/test_research_queue_ui.py` |
| N5 | Test package | Owner answer (2026-10-19): package only when everything is finished, as a no-install Windows zip from a GitHub release link, with a simple non-technical walkthrough. Not built yet. | §18 (P5) | P5 | — |
| U1 | Lead sources | Owner answer (2026-10-19): seed files stay the main source of leads; free read-only PubMed, OpenAlex and NIH RePORTER lookups enrich a lead (recent papers, ORCID/OpenAlex IDs, active NEI grants) and import the applicant's publications, run only on the applicant's click. No faculty-page crawling in the pilot. | §6, §18 (P1) | P1 | (P1 lookups) |
| U2 | AI help | Owner answer (2026-10-19): AI is optional. Everything works without it (templates plus applicant-written personal lines); with an Anthropic key, Claude may suggest the personal lines and CV facts with evidence for review. Practice (sandbox) uses canned drafts at no cost. | §4, §8, §16 | P2 | (P2 drafting) |
| U3 | Channels | Owner answer (2026-10-19): Outlook hand-off only for the pilot; the applicant sends from Outlook and marks Sent / Replied / Rejected in the app. The real Gmail adapter, history sync and Gmail setup are deferred (P0.3 Gmail gate unchanged). | §9.8, §10, §16, §18 (P3) | Pilot scope | — |
| U4 | Owner monitoring | Owner answer (2026-10-19): an always-current, Excel-friendly export (CSV plus a status summary) the tester can send to the owner, instead of a shared Google Sheet. | §13, §16 | P4 | (P4 exports) |

## P0 implementation notes (behavior chosen inside the decisions, documented here)

These notes give concrete code-level meaning to the resolutions above. Each one
narrows behavior in the conservative direction, except note 7, which records the owner's
widening decision W1. None of them changes a decision.

1. **Rolling-window boundary.** An event at time `t` counts in a rolling window
   of length `W` evaluated at `now` iff `t > now - W`. An event exactly 24 h (or
   30 days) old no longer counts. There is no upper bound, so a send time that
   Gmail reports slightly ahead of the local clock counts at once. Held
   (in-flight/uncertain) budget entries count regardless of age until resolved.
2. **Point of no recall.** The atomic claim transaction is the final
   authorization. It re-reads everything listed in B10, checks window/due/
   pacing, records the attempt as `in_flight`, and commits. Only an in-memory
   call separates that commit from `messages.send`; the MIME is built from the
   snapshot before the claim. Cancellation committed before the claim prevents
   submission. Cancellation after it is recorded as a stop request and the
   attempt is reconciled, unless the gateway never invoked Gmail (note 28), in
   which case the attempt is recorded not submitted and the cancel takes effect.
   The point of no recall stays the claim commit; the hand-off after it is
   bounded (note 28).
3. **Missed-window evidence.** Dispatch evaluations are stored per message
   with `functioning` set only when the worker ran with valid authorization and
   a current sync: Gmail coverage at most 15 minutes old (three 5-minute polling
   cycles) at the evaluation's start, with no open gap. (The owner delegated
   this value on 2026-10-16; `CURRENT_SYNC_TOLERANCE` in `services/scheduling.py`.) This is decided from
   the recorded worker state, not from the deferral reason, so a local cap or
   pacing deferral while coverage is stale is not consideration. Deferrals
   meaning the check itself could not run (sync, auth, search or transport
   unavailable) are never functioning; `own_attempt_unresolved` (a deliberate
   hold by a healthy worker) is. Each evaluation is timestamped when that dispatch attempt
   *began*, so a preflight that crosses the window end (16:00 by default; W1)
   still counts as considered inside the window. Deliberate pause and
   inactive-mode intervals come from the runtime-state log. Deferrals caused by
   those deliberate states never count as proof of consideration. Which part of
   a window counts, and how each passed window is judged, is defined in note 16;
   if that portion is empty, the message simply rolls to the next window.
4. **Sandbox sender.** Sandbox snapshots use the fixed sender
   `sandbox-sender@marcelorian.invalid`; sandbox never needs a Gmail connection.
5. **Local UI session.** The bootstrap token travels in the URL fragment, so it
   never reaches server logs. A static page POSTs it to `/bootstrap/exchange`,
   which is the only state change, so no GET changes state. Sessions are
   held in memory and end when the app process exits.
6. **Schema scope.** P0 creates the sending, identity, and audit tables.
   Discovery-only tables (`evidence`, `grant_evidence`, `opportunity`,
   `assessment`, `task`) arrive with their phases via new Alembic migrations.
7. **Window settings.** Originally resolved as "narrow only". The owner
   answered on 2026-10-16 (decision W1): the window may be adjusted anywhere
   within Mon–Fri 08:00–16:00 PI local time. On 2026-10-17 the owner made
   that range the default too (W1).
8. **Transport-test self-send.** When the transport_test recipient is the
   connected sender itself, the external prior-contact search and the
   new-thread reply search are skipped, because the sender's own address
   matches the whole mailbox. The own-attempt check and the thread check still
   run (spec §12: a self-send tests connectivity only).
9. **Duplicate outreach at dispatch.** Email 1 re-runs the duplicate-outreach
   check at the local check and at the atomic claim, over the identity closure
   (note 20). Another same-mode campaign counts when it is active, has a sent
   message, or has an attempt that was or may have been submitted (sent,
   in flight or uncertain) to any address of that identity. If two queued initials become one
   person through a merge, both are blocked until the applicant cancels one
   and re-approves the other.
10. **Prior-contact exclusions.** Only this campaign's own messages are
    excluded from prior-contact hits. Messages the app sent for other campaigns
    are prior contact. Unresolved attempts are counted only within the same
    mailbox (live + transport_test, or sandbox). In live and sandbox, a hit
    that is the app's own message for another campaign blocks as
    `duplicate_outreach` and is never offered for acknowledgment. In
    transport_test the address is a reusable allowlisted tester, so earlier
    test mail there stays an ordinary, reviewable hit.
11. **A cancel recorded during submission.** It takes effect once
    non-submission is definite (definite rejection, or recorded non-submission
    of an uncertain attempt, or the gateway's own record that it never invoked
    Gmail, note 28). An Email 1 then follows the B3 path (campaign
    back to `candidate`, reservations released); a follow-up is cancelled.
    Approval is refused while such a request is pending; a deliberate re-draft
    clears it.
12. **Next step after a stopped sequence.** After a send, the next step's due
    time is recorded whenever the campaign still holds outreach, even if the
    sequence was stopped. `sequence_stopped` blocks automatic generation (P3)
    and dispatch, and keeps the B5 exit (deliberate re-draft) reachable.
13. **Drafted dependency versions.** A draft records the fact versions and the
    PI version (salutation) its text was written from. Approval is refused
    with `dependency_changed_since_draft` if any of them changed, so the
    applicant re-drafts first.
14. **Confirmed-sent resolution.** Resolving an uncertain attempt as sent
    requires its Gmail message and thread IDs, so follow-ups can thread on the
    real conversation. Those IDs are verified by a metadata read before the
    next step can be approved (note 31).
15. **Settings sanity.** The N-day final interval is limited to 1–365 days.
    Blackout settings that leave no send day for 365 days or more are rejected.
    The check scans a full 28-year calendar cycle plus one year (weekday/date
    alignment and every blackout rule repeat on it), counts the runs at both
    ends, and treats fewer than two send days as invalid. Approval still maps
    a scheduling failure to `ApprovalError(no_permitted_window)`.
16. **Missed-window portion.** The considered portion starts at the latest of
    the window start, the approval time and the due time. It contains only
    instants that the *current* window policy permits, which honors blackouts
    and narrowed hours added after approval. Every window that has passed is
    judged in turn (the assigned one, then each next eligible window up to
    now); a window considered by a functioning worker rolls the message on, and
    the first one that passed unattended sends it to review. A later window on
    the same day (a widened policy) is judged only from where the previous one
    ended, so no evaluation counts twice. Deferrals for `outside_window`,
    `not_due`, `paused` and `mode_inactive` are never treated as consideration.
    A mailbox-level sync gap is checked before any cap or pacing deferral.
17. **Reconnect clears the auth block.** A verified reconnection of the bound
    identity, or a permitted setup-only switch (note 32), with both required
    scopes clears the global Gmail auth block. A connection missing a scope
    keeps it. A refused switch changes nothing, the block included.
18. **Changes during preflight.** If the message was claimed, revoked or
    re-approved by another caller during the network preflight, the result is
    `changed_during_preflight` and nothing about the message is altered.
19. **Canonical addresses.** Every address (PI emails, allowlist, do-not-contact
    entries, snapshot recipients) is one strict, lower-cased RFC 5322 addr-spec:
    a dot-atom local part without `=`, `?`, `%` or `/`, an ASCII host name, at
    most 254 characters. A pasted `mailto:` prefix is removed first; a
    percent-encoded or `mailto://` remainder, display names, group syntax,
    quoted strings, comments, domain literals and non-ASCII are rejected.
    Non-ASCII is rejected *before* case folding, because `str.lower()` maps some
    non-ASCII letters (U+212A KELVIN SIGN) to ASCII. `build_mime` re-parses the rendered `To`/`From` headers and
    refuses to build unless each holds exactly the approved mailbox.
20. **Identity closure.** Do-not-contact matching, duplicate outreach and the
    prior-contact/reply search addresses use one identity: the closure over
    confirmed merges, exact known addresses (any confidence) and ORCID/OpenAlex.
    Entries recorded by address or identifier resolve to every record carrying
    them. Name + institution never joins an identity (review warning only).
    Identifiers are canonical on write (note 25), so URL and bare forms of the
    same ORCID iD or OpenAlex author ID are one identity.
21. **Metadata-only guard.** `assert_metadata_only` is a positive allowlist:
    get requests need `format=metadata`, a non-empty header allowlist subset
    and exactly the fixed fields mask; list requests need exactly the ids-only
    mask; unknown parameters are refused.
22. **Cookie parsing.** The session cookie is parsed one pair at a time, and
    every same-named value is tried, so a malformed or decoy cookie set by
    another page on 127.0.0.1 cannot lock the applicant out (Pause included).
23. **Log redaction.** A redacting formatter redacts the complete output,
    including tracebacks and stack info; the filter never raises into the
    caller, even when the message object itself cannot be rendered. Every
    pattern is linear on hostile input (matches start only at the beginning of
    a character run; runs are possessive), so a long hyphenated URL cannot stall
    logging. Percent-escapes of address characters (`%2B %2D %2E %27 %5F %40`)
    are decoded before masking so URL-encoded plus addresses are masked whole;
    nothing else is decoded. uvicorn's loggers are routed through the redacting
    handlers, which holds only if the server is started with
    `UVICORN_LOG_KWARGS` (`log_config=None`, no access log); the P0.3/P5
    launcher must pass them.
24. **Missed-window containment.** A snapshot that fails its hash check during
    the missed-window rule sends only that message to review
    (`snapshot_invalid`); the rule continues for the others.
25. **Canonical scholarly identifiers.** `Pi.orcid`/`DoNotContact.orcid` are
    stored as `0000-0000-0000-000X` (URL prefix, spaces and missing hyphens
    removed, check character upper-cased, ISO 7064 MOD 11-2 verified);
    `openalex_id` as the author ID `A…` (URL prefixes removed; work,
    institution and other entity IDs refused). Malformed values are rejected,
    never stored; the future do-not-contact UI must show the rejection and let
    the request be recorded by PI record or address instead.
26. **Window widening is judged by slots.** A window change needs explicit
    confirmation when any (weekday, time) it allows is allowed by neither the
    default nor the previous window, not when a single key exceeds both. The
    windows are half-open, so touching intervals cover each other.

### Owner P0 repair review (M01–M06, 2026-10-17)

The owner's review of snapshot `4d3a16b` reported six findings. Each was
reproduced with a failing test (fake Gmail, synthetic data, injected clock)
before the fix; the tests live in `tests/service/repair/`. The review file
itself was not available in this session, so the reproductions follow the
owner's written description of each finding.

27. **Overlapping prior-contact acknowledgments (M03).** Two open reviews for
    one campaign may list overlapping exact Gmail IDs (two callers racing the
    Email 1 preflight). Acknowledging a review reuses an existing
    acknowledgment of the same mailbox + campaign + Gmail ID (its first time
    and actor are kept), inserts only new IDs, and never resolves another open
    review or acknowledges by sender. The audit row names the review, the new
    IDs and the IDs already acknowledged. The read-then-insert runs inside the
    caller's BEGIN IMMEDIATE transaction; the unique key is unchanged.
28. **Bounded claim hand-off (M01).** The claim takes the write lock before
    reading the clock and computes `not_after` = the earliest of the assigned
    window end, the current policy window's end (not for an approved
    always-open test override) and `claim + SUBMIT_HANDOFF_LIMIT` (5 s, an
    implementation value the owner may change). Immediately before the single
    transport call the gateway re-reads the wall clock (never a monotonic clock,
    which excludes suspend): earlier than the claim → `clock_moved_backwards`;
    at or after `not_after` → `handoff_expired` or `window_end_reached`. Then
    `send` is never called and `send_results.record_not_invoked` records the
    attempt `not_submitted` with resolution `not_invoked` (distinct from the
    applicant's `definite_non_submission`), releases the budget hold, moves the
    message `sending → needs_reapproval` (no blind retry; a fresh approval gets
    a new Message-ID), opens no `send_uncertain` review, and completes a cancel
    recorded after the claim (note 11). If that record fails, the attempt stays
    `in_flight` and startup recovery treats it as ambiguous. Pacing is anchored
    at `not_after`, so actual Gmail calls are at least the pacing minimum apart.
    A pause, cancel or reply inside the hand-off still cannot stop the call (spec
    §10 Known limit, now bounded). The real adapter's `send` must do no network
    I/O before its single request (P0.3 contract). **Owner confirmed
    (2026-10-17):** a known-not-invoked message needs re-approval rather than
    rolling to the next window, and the limit is 5 s.
29. **Fact confirmation withdrawal (M05).** `update_fact`'s `confirm` is the
    status the fact has after the call. `confirm=False` on a confirmed fact with
    unchanged text withdraws the confirmation: version bumped, audited as
    `confirmation_withdrawn` (confirmed → proposed), and only `approved_queued`
    messages whose snapshot cites the fact move to `needs_reapproval`. Drafts
    citing it are refused with `fact_not_confirmed`, and after re-confirmation
    with `dependency_changed_since_draft` until re-drafted. The same text with
    the same status is a no-op. The claim (`dependencies_current`) also refuses
    any cited fact that is not confirmed, whatever its version. P1 autosave must
    call `update_fact` only on a real change.
30. **Post-Email-4 hold (M04).** The optional hold is the pending-slot row kept
    active with `hold_until`; occupancy ignores it from `hold_until` on (lazy
    expiry, no sweeper). Entering `conversation` or `closed` (a genuine reply or
    any closed outcome, as spec §3 lists) releases it at once in
    `set_campaign_state`. A hold that already lapsed is recorded as released at
    `hold_until` with reason `hold_lapsed`, not as ended by the later reply or
    closure. Consumed new-contact rows are never touched.
31. **Provider-observed Message-ID (M06).** `message.rfc_message_id` now means
    the provider-observed RFC Message-ID, set only after a metadata read
    verifies the SENT label, Gmail ID, thread (the campaign thread for steps
    2–4), identity (our X-Marcelorian-Id or Message-ID), recipient and a
    well-formed `<…@…>` header (which also blocks header injection); NULL on a
    sent message means unverified. The generated ID stays on the attempt for the
    `rfc822msgid` search (B12, test 65). The gateway reads metadata once after
    the success commit, with no transaction held, and returns SENT whatever the
    read does; reconciliation records the matched copy's header; applicant-
    confirmed IDs are verified the same way. Until verified, the next step
    cannot be approved (`thread_headers_unverified`) and a queued one is blocked
    at the claim. A failed read never resends; startup recovery is the only
    re-check until P2/P3 add in-process ones (the P2 confirm flow and the P3
    worker cycle must call `verify_unverified_sent`). Mismatches are audited and
    keep follow-ups held; the way out is close or stop (no new pause reason).
    **Accepted P0 limitation (owner confirmed 2026-10-17):** the sandbox mailbox lives in memory, so a crash
    between the success commit and the read holds that practice sequence
    (fail-closed) until P2 persists it; sandbox is not special-cased (§12).
    Tests use FakeGmailTransport rewrites only; live preservation is untested
    until P0.3.
32. **Mailbox identity binding (M02).** Identity is the canonical primary
    address reported by the provider (no stable account ID without a scope
    beyond `gmail.send`/`gmail.readonly`; owner confirmed 2026-10-17, together
    with the literal history definition below; a renamed primary address therefore
    counts as a different identity). The bound identity is the latest mailbox
    row, or, if none exists, the senders recorded in real-mode history.
    Real-mode history is any live or transport_test campaign (any state),
    attempt or inbound event. `connect_mailbox` checks first, inside its
    transaction and before any write: the same identity may always reconnect
    (row and version kept); a different identity after history is refused
    (`MailboxIdentityRefused`) with nothing changed; before history it is a
    setup-only switch that deactivates the old row (version bumped), resets the
    real sync row (no cursor, coverage, gap or error inherited) and clears live
    readiness. Clearing readiness while live mode is active moves to sandbox
    with dispatch paused (audited `mode_switched`), so live mode is never
    active without readiness; re-entering live needs the checklist again. The B18 sender dependency stays as defence in depth. P0.3
    connect flow: read the profile with the new token outside any transaction,
    call `connect_mailbox`, and store the token only after it commits; a refusal
    discards the token. P3 sync must re-check the bound identity inside the
    transaction that persists the cursor.
33. **Snapshot containment in recovery (round-3 #19).** A snapshot failing its
    hash during reconciliation marks that attempt uncertain (budget and slot
    held, a review opened, no search, no send) and recovery continues; during
    the post-send header check it leaves the message unverified; in
    `_schedule_next` it sends the schedule to review without undoing the send.
34. **Duplicate follow-up sequences (round-3 #2; owner answer 2026-10-17).**
    The duplicate-outreach check now also runs for steps 2–4, at approval and
    in the gateway (phase 0 and the claim), over the same identity closure.
    It covers the case where two unlinked records each got a legitimate Email 1
    and a merge or a shared address/identifier links them later. A follow-up
    is held (`duplicate_outreach`; a queued one is invalidated and needs
    re-approval) while another same-mode campaign of that person is not
    `closed` (a `candidate` reached no one) or has an in-flight or uncertain
    attempt to that identity. The applicant keeps one sequence by closing the
    other; a closed campaign's sent history does not hold the kept one. The
    Email 1 rule is unchanged. Tests:
    `tests/service/repair/test_r3_followup_duplicate.py`.
35. **Outlook hand-off implementation (S1–S6).** The hand-off reuses the
    gateway's final checks (`gateway.final_checks`), minus the ones that need
    the mailbox (auth block, sync coverage, verified thread headers, Gmail
    preflight). A hand-off is a `send_attempt` with channel `outlook_handoff`
    and status `handed_off`; the message moves `approved_queued → handed_off`.
    It holds the 24-hour budget and records pacing at the hand-off instant
    (no hand-off limit applies: nothing is submitted by the app). `handed_off`
    counts as possibly submitted wherever an unresolved attempt does (the
    one-per-message index, duplicate outreach, the own-attempt hold, keeping
    projected contacts) but is never touched by Gmail reconciliation or
    startup recovery. **Sent** finalizes through `finalize_sent` with
    provenance `applicant_reported` and no Gmail IDs; follow-up approval in
    this channel needs no verified Message-ID (there is none to verify), and
    its snapshot has no thread ID or references. **Not sent** releases the
    budget and the message needs re-approval, like `not_invoked`. Applicant
    confirmations (no prior contact, no reply, edits) are stored in the
    hand-off's audit entry. The `.eml` builder writes plain text UTF-8, no
    From/Date/Message-ID headers, and refuses CR/LF or control characters in
    header values. The compose link is built only when the full URL fits
    2,000 characters. The email is built before anything is recorded (a build
    failure is `email_not_buildable` with no hand-off), and the signature used
    is stored with the hand-off so reopening returns the identical email.
    Missed windows roll with no re-approval (S7), clicked or not; a hand-off
    click the app holds is still recorded as a functioning evaluation (audit).
    The owner chose (2026-10-19) to include the follow-up thread test in the
    real hand-off test (P0.3b runbook).
    The countdown never shows a time while a hold no clock ends applies.
    **Not sent is final** (no correction to Sent): the P2 screen must ask for
    confirmation before recording it, since a re-approved message can be
    handed off again.
36. **Migrations on existing data.** Batch table rebuilds (copy, drop, rename)
    fail with foreign keys enforced once other rows reference the table, so
    `db.migrate` runs every upgrade and downgrade with foreign keys off for
    that connection (set before the transaction), runs
    `PRAGMA foreign_key_check` before committing (any violation rolls back),
    and turns enforcement back on. Tested on a populated 0001 database
    (`tests/db/test_migration_data.py`).
37. **P0.3b guided check tool.** `marcelorian.tools.outlook_check` (owner
    approval 2026-10-19) runs the Outlook hand-off check through the
    production hand-off code against its own database in `--folder`. It has
    no mail transport, so it cannot send; it opens each email (link or
    `.eml`) and records the applicant's answers. Only the two consenting
    addresses S and R2 are allowlisted; at most four test emails go out. A
    transport_test schedule override (B2) makes follow-ups due one minute
    after Email 1 is recorded as sent and keeps the window open, and pacing is
    10–20 s in that test database only. The saved report contains no
    addresses. Tested end to end with scripted answers
    (`tests/tools/test_outlook_check.py`).
38. **Research-catalog seed (owner request 2026-10-19).** A seed CSV
    (`marcelorian_seed_v1`) is staged as non-authorizing research leads in new
    tables (`seed_import`, `research_record`, `research_evidence`,
    `research_claim`, `research_link`; migration 0003). There was no earlier
    import or staging service to reuse. The import is dry-run by default,
    refuses the whole file on any invalid row, is repeatable by external id
    plus `row_sha256`, and writes no PI, address, institution, campaign,
    approval, reservation, do-not-contact entry or mode. The send path never
    reads these tables. The applicant's own findings (confirmed current, fellow
    or trainee, left, not faculty or a consultant, retired, wrong person) live
    in `local_*` columns that imports never write. The owner asked that
    accuracy and confidence be clear before any email, so each record shows a
    person and email confidence level: the seed alone reaches at most
    "official source seen", and only the applicant's check confirms a person.
    Promotion into `pi` / `pi_email` remains P1, through the existing evidence
    rules (spec §6). Field mapping: `docs/SEED_IMPORT.md`. The real seed stays
    untracked (`data/seeds/*.csv`), since the repository is public.
39. **Launcher (P1).** `python -m marcelorian` takes the instance lock,
    migrates the database, creates the single applicant profile, serves the UI
    on 127.0.0.1 at a free port and opens it with a one-time sign-in link (never
    logged). Pressing Enter in the window opens a fresh link. With the Outlook-only
    pilot (U3) there is no tray worker: the app runs while its window is open.
    Multipart CV uploads carry the CSRF token in the `X-CSRF-Token` header,
    sent by `app.js`, so the security middleware is unchanged.
40. **Contacts from the research catalog (P1).** "Turn into a contact" on a
    research record is allowed only for a person (not a program or shared
    mailbox), in scope, with an address on record, confirmed by the applicant
    within 90 days and not flagged for re-review. The applicant states where
    the address is published: the PI's current institutional or lab page gives
    confidence **A**, a publication from the last two years explicitly
    attributing it to the investigator gives **B** (spec §6); the source URL
    is recorded as ownership evidence (who, when, where). The institution is
    reused by ROR or exact name, else created, and its time zone is set only
    with the applicant's confirmation. A matching identity (address, ORCID or
    OpenAlex ID) links to the existing PI instead of duplicating it; the same
    name at the same institution requires the applicant to confirm it is a
    different person. A live do-not-contact match blocks. Promotion creates no
    campaign, draft, approval or reservation (`services/promotion.py`).
41. **Free lookups (P1, U1).** PubMed E-utilities, OpenAlex and NIH RePORTER
    run only on the applicant's click, through a client restricted to those
    three HTTPS hosts (no user URLs), with timeouts, a 5 MB cap and polite
    retries on 429/5xx. Network calls run outside database transactions. Each
    source runs on its own and its outcome is recorded (`lookup_run`). Results
    are evidence (titles, venues, dates, authors; OpenAlex profile; NIH grants)
    and never change identifiers, addresses or recipients. Grant evidence
    raises priority but never implies a paid position (spec §7). The
    applicant's own publications are imported as proposed and confirmed one
    by one. The live APIs could not be reached from the development container
    (network policy), so parsing is tested against recorded documented shapes;
    the first real lookup on the laptop is the live check.
42. **Candidates (P1).** Dimensions stay separate (spec §7): research fit is
    the applicant's Strong/Moderate/Weak (AI suggestion later, U2), contact
    quality is the send-ready address confidence plus current-role status,
    research funding evidence is active NIH grants, appointment funding stays
    Unknown and opportunity stays "No opening verified" until a posting is
    recorded. Sort: grant evidence, fit, address confidence. Skipping hides a
    candidate; do-not-contact adds a live entry for the PI, address and
    identifiers. The place-in-use flag uses the same-place rule (N1).
