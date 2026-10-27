"""Research-catalog seed: dry-run preview, staging import and local review (docs/SEED_IMPORT.md).

The import writes only the research staging tables (``seed_import``, ``research_*``).
It never creates or edits PIs, addresses, institutions, campaigns, messages, approvals,
reservations, do-not-contact entries, prior-contact acknowledgements or runtime mode,
and it makes no network calls. A staged record is a lead for the applicant to verify;
promoting one into the send path is a later phase and goes through the existing review.

Categories (preview and import):

* ``invalid``     -- row errors, duplicate ids, dangling relationships, or an id whose
  record type changed. Any invalid row refuses the whole import.
* ``excluded``    -- valid but out of scope (non-US); staged and flagged, never offered.
* ``new``         -- id not staged yet.
* ``unchanged``   -- same ``row_sha256`` as staged.
* ``changed``     -- hash differs; seed-owned fields are replaced.
* ``conflicting`` -- hash differs and the applicant has already reviewed the record:
  seed-owned fields are replaced, the applicant's decisions are kept, and the record
  is flagged for re-review.
* ``duplicate``   -- a new id for a person or posting already in the list (from an
  earlier file or earlier in this one): same address, or the same name at the same
  institution (postings: also the same application link). Not staged, so a second file never adds
  the same person twice (owner request 2026-10-27); links to it point to the kept one.

Records staged earlier but absent from this file are reported and kept, never deleted.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from marcelorian.db.models import (
    AuditEvent,
    Campaign,
    DoNotContact,
    Pi,
    PiEmail,
    ResearchClaim,
    ResearchEvidence,
    ResearchLink,
    ResearchRecord,
    SeedImport,
)
from marcelorian.domain.enums import Actor, Mode
from marcelorian.domain.research import (
    AGING_DAYS,
    BLOCKING_VERIFICATIONS,
    CONFIRMATION_VALID_DAYS,
    LOCAL_STATUSES,
    LOCAL_VERIFICATIONS,
    QUEUE_TIERS,
    RECENT_DAYS,
    Confidence,
    Facts,
    confidence,
    confirmation_expired,
    freshness,
    parse_iso,
    queue_tier,
)
from marcelorian.domain.scholarly_ids import (
    IdentifierError,
    canonical_openalex_author_id,
    canonical_orcid,
)
from marcelorian.domain.seed import COLUMNS, SCHEMA_VERSION, ParsedSeed, RowError, SeedRecord
from marcelorian.domain.seed import dangling_references as _dangling
from marcelorian.services.audit import audit

SOURCE_REQUIRED = "Say where you checked (a URL or a short note) before confirming."
CATEGORIES = ("new", "duplicate", "unchanged", "changed", "conflicting", "excluded", "invalid")
MAX_LOCAL_TEXT = 2000

#: Seed columns copied to same-named ``research_record`` columns (text, blank = "").
TEXT_FIELDS = (
    "contact_kind",
    "name",
    "institution",
    "institution_key",
    "institution_group_key",
    "city",
    "state_code",
    "country_code",
    "geography_status",
    "timezone_iana",
    "timezone_status",
    "role",
    "role_status",
    "specialty",
    "email",
    "email_status",
    "email_confidence",
    "email_source_url",
    "email_verified_on",
    "recipient_dedupe_key",
    "verification_status",
    "last_verified_on",
    "primary_source_url",
    "review_lane",
    "funding_status",
    "opportunity_status",
    "application_opens_on",
    "application_deadline",
    "snapshot_date",
)
BOOL_FIELDS = ("email_is_shared", "unpaid_flag", "funding_unknown_flag")
RESTRICTION_FIELDS = ("degree_requirement", "prior_training_requirement", "visa_requirement")
EVIDENCE_KEYS = (
    "field",
    "value",
    "source_url",
    "source_type",
    "source_access",
    "checked_on",
    "verification_origin",
    "notes",
)


class SeedRejected(ValueError):
    """The file has invalid rows; nothing was written."""

    def __init__(self, preview: SeedPreview) -> None:
        super().__init__(f"{len(preview.invalid)} invalid row(s); nothing imported")
        self.preview = preview


@dataclass
class SeedPreview:
    row_count: int = 0
    snapshot_dates: list[str] = field(default_factory=list)
    unknown_columns: tuple[str, ...] = ()
    categories: dict[str, list[str]] = field(
        default_factory=lambda: {c: [] for c in CATEGORIES if c != "invalid"}
    )
    invalid: list[RowError] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    record_types: dict[str, int] = field(default_factory=dict)
    import_id: int | None = None
    #: Skipped duplicate id -> the external id of the record kept for that person.
    duplicate_of: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.invalid

    def counts(self) -> dict[str, int]:
        counts = {c: len(ids) for c, ids in self.categories.items()}
        counts["invalid"] = len(self.invalid)
        counts["absent_from_file"] = len(self.absent)
        return counts


# --------------------------------------------------------------------------- #
# Preview and import
# --------------------------------------------------------------------------- #


def has_local_decisions(record: ResearchRecord) -> bool:
    return (
        record.local_status != "unreviewed"
        or record.local_verification != "unchecked"
        or bool(record.local_note)
        or bool(record.local_verification_source)
        or record.promoted_pi_id is not None
    )


def preview(session: Session, parsed: ParsedSeed) -> SeedPreview:
    """Classify every row against what is staged. Reads only; writes nothing."""
    staged = {r.external_record_id: r for r in session.scalars(select(ResearchRecord))}
    result = SeedPreview(
        row_count=parsed.row_count,
        snapshot_dates=sorted(parsed.snapshot_dates()),
        unknown_columns=parsed.unknown_columns,
        invalid=list(parsed.errors),
        record_types=dict(Counter(r.record_type for r in parsed.records)),
    )
    result.invalid += _dangling(parsed, staged)
    bad = {e.record_id for e in result.invalid}
    known: dict[str, str] = {}  # identity key -> external id already in the list
    for staged_record in staged.values():
        for key in identity_keys(
            staged_record.record_type,
            name=staged_record.name,
            institution_key=staged_record.institution_key,
            email=staged_record.email,
            email_is_shared=bool(staged_record.email_is_shared),
            application_url=_raw_value(staged_record, "application_url"),
        ):
            known.setdefault(key, staged_record.external_record_id)
    for record in parsed.records:
        existing = staged.get(record.record_id)
        if existing is not None and existing.record_type != record.record_type:
            result.invalid.append(RowError(0, record.record_id, "record_type_changed"))
            continue
        if record.record_id in bad:
            continue
        category = _category(record, existing)
        keys = identity_keys(
            record.record_type,
            name=record.text("name"),
            institution_key=record.text("institution_key"),
            email=record.text("email"),
            email_is_shared=record.text("email_is_shared").strip().lower() in ("true", "1", "yes"),
            application_url=record.text("application_url"),
        )
        if category == "new":
            match = next((known[k] for k in keys if k in known), None)
            if match is not None:
                result.duplicate_of[record.record_id] = match
                category = "duplicate"
        for key in keys:
            known.setdefault(key, record.record_id)
        result.categories[category].append(record.record_id)
    in_file = {r.record_id for r in parsed.records}
    result.absent = sorted(set(staged) - in_file)
    return result


_TITLES = "dr prof professor md phd mph mbbs do od ms msc jr sr ii iii"
_PERSON_TITLES = frozenset(_TITLES.split())


def _folded_name(name: str) -> str:
    words = re.sub(r"[^\w\s]", " ", name.casefold()).split()
    return " ".join(w for w in words if w not in _PERSON_TITLES)


def _url_key(url: str) -> str:
    text = url.strip().casefold()
    text = re.sub(r"^https?://(www\.)?", "", text)
    return text.split("#", 1)[0].rstrip("/")


def identity_keys(
    record_type: str,
    *,
    name: str,
    institution_key: str,
    email: str,
    email_is_shared: bool,
    application_url: str,
) -> set[str]:
    """Keys that mean "the same person or posting" across files with different ids."""
    keys: set[str] = set()
    folded = _folded_name(name)
    place = institution_key.strip().casefold()
    address = email.strip().casefold()
    if record_type == "contact":
        if address and "@" in address and not email_is_shared:
            keys.add(f"email:{address}")
        if folded and place:
            keys.add(f"person:{folded}|{place}")
    elif folded and place:
        # Not the application link alone: programs reuse one portal for every cycle.
        keys.add(f"posting:{folded}|{place}|{_url_key(application_url)}")
    return keys


def _raw_value(record: ResearchRecord, column: str) -> str:
    try:
        raw = json.loads(record.raw_json or "{}")
    except ValueError:
        return ""
    value = raw.get(column, "") if isinstance(raw, dict) else ""
    return value if isinstance(value, str) else ""


def _category(record: SeedRecord, existing: ResearchRecord | None) -> str:
    if record.excluded_reason:
        return "excluded"
    if existing is None:
        return "new"
    if existing.row_sha256 == record.row_sha256:
        return "unchanged"
    return "conflicting" if has_local_decisions(existing) else "changed"


def import_seed(
    session: Session,
    parsed: ParsedSeed,
    *,
    source_name: str,
    file_sha256: str,
    now: datetime,
    actor: Actor | str = Actor.APPLICANT,
) -> SeedPreview:
    """Stage the file in the caller's transaction, or raise ``SeedRejected`` untouched."""
    plan = preview(session, parsed)
    if not plan.ok or not parsed.records:
        raise SeedRejected(plan)
    batch = SeedImport(
        schema_version=SCHEMA_VERSION,
        source_name=source_name[:255],
        file_sha256=file_sha256,
        snapshot_date=max(plan.snapshot_dates),
        imported_at=now,
        actor=str(actor),
        counts_json=json.dumps(plan.counts(), sort_keys=True),
    )
    session.add(batch)
    session.flush()
    staged = {r.external_record_id: r for r in session.scalars(select(ResearchRecord))}
    category_of = {rid: c for c, ids in plan.categories.items() for rid in ids}
    for record in parsed.records:
        existing = staged.get(record.record_id)
        category = category_of[record.record_id]
        if category == "duplicate":
            continue  # already in the list under another id
        if existing is None:
            existing = ResearchRecord(
                external_record_id=record.record_id,
                record_type=record.record_type,
                first_import_id=batch.id,
                created_at=now,
                local_status="unreviewed",
                local_verification="unchecked",
            )
            session.add(existing)
        elif existing.row_sha256 == record.row_sha256:
            # Unchanged content: only the file's date and the raw row move forward.
            existing.snapshot_date = record.text("snapshot_date")
            existing.raw_json = _raw(record)
            existing.last_import_id = batch.id
            continue
        elif category == "conflicting":
            existing.rereview_reason = (
                f"The seed changed this record (import {batch.id}) after your review; "
                "your decisions were kept. Re-check before relying on them."
            )
        _apply_seed_fields(existing, record)
        existing.last_import_id = batch.id
        existing.updated_at = now
        session.flush()
        _replace_children(session, existing, record, plan.duplicate_of)
    plan.import_id = batch.id
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="seed_import",
        entity_id=batch.id,
        action="research_seed_imported",
        detail={"counts": plan.counts(), "source": batch.source_name},
    )
    return plan


