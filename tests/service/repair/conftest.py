"""Shared fixtures for the P0 repair regression tests (M01-M06)."""

from __future__ import annotations

import pytest

from marcelorian.domain.enums import Mode


@pytest.fixture
def live(world):
    """A world with the synthetic applicant mailbox connected and live mode active."""
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    return world
