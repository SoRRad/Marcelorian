"""Audit events: every state transition writes one (spec §3).

Audit rows live in the authoritative local database. They never contain
email bodies, CV text, tokens or keys.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from marcelorian.db.models import AuditEvent
from marcelorian.domain.enums import Actor


def audit(
    session: Session,
    *,
    at: datetime,
    actor: Actor | str,
    entity_type: str,
    entity_id: int | str,
    action: str,
    from_state: str | None = None,
    to_state: str | None = None,
    reason: str | None = None,
    detail: dict[str, Any] | None = None,
    mode: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        at=at,
        actor=str(actor),
        entity_type=entity_type,
        entity_id=str(entity_id),
        action=action,
        from_state=from_state,
        to_state=to_state,
        reason=reason,
        detail_json=json.dumps(detail, sort_keys=True, default=str) if detail else None,
        mode=mode,
    )
    session.add(event)
    return event