def _raw(record: SeedRecord) -> str:
    return json.dumps(dict(record.raw), ensure_ascii=False, sort_keys=True)


def _apply_seed_fields(target: ResearchRecord, record: SeedRecord) -> None:
    for name in TEXT_FIELDS:
        setattr(target, name, record.text(name).strip())
    for name in BOOL_FIELDS:
        setattr(target, name, record.bools[name])
    band = record.text("priority_band").strip()
    target.priority_band = int(band) if band else None
    target.has_eligibility_restrictions = any(
        _is_restriction(record.text(name)) for name in RESTRICTION_FIELDS
    )
    observed = [parse_iso(str(e.get("checked_on") or "")) for e in record.json["evidence_json"]]
    candidates = [
        record.dates["last_verified_on"],
        max((d for d in observed if d is not None), default=None),
        record.dates["email_verified_on"],
    ]
    first = next((d for d in candidates if d is not None), None)
    target.freshness_date = first.isoformat() if first else ""
    target.evidence_count = len(record.json["evidence_json"])
    target.row_sha256 = record.row_sha256
    target.raw_json = _raw(record)
    target.excluded_reason = record.excluded_reason


def _is_restriction(value: str) -> bool:
    text = value.strip()
    return bool(text) and not text.lower().startswith("unknown")


