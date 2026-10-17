"""Specialty configuration loader (spec §5).

P0 needs only the display name (used in the fixed subject). The MeSH terms,
queries, NEI filter, seed departments and title vocabulary are added in P1.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources

import yaml


@dataclass(frozen=True)
class Specialty:
    key: str
    display_name: str


def load_specialty(key: str = "ophthalmology") -> Specialty:
    text = resources.files("marcelorian.specialties").joinpath(f"{key}.yaml").read_text("utf-8")
    data = yaml.safe_load(text)
    display = str(data["display_name"]).strip()
    if not display:
        raise ValueError("specialty display_name must not be empty")
    return Specialty(key=key, display_name=display)
