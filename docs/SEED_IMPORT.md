# Research-catalog seed import (`marcelorian_seed_v1`)

A seed file is a **non-authorizing research catalog**. Every row is a lead to verify,
not a recipient. Importing it never sends, drafts, approves, reserves capacity, changes
mode, or creates PIs, addresses, institutions, campaigns or tasks, and it never opens a
network connection. The send path never reads the staging tables.

## Running it

```text
# 1. Dry run (default): opens the app database read-only and writes nothing
python -m marcelorian.tools.seed_import data/seeds/marcelorian_ophthalmology_seed.csv

# 2. Stage it
python -m marcelorian.tools.seed_import data/seeds/marcelorian_ophthalmology_seed.csv --commit
```

Use `--db PATH` to point at another database (the default is the app database). Then
open **Research catalog** from the dashboard (`/research`).

The real seed file stays **local and untracked**: `data/seeds/*.csv` is git-ignored
because the repository is public and the file names real people.

## Parsing contract

| Rule | Implementation (`domain/seed.py`) |
|---|---|
| CSV | `csv.DictReader` over a file opened with `encoding="utf-8-sig", newline=""` |
| Schema | every row must have `schema_version = marcelorian_seed_v1`; any other value is invalid |
| Header | all 60 contract columns are required (missing or duplicate header → the file is refused) |
| Unknown columns | kept verbatim in the record's `raw_json` and reported by the preview; never rejected |
| `*_json` | `json.loads` only (never `eval`), and each must have its expected type: lists for `related_record_ids_json`, `evidence_json`, `source_urls_json`, `review_flags_json`; objects for `details_json`, `unverified_claims_json`, `provenance_json` |
| Booleans | exactly `true` / `false` (case-insensitive); blank = unknown (`NULL`); anything else is invalid |
| Authorization | `applicant_review_required` must be `true` and `send_authorized` must be `false`, or the row is invalid |
| Dates | ISO `YYYY-MM-DD` or blank. `snapshot_date` (when the file was made) is kept apart from evidence dates (`checked_on`, `last_verified_on`, `email_verified_on`) |
| `row_sha256` | SHA-256 of `json.dumps(row without row_sha256 and snapshot_date, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` in UTF-8. A mismatch is invalid. Re-exporting unchanged data on another day keeps the hash |
| Duplicate `record_id` | every copy is invalid |
| Relationships | every id in `related_record_ids_json` and `details_json.same_recipient_record_ids` must exist in the file or already be staged |

**Any invalid row refuses the whole file**; nothing is written.

## Preview categories

| Category | Meaning | Import effect |
|---|---|---|
| new | id not staged yet | inserted |
| unchanged | same `row_sha256` | only `snapshot_date`, `raw_json` and `last_import_id` move forward |
| changed | hash differs, no applicant decisions yet | seed-owned fields, evidence, claims and links replaced |
| conflicting | hash differs and the applicant has already reviewed the record | seed fields replaced, **applicant decisions kept**, record flagged "re-review" |
| excluded | valid but out of scope (`geography_status`/`review_lane` `excluded_non_us`, or a non-US `country_code`) | staged and flagged, shown as Blocked |
| invalid | row errors, duplicates, dangling references, or a staged id whose `record_type` changed | the file is refused |
| absent from file | staged earlier, not in this file | reported; **never deleted** |

External `record_id`s are stored in `external_record_id` (unique); the table has its own
integer primary key.

## Staging model (migration `0003`)

New tables only; no existing table changes.

* `seed_import`: one row per committed import (schema, file name, file SHA-256,
  snapshot date, time, actor, category counts).
* `research_record`: one row per seed record (see mapping below), plus
  `raw_json` (the complete row as read, including unknown columns),
  `excluded_reason`, `rereview_reason`, `first_import_id` / `last_import_id`, and the
  **applicant-owned** columns, which no import writes:
  `local_verification`, `local_status`, `local_verification_source`, `local_note`,
  `local_updated_at`.
* `research_evidence`: one row per `evidence_json` item (observed evidence).
* `research_claim`: one row per `unverified_claims_json` key (never treated as evidence).
* `research_link`: one row per relationship (`related` or `same_recipient`), by external id.

### Field mapping

| Seed column | Staged as |
|---|---|
| schema_version, applicant_review_required, send_authorized | validated; kept in `raw_json` |
| snapshot_date | `research_record.snapshot_date`; `seed_import.snapshot_date` |
| record_id | `research_record.external_record_id` |
| record_type | `record_type` (`contact` / `opportunity`, separate record types) |
| contact_kind, name, institution, city, state_code, country_code, geography_status, role, role_status, specialty, verification_status, review_lane, funding_status, opportunity_status | same-named columns |
| institution_key, institution_group_key | same-named columns (**proposals**; no `institution` row is created) |
| timezone_iana, timezone_status | same-named columns (**proposals**; never sets `institution.tz_verified`) |
| email, email_status, email_confidence, email_source_url, email_verified_on, email_is_shared | same-named columns. The address is **not** a `pi_email` and is never a recipient |
| recipient_dedupe_key | same-named column (indexed; drives "same route" grouping) |
| last_verified_on, primary_source_url | same-named columns |
| priority_band | integer `priority_band` (organizes work only) |
| unpaid_flag, funding_unknown_flag | nullable booleans |
| application_opens_on, application_deadline | same-named columns |
| degree_requirement, prior_training_requirement, visa_requirement | in `raw_json`; `has_eligibility_restrictions` = any of them is non-blank and does not start with "Unknown" |
| evidence_json | `research_evidence` rows (`field`, `value`, `source_url`, `source_type`, `source_access`, `checked_on`, `verification_origin`, `notes`; unknown keys in `extra_json`); `evidence_count` |
| unverified_claims_json | `research_claim` rows (key + JSON value) |
| related_record_ids_json, details_json.same_recipient_record_ids | `research_link` rows |
| subspecialties_claimed, research_topics_verified, priority_reason, next_action, outreach_approach, funding_scope, application_url, deadline_note, orcid, openalex_id, institution_ror_id, details_json, provenance_json, source_urls_json, review_flags_json, row_sha256 | `raw_json` (row_sha256 also a column); shown under "All seed fields" |
| (derived) | `freshness_date` = `last_verified_on`, else the latest evidence `checked_on`, else `email_verified_on` |