def _replace_children(
    session: Session,
    target: ResearchRecord,
    record: SeedRecord,
    aliases: dict[str, str] | None = None,
) -> None:
    for model in (ResearchEvidence, ResearchClaim, ResearchLink):
        session.execute(delete(model).where(model.record_id == target.id))
    for position, item in enumerate(record.json["evidence_json"]):
        extra = {k: v for k, v in item.items() if k not in EVIDENCE_KEYS}
        values = {k: _as_text(item.get(k)) for k in EVIDENCE_KEYS}
        session.add(
            ResearchEvidence(
                record_id=target.id,
                position=position,
                extra_json=json.dumps(extra, ensure_ascii=False, sort_keys=True) if extra else None,
                **values,
            )
        )
    for key, value in record.json["unverified_claims_json"].items():
        session.add(
            ResearchClaim(
                record_id=target.id,
                key=str(key)[:120],
                value_json=json.dumps(value, ensure_ascii=False, sort_keys=True),
            )
        )
    links: dict[tuple[str, str], None] = {}
    for target_id in record.json["related_record_ids_json"]:
        links[(str(target_id), "related")] = None
    same = record.json["details_json"].get("same_recipient_record_ids") or []
    for target_id in same if isinstance(same, list) else []:
        links[(str(target_id), "same_recipient")] = None
    renamed = {((aliases or {}).get(t, t), kind) for t, kind in links}
    for target_id, kind in sorted(renamed):
        if target_id == target.external_record_id:
            continue  # a duplicate that was this record itself
        session.add(ResearchLink(record_id=target.id, target_external_id=target_id, kind=kind))


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


