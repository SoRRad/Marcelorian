"""Fixed subject lines (spec §1, decision C5). Subjects are never personalized."""

from __future__ import annotations

import re

EN_DASH = "–"
_REPLY_PREFIX = re.compile(r"^\s*(?:re\s*:\s*)+", re.IGNORECASE)


def initial_subject(display_name: str) -> str:
    name = display_name.strip()
    if not name:
        raise ValueError("specialty display name must not be empty")
    return f"Research Position Application {EN_DASH} {name}"


def normalize_reply_subject(subject: str) -> str:
    """Exactly one ``Re: `` prefix, however many reply prefixes were present."""
    return "Re: " + _REPLY_PREFIX.sub("", subject).strip()


def expected_subject(step: int, display_name: str) -> str:
    if step == 1:
        return initial_subject(display_name)
    if step in (2, 3, 4):
        return normalize_reply_subject(initial_subject(display_name))
    raise ValueError("steps are 1-4")
