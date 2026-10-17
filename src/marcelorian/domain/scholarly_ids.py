"""Canonical ORCID iDs and OpenAlex author IDs (spec §3 identity basis; decision C7).

Identity matching compares these exactly, so every stored value has one canonical
form, like addresses: the same identifier written as a URL, with spaces, without
hyphens or with a lower-case check character must never become a different
identity. Malformed values are rejected rather than stored.
"""

from __future__ import annotations

import re


class IdentifierError(ValueError):
    pass


_ORCID_PREFIX = re.compile(r"^(?:https?://)?(?:www\.)?orcid\.org/", re.IGNORECASE)
_ORCID_HYPHENATED = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", re.ASCII)
_ORCID_BARE = re.compile(r"\d{15}[\dX]", re.ASCII)
_OPENALEX_PREFIX = re.compile(
    r"^(?:https?://)?(?:api\.)?openalex\.org/(?:authors/)?", re.IGNORECASE
)
_OPENALEX_AUTHOR = re.compile(r"A\d{1,15}", re.ASCII)


def orcid_check_character(base_digits: str) -> str:
    """ISO 7064 MOD 11-2 check character for the first 15 digits of an ORCID iD."""
    total = 0
    for digit in base_digits:
        total = (total + int(digit)) * 2
    result = (12 - total % 11) % 11
    return "X" if result == 10 else str(result)


def canonical_orcid(value: str) -> str:
    """Return ``0000-0000-0000-000X`` form or raise ``IdentifierError``."""
    text = value.strip()
    if not text.isascii():
        raise IdentifierError("not an ORCID iD")
    text = _ORCID_PREFIX.sub("", text).strip().upper()
    if _ORCID_BARE.fullmatch(text):
        text = "-".join((text[0:4], text[4:8], text[8:12], text[12:16]))
    if not _ORCID_HYPHENATED.fullmatch(text):
        raise IdentifierError("not an ORCID iD")
    digits = text.replace("-", "")
    if orcid_check_character(digits[:15]) != digits[15]:
        raise IdentifierError("ORCID iD check character does not match")
    return text


def canonical_openalex_author_id(value: str) -> str:
    """Return the ``A123...`` author ID or raise ``IdentifierError``.

    Only author IDs are accepted: a work, institution or source ID is never a PI.
    """
    text = value.strip()
    if not text.isascii():
        raise IdentifierError("not an OpenAlex author ID")
    text = _OPENALEX_PREFIX.sub("", text).strip().upper()
    if not _OPENALEX_AUTHOR.fullmatch(text):
        raise IdentifierError("not an OpenAlex author ID")
    return text
