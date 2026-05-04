from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import Actor, set_actor
from aslan_core.errors import EntityNotFound, IdentifierConflict
from aslan_core.registry.client import EntityRegistryClient

pytestmark = pytest.mark.integration


async def _wipe(session: AsyncSession) -> None:
    for stmt in [
        "DELETE FROM audit.events",
        "DELETE FROM ts.entity_quality_score",
        "DELETE FROM ts.canonical_financial",
        "DELETE FROM ts.financial_line_item",
        "DELETE FROM ref.entity_sector",
        "DELETE FROM ref.entity_relationship",
        "DELETE FROM ref.identifier",
        "DELETE FROM ref.entity",
        "DELETE FROM ref.sector",
        "DELETE FROM doc.filing_body",
        "DELETE FROM doc.filing_attachment",
        "DELETE FROM doc.filing",
        "DELETE FROM src.ingestion_run",
        "DELETE FROM src.source",
    ]:
        await session.execute(text(stmt))
    await session.commit()


async def _new_run(session: AsyncSession, source_id: str = "kap") -> int:
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES (:sid, :sid, 'scraper', 'open') ON CONFLICT DO NOTHING"
        ),
        {"sid": source_id},
    )
    run_id: int = (
        await session.execute(
            text(
                "INSERT INTO src.ingestion_run (source_id, job_name, status) "
                "VALUES (:sid, 'job', 'succeeded') RETURNING ingestion_run_id"
            ),
            {"sid": source_id},
        )
    ).scalar_one()
    await session.commit()
    return run_id


async def test_add_identifier_idempotent(session: AsyncSession) -> None:
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    e = await client.create_entity(
        type="company",
        legal_name="X",
        identifiers={"kap_entity_code": "1"},
    )
    await session.commit()
    await client.add_identifier(e.entity_id, "bist_ticker", "X")
    await client.add_identifier(e.entity_id, "bist_ticker", "X")  # no-op
    await session.commit()


async def test_add_identifier_collision_raises(session: AsyncSession) -> None:
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    a = await client.create_entity(
        type="company", legal_name="A", identifiers={"kap_entity_code": "1"}
    )
    b = await client.create_entity(
        type="company", legal_name="B", identifiers={"kap_entity_code": "2"}
    )
    await session.commit()
    assert a.entity_id != b.entity_id
    with pytest.raises(IdentifierConflict):
        await client.add_identifier(b.entity_id, "kap_entity_code", "1")


async def test_expire_identifier_makes_resolution_return_none(session: AsyncSession) -> None:
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    e = await client.create_entity(
        type="company",
        legal_name="X",
        identifiers={"bist_ticker": "OLD"},
    )
    await session.commit()
    await client.expire_identifier("bist_ticker", "OLD", as_of=date(2026, 4, 27))
    await session.commit()
    # On expiry day itself: gone (FIRST INVALID DAY)
    assert await client.resolve("bist_ticker", "OLD", as_of=date(2026, 4, 27)) is None
    # The day before: still resolves
    assert await client.resolve("bist_ticker", "OLD", as_of=date(2026, 4, 26)) == e.entity_id


async def test_expire_identifier_raises_when_no_active(session: AsyncSession) -> None:
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    with pytest.raises(EntityNotFound):
        await client.expire_identifier("bist_ticker", "GHOST", as_of=date(2026, 4, 27))


async def test_update_entity_partial_patch(session: AsyncSession) -> None:
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    e = await client.create_entity(
        type="company", legal_name="X", identifiers={"kap_entity_code": "9"}
    )
    await session.commit()
    e2 = await client.update_entity(e.entity_id, short_name="Xco", status="suspended")
    await session.commit()
    assert e2.short_name == "Xco"
    assert e2.status == "suspended"
    assert e2.legal_name == "X"  # unchanged


