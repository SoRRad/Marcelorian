"""Optional lookup keys and the NCBI contact address (in-memory secret store only)."""

from __future__ import annotations

import pytest

from marcelorian.db.engine import transaction
from marcelorian.lookups import enrich
from marcelorian.lookups import keys as lookup_keys
from marcelorian.platform.secrets import SecretStoreUnavailable
from tests.fakes.secrets import InMemorySecretStore

NCBI_KEY = "0123456789abcdef0123456789abcdef0123"
OPENALEX_KEY = "synthetic-openalex-key-0000"


def test_nothing_saved_means_keyless_without_opening_the_store(world) -> None:
    def never():
        raise AssertionError("the credential store must not be opened")

    assert lookup_keys.load(world.factory, never) is enrich.NO_KEYS


def test_keys_are_stored_in_the_credential_store_and_loaded(world) -> None:
    store = InMemorySecretStore()
    lookup_keys.check_and_store(store, "ncbi", f"  {NCBI_KEY}\n")
    lookup_keys.check_and_store(store, "openalex", OPENALEX_KEY)
    with transaction(world.factory) as s:
        lookup_keys.record(s, "ncbi", True, now=world.clock.now())
        lookup_keys.record(s, "openalex", True, now=world.clock.now())
        lookup_keys.set_contact_email(s, "owner@example.org", now=world.clock.now())
        assert lookup_keys.saved(s) == {"ncbi": True, "openalex": True}
    assert store.get("ncbi_api_key") == NCBI_KEY
    assert lookup_keys.load(world.factory, lambda: store) == enrich.Keys(
        ncbi=NCBI_KEY, openalex=OPENALEX_KEY, ncbi_email="owner@example.org"
    )
    with transaction(world.factory) as s:
        lookup_keys.record(s, "openalex", False, now=world.clock.now())
    assert lookup_keys.load(world.factory, lambda: store).openalex == ""


@pytest.mark.parametrize("value", ["", "short", "has spaces in the middle of it", "x" * 201])
def test_malformed_keys_are_refused(value: str) -> None:
    store = InMemorySecretStore()
    with pytest.raises(lookup_keys.KeyInputError):
        lookup_keys.check_and_store(store, "ncbi", value)
    with pytest.raises(lookup_keys.KeyInputError):
        lookup_keys.check_and_store(store, "other", NCBI_KEY)


def test_contact_email_is_checked_and_can_be_cleared(world) -> None:
    with transaction(world.factory) as s:
        with pytest.raises(lookup_keys.KeyInputError):
            lookup_keys.set_contact_email(s, "not an address", now=world.clock.now())
        lookup_keys.set_contact_email(s, "owner@example.org", now=world.clock.now())
        lookup_keys.set_contact_email(s, "", now=world.clock.now())
        assert lookup_keys.contact_email(s) == ""


def test_an_unavailable_store_falls_back_to_keyless(world) -> None:
    with transaction(world.factory) as s:
        lookup_keys.record(s, "ncbi", True, now=world.clock.now())
        lookup_keys.set_contact_email(s, "owner@example.org", now=world.clock.now())

    def broken():
        raise SecretStoreUnavailable("no store")

    assert lookup_keys.load(world.factory, broken) == enrich.Keys(ncbi_email="owner@example.org")
