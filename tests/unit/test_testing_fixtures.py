from __future__ import annotations


def test_object_storage_fake_fixture(object_storage_fake) -> None:  # type: ignore[no-untyped-def]
    """Fixture is reachable + each test gets a fresh instance."""
    from aslan_core.documents.object_storage import InMemoryFake

    assert isinstance(object_storage_fake, InMemoryFake)
    assert object_storage_fake.all_keys() == set()