# --------------------------------------------------------------------------- #
# Applicant review (never touched by an import)
# --------------------------------------------------------------------------- #


def set_local_review(
    session: Session,
    record_id: int,
    *,
    status: str,
    verification: str,
    source: str,
    note: str,
    now: datetime,
    actor: Actor | str = Actor.APPLICANT,
    reconfirm: bool = False,
) -> ResearchRecord:
    """Save the applicant's finding. A confirmation must say where it was checked.

    The finding's date (``local_verified_at``) moves when the finding changes, when an
    expired confirmation is saved again, or on an explicit re-confirmation.
    """
    if status not in LOCAL_STATUSES:
        raise ValueError(f"unknown local status: {status}")
    if verification not in LOCAL_VERIFICATIONS:
        raise ValueError(f"unknown verification: {verification}")
    if verification == "confirmed_current" and not source.strip():
        raise ValueError(SOURCE_REQUIRED)
    record = session.get(ResearchRecord, record_id)
    if record is None:
        raise LookupError(record_id)
    before = record.local_verification
    verified_on = record.local_verified_at.date() if record.local_verified_at else None
    if (
        verification != before
        or reconfirm
        or confirmation_expired(verification, verified_on, now.date())
    ):
        record.local_verified_at = now if verification != "unchecked" else None
    record.local_status = status
    record.local_verification = verification
    record.local_verification_source = source.strip()[:MAX_LOCAL_TEXT] or None
    record.local_note = note.strip()[:MAX_LOCAL_TEXT] or None
    record.local_updated_at = now
    record.rereview_reason = None  # the applicant has looked at it again
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="research_record",
        entity_id=record.id,
        action="research_local_review",
        from_state=before,
        to_state=verification,
        detail={"local_status": status, "reconfirm": reconfirm},
    )
    return record