async def test_identifier_transfer_ticker_rename(session: AsyncSession) -> None:
    """Spec §10 v0.1 invariant: a ticker can be reassigned to a different
    entity. Expire the active row at as_of=D, then add a new row for the
    same (namespace, value) on the new entity with valid_from=D.

    Half-open interval [valid_from, valid_to) means the two rows touch
    without overlapping, so the GiST exclusion permits both.
    """
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)

    a = await client.create_entity(
        type="company", legal_name="OldCo", identifiers={"bist_ticker": "X"}
    )
    b = await client.create_entity(
        type="company", legal_name="NewCo", identifiers={"kap_entity_code": "B-CODE"}
    )
    await session.commit()

    rename_dt = date(2026, 4, 27)
    await client.expire_identifier("bist_ticker", "X", as_of=rename_dt)
    await client.add_identifier(b.entity_id, "bist_ticker", "X", valid_from=rename_dt)
    await session.commit()

    # Day before the rename: still resolves to the old entity.
    assert await client.resolve("bist_ticker", "X", as_of=date(2026, 4, 26)) == a.entity_id
    # On the rename day (FIRST INVALID DAY for OldCo, valid_from for NewCo): NewCo.
    assert await client.resolve("bist_ticker", "X", as_of=rename_dt) == b.entity_id
    # After the rename: NewCo.
    assert await client.resolve("bist_ticker", "X", as_of=date(2026, 4, 28)) == b.entity_id


# ─── codex F1 regression tests for Task 7 ─────────────────────────────


async def test_add_identifier_idempotent_hit_preserves_original_attribution(
    session: AsyncSession,
) -> None:
    """Codex F1, 2026-04-29: a second actor calling add_identifier with
    the same (namespace, value, valid_from) already pointing at the
    target entity must NOT rewrite the row's audit columns. The retry
    emits identifier.idempotent_hit; the row's actor_id stays frozen on
    the first writer."""
    await _wipe(session)
    run = await _new_run(session)
    set_actor(Actor(actor_id="user:first", actor_kind="user"))
    client = EntityRegistryClient(session, ingestion_run_id=run)
    e = await client.create_entity(
        type="company", legal_name="X", identifiers={"kap_entity_code": "1"}
    )
    await session.commit()

    set_actor(Actor(actor_id="user:second", actor_kind="user"))
    await client.add_identifier(e.entity_id, "bist_ticker", "X")
    await session.commit()
    set_actor(Actor(actor_id="user:retrier", actor_kind="user"))
    await client.add_identifier(e.entity_id, "bist_ticker", "X")  # idempotent retry
    await session.commit()

    # Row's denormalised audit cols reflect the FIRST writer (user:second).
    row = (
        await session.execute(
            text(
                "SELECT actor_id FROM ref.identifier "
                "WHERE namespace = 'bist_ticker' AND value = 'X'"
            )
        )
    ).one()
    assert row.actor_id == "user:second"

    bist_events = (
        await session.execute(
            text(
                "SELECT operation, actor_id FROM audit.events e "
                "WHERE e.target_table = 'identifier' "
                "  AND (e.before->>'value' = 'X' OR e.after->>'value' = 'X') "
                "ORDER BY occurred_at"
            )
        )
    ).all()
    assert [r.operation for r in bist_events] == [
        "identifier.add",
        "identifier.idempotent_hit",
    ]
    assert bist_events[0].actor_id == "user:second"
    assert bist_events[1].actor_id == "user:retrier"

    set_actor(None)


async def test_update_entity_writes_audit_with_before_and_after(
    session: AsyncSession,
) -> None:
    """update_entity is always a fresh write — last-writer-wins on the
    row's audit cols and a single entity.update event with both
    before/after snapshots."""
    await _wipe(session)
    run = await _new_run(session)
    set_actor(Actor(actor_id="user:creator", actor_kind="user"))
    client = EntityRegistryClient(session, ingestion_run_id=run)
    e = await client.create_entity(
        type="company", legal_name="X", identifiers={"kap_entity_code": "1"}
    )
    await session.commit()

    set_actor(Actor(actor_id="user:updater", actor_kind="user"))
    await client.update_entity(e.entity_id, short_name="Xco")
    await session.commit()

    # Last-writer-wins on the row's audit cols.
    row = (
        await session.execute(
            text("SELECT actor_id FROM ref.entity WHERE entity_id = :eid"),
            {"eid": e.entity_id},
        )
    ).one()
    assert row.actor_id == "user:updater"

    events = (
        await session.execute(
            text(
                "SELECT operation, actor_id, before, after FROM audit.events "
                "WHERE target_table = 'entity' AND operation = 'entity.update'"
            )
        )
    ).all()
    assert len(events) == 1
    ev = events[0]
    assert ev.actor_id == "user:updater"
    assert ev.before["short_name"] is None
    assert ev.after["short_name"] == "Xco"

    set_actor(None)