## Confidence (what is known before any email)

`domain/research.py` judges two questions and shows the weaker answer:

* **Person**: Blocked (you recorded a negative finding) · Confirmed by you · Official
  source seen (`role_status = official_source_checked`) · Partial evidence ·
  Unverified.
* **Email**: Official source seen (`official_published`, `official_program_mailbox`) ·
  Partial (`publication_attribution_observed`, `secondary_directory_observed`) ·
  Unverified (`claim_only`, `not_published_or_not_reverified`) · No usable address.
  Addresses that appear only among the claims are never used as recipients.

From the seed alone the best possible level is **Official source seen**. Only your own
check ("Your check" on the record page) can make a person **Confirmed**, and only with
an officially published, non-shared address does the record become Confirmed overall.
Recording that someone is a fellow or trainee, has left, is not faculty or a consultant,
is retired or emeritus, or is the wrong person makes the record **Blocked**. An import
never changes your check. If the seed later changes a record you have checked, it is
flagged for re-review and your check is kept.

Warnings shown on each record: role text that suggests a trainee (fellow, resident,
postdoc), emeritus, "known only as a publication author", program or shared-mailbox
routes, publication-sourced addresses, no observed evidence, stale or unknown
freshness, and hiring authority not established.

## Verification queue (owner decisions R1–R4)

`/research/queue` shows one record at a time, most sendable first:

1. Faculty or investigators whose role **and** email were both seen on an official source.
2. Partial evidence: an address from a publication or secondary directory, or a role not
   yet seen on an official page.
3. Opportunities and program routes (program contacts, shared mailboxes).
4. Claims only: publication authors and research contacts without an observed address.

Within a tier, flagged records come first, then the seed's priority band, then the
institution, so neighbours on one faculty page tend to come together. Out-of-scope
records and records matching a **live** do-not-contact entry are never queued.

In the queue:

* records not checked yet (unless you set them aside);
* confirmations older than **90 days** ("confirm again"). An expired confirmation counts
  as "Official source seen" until you confirm again;
* records the seed changed since your check. A negative finding is **never lifted
  automatically**: it shows "new evidence since your finding", and you either keep it
  (click the same finding) or **Reopen** it back to "not checked". The old finding stays
  in the record's history.

The quick buttons save your finding and move to the next record:
Confirmed current, fellow or trainee, left, not faculty, retired or emeritus, wrong person.
**Confirming requires saying where you checked** (a URL or a short note), in the queue and
on the record page. Saving the same confirmation again from the record page does not
extend it; the queue's "Confirmed current" button is an explicit re-confirmation.
Migration `0004` adds `research_record.local_verified_at` (when the current finding
was made). Existing findings take the time they were last saved.

### Working faster (owner decision N4)

* **Keyboard:** 1 Confirmed current, 2 Fellow or trainee, 3 Left, 4 Not faculty,
  5 Retired or emeritus, 6 Wrong person, S skip, P previous, O open the primary source,
  R reopen. Shortcuts pause while you type in a box.
* **Same page next:** after any finding, the queue shows the next record from the same
  source page (else the same institution), since that page is probably still open.
* **Progress:** the queue and the dashboard show confirmed (valid), confirmed this week,
  expiring within 14 days, expired, negative findings and left to check.

## Semantics carried over

* Contacts and opportunities are separate record types. A program, shared mailbox or
  coordinator is shown as a route, not a PI.
* Compensation, funding and eligibility stay on the opportunity record. Linked faculty
  keep their own (usually `unknown`) funding status.
* Records sharing a `recipient_dedupe_key`, an address or a relationship are listed
  together as one route ("approach one route, not every listed contact").
* Closed or future cycles stay as imported until a new seed provides new evidence. A
  closed program does not exclude its faculty.
* `review_lane` and `priority_band` organize the work. They are not a hiring
  probability or a measure of fit.
* The seed never establishes "not previously contacted". Every record page says that
  prior contact was not checked.
* The record page cross-references existing do-not-contact entries, PI records and
  campaigns by address, ORCID or OpenAlex ID (read-only).

## Not in this integration (later phases)

* Promoting a verified record into a `pi` / `pi_email` (P1 identity and email
  verification, through the existing review and evidence rules, spec §6).
* Re-checking sources from the app (no network access here).
* Adding a do-not-contact entry from a research record (use the existing DNC flow).