def quick_finding(
    session: Session,
    record_id: int,
    *,
    verification: str,
    source: str,
    note: str,
    now: datetime,
    actor: Actor | str = Actor.APPLICANT,
) -> ResearchRecord:
    """The queue's one-click finding: keeps triage, adds the note to any earlier one."""
    record = session.get(ResearchRecord, record_id)
    if record is None:
        raise LookupError(record_id)
    notes = [n for n in (record.local_note, note.strip()) if n]
    return set_local_review(
        session,
        record_id,
        status=record.local_status,
        verification=verification,
        source=source or (record.local_verification_source or ""),
        note="\n".join(notes),
        now=now,
        actor=actor,
        reconfirm=verification == "confirmed_current",
    )


def reopen(
    session: Session, record_id: int, *, now: datetime, actor: Actor | str = Actor.APPLICANT
) -> ResearchRecord:
    """Lift a negative finding (owner, R1). The old finding stays in the audit history."""
    record = session.get(ResearchRecord, record_id)
    if record is None:
        raise LookupError(record_id)
    if record.local_verification not in BLOCKING_VERIFICATIONS:
        raise ValueError("only a negative finding can be reopened")
    before = record.local_verification
    record.local_verification = "unchecked"
    record.local_verified_at = None
    record.local_updated_at = now
    record.rereview_reason = None
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="research_record",
        entity_id=record.id,
        action="research_reopened",
        from_state=before,
        to_state="unchecked",
        detail={"previous_source": record.local_verification_source or ""},
    )
    return record


def history(session: Session, record: ResearchRecord) -> list[AuditEvent]:
    return list(
        session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.entity_type == "research_record",
                AuditEvent.entity_id == str(record.id),
            )
            .order_by(AuditEvent.at.desc(), AuditEvent.id.desc())
        )
    )


# --------------------------------------------------------------------------- #
# Verification queue (owner, R2/R4: most sendable first, one record at a time)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class QueueItem:
    record: ResearchRecord
    confidence: Confidence
    tier: int
    reason: str


def queue(session: Session, today: date, tier: int | None = None) -> list[QueueItem]:
    """Records that need the applicant's check, most sendable first.

    In the queue: records not checked yet (unless set aside), expired confirmations,
    and records the seed changed since the applicant's check (including negative
    findings with new evidence, which can be reopened). Never in it: out-of-scope
    records and records matching a live do-not-contact entry.
    """
    blocked = _dnc_keys(session)
    items: list[tuple[tuple[Any, ...], QueueItem]] = []
    for record in session.scalars(select(ResearchRecord)):
        if record.excluded_reason or _matches_dnc(record, blocked):
            continue
        facts = facts_for(record, today)
        reason = _queue_reason(record, facts)
        if reason is None:
            continue
        item_tier = queue_tier(facts)
        if tier is not None and item_tier != tier:
            continue
        flagged = 0 if reason != NOT_CHECKED else 1
        key = (
            item_tier,
            flagged,
            record.priority_band is None,
            record.priority_band or 0,
            record.institution_key,
            record.name,
            record.id,
        )
        items.append((key, QueueItem(record, confidence(facts), item_tier, reason)))
    return [item for _key, item in sorted(items, key=lambda pair: pair[0])]


NOT_CHECKED = "Not checked yet"


def _queue_reason(record: ResearchRecord, facts: Facts) -> str | None:
    if record.rereview_reason:
        if record.local_verification in BLOCKING_VERIFICATIONS:
            return "New evidence since your finding: keep it or reopen"
        return "The seed changed since your check"
    if facts.confirmation_expired:
        return f"Confirm again (your check is over {CONFIRMATION_VALID_DAYS} days old)"
    if record.local_verification == "unchecked" and record.local_status != "set_aside":
        return NOT_CHECKED
    return None


@dataclass(frozen=True)
class Progress:
    """Verification progress for the dashboard."""

    confirmed: int  # confirmations still valid
    confirmed_this_week: int
    expiring_soon: int  # valid, but expire within EXPIRY_WARNING_DAYS
    expired: int
    negative: int  # fellow, left, not faculty, retired, wrong person
    to_check: int  # current queue length
    total: int


EXPIRY_WARNING_DAYS = 14


