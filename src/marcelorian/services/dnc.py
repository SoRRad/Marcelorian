"""Do-not-contact checks (spec §3, §10; decision C7).

Matches verified PI identity and confirmed merges, exact known addresses, and
ORCID/OpenAlex identifiers. Name + institution alone is only a review warning.
Entries scoped to ``live`` apply in every mode; entries created by simulated
events (sandbox / transport_test) apply only in their own mode and never edit
the live list.
"""

from __future__ import annotations

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
class _Identity:
    pi_ids: frozenset[int]
    addresses: frozenset[str]
    orcids: frozenset[str]
    openalex: frozenset[str]

    def intersects(self, other: _Identity) -> bool:
        return bool(
            self.pi_ids & other.pi_ids
            or self.addresses & other.addresses
            or self.orcids & other.orcids
            or self.openalex & other.openalex
        )


def _identity(
    session: Session, pi_id: int | None, extra_addresses: tuple[str, ...] = ()
) -> _Identity:
    cluster = merge_cluster(session, pi_id) if pi_id is not None else set()
    pis = session.scalars(select(Pi).where(Pi.id.in_(cluster))).all() if cluster else []
    return _Identity(
        pi_ids=frozenset(cluster),
        addresses=frozenset(
            cluster_addresses(session, cluster) | {a.lower() for a in extra_addresses}
        ),
        orcids=frozenset(p.orcid for p in pis if p.orcid),
        openalex=frozenset(p.openalex_id for p in pis if p.openalex_id),
    )


def matches(
    session: Session, pi_id: int, mode: Mode, extra_addresses: tuple[str, ...] = ()
) -> list[DoNotContact]:
    """In-scope entries matching this PI through any identity basis (C7).

    An entry made on a PI record covers that record's merge cluster, its exact known
    addresses and its ORCID/OpenAlex identifiers, so a re-discovered, unmerged record
    sharing any of them is matched too. Name + institution alone never matches.
    """
    candidate = _identity(session, pi_id, extra_addresses)
    scope = or_(DoNotContact.scope_mode == Mode.LIVE.value, DoNotContact.scope_mode == mode.value)
    found: list[DoNotContact] = []
    for entry in session.scalars(select(DoNotContact).where(scope)):
        entry_identity = _identity(session, entry.pi_id, (entry.email,) if entry.email else ())
        entry_identity = _Identity(
            pi_ids=entry_identity.pi_ids,
            addresses=entry_identity.addresses,
            orcids=entry_identity.orcids | ({entry.orcid} if entry.orcid else set()),
            openalex=entry_identity.openalex
            | ({entry.openalex_id} if entry.openalex_id else set()),
        )
        if candidate.intersects(entry_identity):
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
