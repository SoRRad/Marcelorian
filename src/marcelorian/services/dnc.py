"""Do-not-contact checks (spec §3, §10; decision C7).

Matches verified PI identity and confirmed merges, exact known addresses, and
ORCID/OpenAlex identifiers. Name + institution alone is only a review warning.
Entries scoped to ``live`` apply in every mode; entries created by simulated
events (sandbox / transport_test) apply only in their own mode and never edit
the live list.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from marcelorian.db.models import DoNotContact, Pi, PiEmail
from marcelorian.domain.enums import Actor, Mode
from marcelorian.domain.recipients import normalize_address
from marcelorian.services.audit import audit


def merge_cluster(session: Session, pi_id: int) -> set[int]:
    """All PI ids connected to ``pi_id`` through confirmed merges (either direction)."""
    cluster = {pi_id}
    frontier = [pi_id]
    while frontier:
        current = frontier.pop()
        pi = session.get(Pi, current)
        neighbours: set[int] = set()
        if pi is not None and pi.merged_into_id is not None:
            neighbours.add(pi.merged_into_id)
        neighbours.update(session.scalars(select(Pi.id).where(Pi.merged_into_id == current)))
        for other in neighbours - cluster:
            cluster.add(other)
            frontier.append(other)
    return cluster


def cluster_addresses(session: Session, cluster: set[int]) -> set[str]:
    return {
        a.lower()
        for a in session.scalars(select(PiEmail.address).where(PiEmail.pi_id.in_(cluster)))
    }


@dataclass(frozen=True)
class Identity:
    """Everything known to denote one person (spec §3; decision C7).

    The closure of the seeds over confirmed merges, exact known addresses and
    ORCID/OpenAlex identifiers: any PI record sharing one of them belongs to the
    same identity, and its own addresses and identifiers join in turn. Name +
    institution is never part of it.
    """

    pi_ids: frozenset[int]
    addresses: frozenset[str]
    orcids: frozenset[str]
    openalex: frozenset[str]


def identity_closure(
    session: Session,
    *,
    pi_ids: Iterable[int] = (),
    addresses: Iterable[str] = (),
    orcids: Iterable[str] = (),
    openalex: Iterable[str] = (),
) -> Identity:
    ids: set[int] = set()
    addrs = {a.strip().lower() for a in addresses if a}
    orc = {o for o in orcids if o}
    oa = {o for o in openalex if o}
    seeds = set(pi_ids)
    while True:
        if addrs:
            seeds.update(session.scalars(select(PiEmail.pi_id).where(PiEmail.address.in_(addrs))))
        if orc:
            seeds.update(session.scalars(select(Pi.id).where(Pi.orcid.in_(orc))))
        if oa:
            seeds.update(session.scalars(select(Pi.id).where(Pi.openalex_id.in_(oa))))
        for pid in seeds:
            if pid not in ids:
                ids |= merge_cluster(session, pid)
        records = session.scalars(select(Pi).where(Pi.id.in_(ids))).all() if ids else []
        new_addrs = addrs | cluster_addresses(session, ids)
        new_orc = orc | {r.orcid for r in records if r.orcid}
        new_oa = oa | {r.openalex_id for r in records if r.openalex_id}
        if seeds <= ids and (new_addrs, new_orc, new_oa) == (addrs, orc, oa):
            return Identity(frozenset(ids), frozenset(addrs), frozenset(orc), frozenset(oa))
        addrs, orc, oa = new_addrs, new_orc, new_oa
        seeds = set(ids)


def pi_identity(session: Session, pi_id: int, extra_addresses: Iterable[str] = ()) -> Identity:
    return identity_closure(session, pi_ids=(pi_id,), addresses=extra_addresses)


def matches(
    session: Session, pi_id: int, mode: Mode, extra_addresses: tuple[str, ...] = ()
) -> list[DoNotContact]:
    """In-scope entries matching this PI through any identity basis (C7).

    The candidate's identity closure covers every record linked to it by a confirmed
    merge, an exact known address or an ORCID/OpenAlex identifier, so an entry made
    on any of those records, addresses or identifiers matches a re-discovered,
    unmerged record too. Name + institution alone never matches.
    """
    candidate = pi_identity(session, pi_id, extra_addresses)
    scope = or_(DoNotContact.scope_mode == Mode.LIVE.value, DoNotContact.scope_mode == mode.value)
    found: list[DoNotContact] = []
    for entry in session.scalars(select(DoNotContact).where(scope)):
        if (
            (entry.pi_id is not None and entry.pi_id in candidate.pi_ids)
            or (entry.email is not None and entry.email.lower() in candidate.addresses)
            or (entry.orcid is not None and entry.orcid in candidate.orcids)
            or (entry.openalex_id is not None and entry.openalex_id in candidate.openalex)
        ):
            found.append(entry)
    return found


def name_institution_warnings(session: Session, pi_id: int) -> list[int]:
    """PIs on the DNC list with the same name and institution: a warning, never a match."""
    pi = session.get(Pi, pi_id)
    if pi is None:
        return []
    return list(
        session.scalars(
            select(Pi.id)
            .join(DoNotContact, DoNotContact.pi_id == Pi.id)
            .where(
                Pi.id != pi_id,
                func.lower(Pi.full_name) == pi.full_name.lower(),
                Pi.institution_id == pi.institution_id,
            )
        )
    )


def add_entry(
    session: Session,
    *,
    scope_mode: Mode,
    actor: Actor,
    now: datetime,
    pi_id: int | None = None,
    email: str | None = None,
    orcid: str | None = None,
    openalex_id: str | None = None,
    reason: str | None = None,
) -> DoNotContact:
    if not any((pi_id, email, orcid, openalex_id)):
        raise ValueError("a do-not-contact entry needs an identity")
    entry = DoNotContact(
        scope_mode=scope_mode.value,
        pi_id=pi_id,
        email=normalize_address(email) if email else None,
        orcid=orcid,
        openalex_id=openalex_id,
        reason=reason,
        created_at=now,
        created_by=str(actor),
    )
    session.add(entry)
    session.flush()
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="do_not_contact",
        entity_id=entry.id,
        action="added",
        detail={"scope": scope_mode.value, "pi_id": pi_id},
    )
    return entry