def progress(session: Session, today: date) -> Progress:
    confirmed = this_week = expiring = expired = negative = total = 0
    for record in session.scalars(select(ResearchRecord)):
        total += 1
        if record.local_verification in BLOCKING_VERIFICATIONS:
            negative += 1
        if record.local_verification != "confirmed_current":
            continue
        verified_on = record.local_verified_at.date() if record.local_verified_at else None
        if confirmation_expired(record.local_verification, verified_on, today):
            expired += 1
            continue
        confirmed += 1
        assert verified_on is not None
        age = (today - verified_on).days
        if age < 7:
            this_week += 1
        if age > CONFIRMATION_VALID_DAYS - EXPIRY_WARNING_DAYS:
            expiring += 1
    return Progress(
        confirmed, this_week, expiring, expired, negative, len(queue(session, today)), total
    )


def same_page_next(
    session: Session, items: list[QueueItem], record_id: int
) -> tuple[int, str] | None:
    """After a finding, the next queued record from the same source page, else the
    same institution: that page is probably still open in the applicant's browser.

    Returns (position in ``items``, name of the record just checked) or None.
    """
    done = session.get(ResearchRecord, record_id)
    if done is None:
        return None
    label = done.name or done.external_record_id
    page = done.primary_source_url.strip()
    if page:
        for index, item in enumerate(items):
            if item.record.id != done.id and item.record.primary_source_url.strip() == page:
                return index, label
    if done.institution_key:
        for index, item in enumerate(items):
            if item.record.id != done.id and item.record.institution_key == done.institution_key:
                return index, label
    return None


def queue_counts(items: list[QueueItem]) -> dict[int, int]:
    counts = dict.fromkeys(QUEUE_TIERS, 0)
    for item in items:
        counts[item.tier] += 1
    return counts


def _dnc_keys(session: Session) -> tuple[set[str], set[str], set[str]]:
    emails: set[str] = set()
    orcids: set[str] = set()
    openalex: set[str] = set()
    for entry in session.scalars(
        select(DoNotContact).where(DoNotContact.scope_mode == Mode.LIVE.value)
    ):
        if entry.email:
            emails.add(entry.email.lower())
        if entry.orcid:
            orcids.add(entry.orcid)
        if entry.openalex_id:
            openalex.add(entry.openalex_id)
    return emails, orcids, openalex


def _matches_dnc(record: ResearchRecord, keys: tuple[set[str], set[str], set[str]]) -> bool:
    emails, orcids, openalex = keys
    if not (emails or orcids or openalex):
        return False
    if record.email and record.email.strip().lower() in emails:
        return True
    orcid = _canonical(canonical_orcid, record_orcid(record))
    oa = _canonical(canonical_openalex_author_id, record_value(record, "openalex_id"))
    return (orcid is not None and orcid in orcids) or (oa is not None and oa in openalex)


# --------------------------------------------------------------------------- #
# Reading for the UI
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Filters:
    record_type: str = ""
    review_lane: str = ""
    institution_key: str = ""
    funding_status: str = ""
    restrictions: str = ""  # "yes" | "no" | ""
    email_status: str = ""
    freshness: str = ""
    confidence: str = ""
    local_verification: str = ""
    query: str = ""


@dataclass(frozen=True)
class Row:
    record: ResearchRecord
    confidence: Confidence
    freshness: str
    shared_route: bool


def facts_for(record: ResearchRecord, today: date) -> Facts:
    verified_on = record.local_verified_at.date() if record.local_verified_at else None
    details: dict[str, Any] = {}
    try:
        raw = json.loads(record.raw_json)
        loaded = json.loads(raw.get("details_json") or "{}")
        details = loaded if isinstance(loaded, dict) else {}
    except (ValueError, TypeError):
        details = {}
    return Facts(
        record_type=record.record_type,
        contact_kind=record.contact_kind,
        role=record.role,
        role_status=record.role_status,
        verification_status=record.verification_status,
        email=record.email,
        email_status=record.email_status,
        email_is_shared=record.email_is_shared,
        excluded_reason=record.excluded_reason,
        local_verification=record.local_verification,
        evidence_count=record.evidence_count,
        hiring_authority=str(details.get("hiring_authority") or ""),
        freshness=freshness(parse_iso(record.freshness_date), today),
        local_verified_on=verified_on,
        confirmation_expired=confirmation_expired(record.local_verification, verified_on, today),
    )


