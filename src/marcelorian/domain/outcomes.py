"""Classification of ``messages.send`` outcomes (spec §9.6 steps 4-6).

Only responses that prove Gmail did *not* accept the request are definite
rejections. Everything else that is not a success - timeouts, connection
loss, 5xx, unknown statuses - is ambiguous and goes to reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class OutcomeKind(StrEnum):
    DEFINITE_REJECTION = "definite_rejection"
    AMBIGUOUS = "ambiguous"


#: HTTP statuses that prove the request was refused before acceptance.
DEFINITE_REJECTION_STATUSES: frozenset[int] = frozenset({400, 401, 403, 404, 413, 429})
_AUTH_REASONS = frozenset({"autherror", "insufficientpermissions", "invalidcredentials"})


@dataclass(frozen=True)
class Classified:
    kind: OutcomeKind
    auth_failure: bool


def classify_send_error(status: int | None, reason: str | None = None) -> Classified:
    """``status`` is None when no HTTP response was received at all."""
    if status is None or status not in DEFINITE_REJECTION_STATUSES:
        return Classified(OutcomeKind.AMBIGUOUS, auth_failure=False)
    normalized = (reason or "").replace("_", "").lower()
    auth = status == 401 or (status == 403 and normalized in _AUTH_REASONS)
    return Classified(OutcomeKind.DEFINITE_REJECTION, auth_failure=auth)