def list_records(session: Session, filters: Filters, today: date) -> list[Row]:
    query = select(ResearchRecord)
    for column, value in (
        (ResearchRecord.record_type, filters.record_type),
        (ResearchRecord.review_lane, filters.review_lane),
        (ResearchRecord.institution_key, filters.institution_key),
        (ResearchRecord.funding_status, filters.funding_status),
        (ResearchRecord.email_status, filters.email_status),
        (ResearchRecord.local_verification, filters.local_verification),
    ):
        if value:
            query = query.where(column == value)
    if filters.restrictions in {"yes", "no"}:
        query = query.where(
            ResearchRecord.has_eligibility_restrictions.is_(filters.restrictions == "yes")
        )
    if filters.freshness:
        query = query.where(_freshness_clause(filters.freshness, today))
    if filters.query:
        like = f"%{filters.query.strip()}%"
        query = query.where(
            or_(ResearchRecord.name.ilike(like), ResearchRecord.institution.ilike(like))
        )
    query = query.order_by(
        ResearchRecord.priority_band.is_(None),
        ResearchRecord.priority_band,
        ResearchRecord.name,
    )
    records = list(session.scalars(query))
    shared = _shared_keys(session)
    rows = []
    for record in records:
        facts = facts_for(record, today)
        level = confidence(facts)
        if filters.confidence and level.level != filters.confidence:
            continue
        rows.append(Row(record, level, facts.freshness, record.recipient_dedupe_key in shared))
    return rows


def _freshness_clause(bucket: str, today: date) -> Any:
    recent = (today - timedelta(days=RECENT_DAYS)).isoformat()
    aging = (today - timedelta(days=AGING_DAYS)).isoformat()
    column = ResearchRecord.freshness_date
    if bucket == "recent":
        return column >= recent
    if bucket == "aging":
        return (column < recent) & (column >= aging)
    if bucket == "stale":
        return (column != "") & (column < aging)
    return column == ""


def _shared_keys(session: Session) -> set[str]:
    keys = Counter(k for k in session.scalars(select(ResearchRecord.recipient_dedupe_key)) if k)
    return {k for k, n in keys.items() if n > 1}


def filter_options(session: Session) -> dict[str, list[str]]:
    def distinct(column: Any) -> list[str]:
        return sorted(v for v in session.scalars(select(column).distinct()) if v)

    return {
        "record_type": distinct(ResearchRecord.record_type),
        "review_lane": distinct(ResearchRecord.review_lane),
        "institution_key": distinct(ResearchRecord.institution_key),
        "funding_status": distinct(ResearchRecord.funding_status),
        "email_status": distinct(ResearchRecord.email_status),
    }


def institution_names(session: Session) -> dict[str, str]:
    """A display name per proposed institution key (first seen; keys are proposals)."""
    names: dict[str, str] = {}
    for key, name in session.execute(
        select(ResearchRecord.institution_key, ResearchRecord.institution).order_by(
            ResearchRecord.id
        )
    ):
        if key and name and key not in names:
            names[key] = name
    return names


def route_group(session: Session, record: ResearchRecord) -> list[ResearchRecord]:
    """Other records reaching the same endpoint: shared dedupe key, address or link.

    Only one of them should be approached (no parallel outreach to one endpoint).
    """
    linked_out = set(
        session.scalars(
            select(ResearchLink.target_external_id).where(ResearchLink.record_id == record.id)
        )
    )
    linked_in = set(
        session.scalars(
            select(ResearchLink.record_id).where(
                ResearchLink.target_external_id == record.external_record_id
            )
        )
    )
    conditions: list[Any] = [
        ResearchRecord.external_record_id.in_(linked_out),
        ResearchRecord.id.in_(linked_in),
    ]
    if record.recipient_dedupe_key:
        conditions.append(ResearchRecord.recipient_dedupe_key == record.recipient_dedupe_key)
    if record.email:
        conditions.append(ResearchRecord.email.ilike(record.email.strip()))
    return list(
        session.scalars(
            select(ResearchRecord)
            .where(or_(*conditions), ResearchRecord.id != record.id)
            .order_by(ResearchRecord.record_type, ResearchRecord.name)
        )
    )


@dataclass(frozen=True)
class LocalMatches:
    """Read-only cross-reference with the authoritative tables (display only)."""

    dnc: list[DoNotContact]
    pi_ids: list[int]
    campaign_states: list[str]


def local_matches(session: Session, record: ResearchRecord) -> LocalMatches:
    orcid = _canonical(canonical_orcid, record_orcid(record))
    openalex = _canonical(canonical_openalex_author_id, record_value(record, "openalex_id"))
    email = record.email.strip().lower()
    dnc_conditions: list[Any] = []
    pi_ids: set[int] = set()
    if email:
        dnc_conditions.append(DoNotContact.email == email)
        pi_ids.update(session.scalars(select(PiEmail.pi_id).where(PiEmail.address == email)))
    if orcid:
        dnc_conditions.append(DoNotContact.orcid == orcid)
        pi_ids.update(session.scalars(select(Pi.id).where(Pi.orcid == orcid)))
    if openalex:
        dnc_conditions.append(DoNotContact.openalex_id == openalex)
        pi_ids.update(session.scalars(select(Pi.id).where(Pi.openalex_id == openalex)))
    if pi_ids:
        dnc_conditions.append(DoNotContact.pi_id.in_(pi_ids))
    dnc = (
        list(session.scalars(select(DoNotContact).where(or_(*dnc_conditions))))
        if (dnc_conditions)
        else []
    )
    states = (
        sorted(
            f"{mode}: {state}"
            for mode, state in session.execute(
                select(Campaign.mode, Campaign.state).where(Campaign.pi_id.in_(pi_ids))
            )
        )
        if pi_ids
        else []
    )
    return LocalMatches(dnc, sorted(pi_ids), states)


def record_value(record: ResearchRecord, column: str) -> str:
    try:
        raw = json.loads(record.raw_json)
    except ValueError:
        return ""
    return str(raw.get(column) or "") if isinstance(raw, dict) else ""


def raw_fields(record: ResearchRecord) -> list[tuple[str, str]]:
    """Every column of the row as last imported, in file order (unknown columns last)."""
    try:
        raw = json.loads(record.raw_json)
    except ValueError:
        return []
    if not isinstance(raw, dict):
        return []
    order = {name: i for i, name in enumerate(COLUMNS)}
    return sorted(
        ((str(k), str(v)) for k, v in raw.items()), key=lambda kv: (order.get(kv[0], 999), kv[0])
    )


def _json_list(record: ResearchRecord, column: str) -> list[Any]:
    try:
        value = json.loads(record_value(record, column) or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def source_urls(record: ResearchRecord) -> list[str]:
    return [str(u) for u in _json_list(record, "source_urls_json")]


def review_flags(record: ResearchRecord) -> list[str]:
    return [str(f) for f in _json_list(record, "review_flags_json")]


def record_orcid(record: ResearchRecord) -> str:
    return record_value(record, "orcid")


def _canonical(fn: Any, value: str) -> str | None:
    if not value:
        return None
    try:
        result: str = fn(value)
    except IdentifierError:
        return None
    return result


def evidence_for(session: Session, record: ResearchRecord) -> list[ResearchEvidence]:
    return list(
        session.scalars(
            select(ResearchEvidence)
            .where(ResearchEvidence.record_id == record.id)
            .order_by(ResearchEvidence.position)
        )
    )


def claims_for(session: Session, record: ResearchRecord) -> list[tuple[str, Any]]:
    claims = session.scalars(
        select(ResearchClaim)
        .where(ResearchClaim.record_id == record.id)
        .order_by(ResearchClaim.key)
    )
    return [(c.key, json.loads(c.value_json)) for c in claims]


def imports(session: Session) -> Iterable[SeedImport]:
    return session.scalars(select(SeedImport).order_by(SeedImport.id.desc()))
