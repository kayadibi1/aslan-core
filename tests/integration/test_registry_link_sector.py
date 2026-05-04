from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import Actor, set_actor
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


async def test_link_idempotent_and_updates_weight(session: AsyncSession) -> None:
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    parent = await client.create_entity(
        type="fund",
        legal_name="P",
        identifiers={"tefas_founder_code": "ABC"},
    )
    child = await client.create_entity(
        type="fund",
        legal_name="C",
        identifiers={"tefas_code": "ABCDEF"},
    )
    await client.link(parent.entity_id, child.entity_id, "fund_of_founder")
    await client.link(parent.entity_id, child.entity_id, "fund_of_founder", weight=Decimal("0.5"))
    await session.commit()

    n = await session.scalar(
        text("SELECT count(*) FROM ref.entity_relationship WHERE parent_id = :p AND child_id = :c"),
        {"p": parent.entity_id, "c": child.entity_id},
    )
    assert n == 1
    w = await session.scalar(
        text("SELECT weight FROM ref.entity_relationship WHERE parent_id = :p AND child_id = :c"),
        {"p": parent.entity_id, "c": child.entity_id},
    )
    assert w == Decimal("0.5")


async def test_upsert_and_assign_sector(session: AsyncSession) -> None:
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    e = await client.create_entity(
        type="company",
        legal_name="Bank",
        identifiers={"kap_entity_code": "99"},
    )
    await client.upsert_sector(
        sector_id="bist:XBANK",
        taxonomy="bist",
        code="XBANK",
        name_tr="Bankalar",
    )
    await client.assign_sector(e.entity_id, "bist:XBANK", is_primary=True)
    await client.assign_sector(e.entity_id, "bist:XBANK", is_primary=True)  # idempotent
    await session.commit()
    n = await session.scalar(
        text("SELECT count(*) FROM ref.entity_sector WHERE entity_id = :eid"),
        {"eid": e.entity_id},
    )
    assert n == 1


async def test_upsert_sector_updates_existing(session: AsyncSession) -> None:
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    await client.upsert_sector(
        sector_id="bist:XBANK",
        taxonomy="bist",
        code="XBANK",
        name_tr="Old",
    )
    await client.upsert_sector(
        sector_id="bist:XBANK",
        taxonomy="bist",
        code="XBANK",
        name_tr="Bankalar",
        name_en="Banks",
    )
    await session.commit()
    row = (
        await session.execute(
            text("SELECT name_tr, name_en FROM ref.sector WHERE sector_id = 'bist:XBANK'")
        )
    ).one()
    assert row.name_tr == "Bankalar"
    assert row.name_en == "Banks"


# ─── codex F1 regression tests for Task 7 ─────────────────────────────


async def test_assign_sector_idempotent_hit_preserves_original_attribution(
    session: AsyncSession,
) -> None:
    """Codex F1, 2026-04-29: a second actor calling assign_sector with
    the same args must NOT rewrite the row's audit columns. Only the
    first call's audit identity persists on the row."""
    await _wipe(session)
    run = await _new_run(session)
    set_actor(Actor(actor_id="user:first", actor_kind="user"))
    client = EntityRegistryClient(session, ingestion_run_id=run)
    e = await client.create_entity(
        type="company", legal_name="Bank", identifiers={"kap_entity_code": "99"}
    )
    await client.upsert_sector(
        sector_id="bist:XBANK", taxonomy="bist", code="XBANK", name_tr="Bankalar"
    )
    await client.assign_sector(e.entity_id, "bist:XBANK", is_primary=True)
    await session.commit()

    set_actor(Actor(actor_id="user:retry", actor_kind="user"))
    await client.assign_sector(e.entity_id, "bist:XBANK", is_primary=True)
    await session.commit()

    # Row's audit cols still reflect the first writer.
    row = (
        await session.execute(
            text(
                "SELECT actor_id FROM ref.entity_sector "
                "WHERE entity_id = :eid AND sector_id = 'bist:XBANK'"
            ),
            {"eid": e.entity_id},
        )
    ).one()
    assert row.actor_id == "user:first"

    events = (
        await session.execute(
            text(
                "SELECT operation, actor_id FROM audit.events "
                "WHERE target_table = 'entity_sector' "
                "ORDER BY occurred_at"
            )
        )
    ).all()
    assert [e.operation for e in events] == [
        "entity_sector.upsert",
        "entity_sector.idempotent_hit",
    ]
    assert events[0].actor_id == "user:first"
    assert events[1].actor_id == "user:retry"

    set_actor(None)


async def test_upsert_sector_writes_audit_row_on_create(
    session: AsyncSession,
) -> None:
    """Codex Batch 2 F1, 2026-04-29: a fresh upsert_sector call MUST
    stamp the row's audit columns AND emit one sector.upsert audit
    event. The row is the canonical record of who first defined the
    sector taxonomy entry — its actor cols are written by the actor
    that first introduced this sector_id."""
    await _wipe(session)
    run = await _new_run(session)
    set_actor(Actor(actor_id="user:taxonomist", actor_kind="user"))
    client = EntityRegistryClient(session, ingestion_run_id=run)
    await client.upsert_sector(
        sector_id="bist:XBANK",
        taxonomy="bist",
        code="XBANK",
        name_tr="Bankalar",
    )
    await session.commit()

    row = (
        await session.execute(
            text("SELECT actor_id, actor_kind FROM ref.sector WHERE sector_id = 'bist:XBANK'")
        )
    ).one()
    assert row.actor_id == "user:taxonomist"
    assert row.actor_kind == "user"

    events = (
        await session.execute(
            text(
                "SELECT operation, actor_id FROM audit.events "
                "WHERE target_table = 'sector' "
                "ORDER BY occurred_at"
            )
        )
    ).all()
    assert [e.operation for e in events] == ["sector.upsert"]
    assert events[0].actor_id == "user:taxonomist"

    set_actor(None)


async def test_upsert_sector_idempotent_hit_preserves_attribution(
    session: AsyncSession,
) -> None:
    """Codex Batch 2 F1, 2026-04-29: two actors call upsert_sector with
    identical args; the row's actor_id MUST stay the FIRST actor.
    Events emitted: sector.upsert (first actor) + sector.idempotent_hit
    (second actor)."""
    await _wipe(session)
    run = await _new_run(session)
    set_actor(Actor(actor_id="user:first", actor_kind="user"))
    client = EntityRegistryClient(session, ingestion_run_id=run)
    await client.upsert_sector(
        sector_id="bist:XBANK",
        taxonomy="bist",
        code="XBANK",
        name_tr="Bankalar",
    )
    await session.commit()

    set_actor(Actor(actor_id="user:retry", actor_kind="user"))
    await client.upsert_sector(
        sector_id="bist:XBANK",
        taxonomy="bist",
        code="XBANK",
        name_tr="Bankalar",
    )
    await session.commit()

    # Row's denormalised audit cols stay frozen on the first writer.
    row = (
        await session.execute(
            text("SELECT actor_id FROM ref.sector WHERE sector_id = 'bist:XBANK'")
        )
    ).one()
    assert row.actor_id == "user:first"

    events = (
        await session.execute(
            text(
                "SELECT operation, actor_id FROM audit.events "
                "WHERE target_table = 'sector' "
                "ORDER BY occurred_at"
            )
        )
    ).all()
    assert [e.operation for e in events] == [
        "sector.upsert",
        "sector.idempotent_hit",
    ]
    assert events[0].actor_id == "user:first"
    assert events[1].actor_id == "user:retry"

    set_actor(None)


async def test_upsert_sector_field_change_is_fresh_write(
    session: AsyncSession,
) -> None:
    """Codex Batch 2 F1, 2026-04-29: a second upsert with a CHANGED
    field (e.g. name_en) is a fresh write — last-writer-wins on the
    row's audit cols. Events: sector.upsert + sector.upsert."""
    await _wipe(session)
    run = await _new_run(session)
    set_actor(Actor(actor_id="user:first", actor_kind="user"))
    client = EntityRegistryClient(session, ingestion_run_id=run)
    await client.upsert_sector(
        sector_id="bist:XBANK",
        taxonomy="bist",
        code="XBANK",
        name_tr="Bankalar",
    )
    await session.commit()

    set_actor(Actor(actor_id="user:editor", actor_kind="user"))
    await client.upsert_sector(
        sector_id="bist:XBANK",
        taxonomy="bist",
        code="XBANK",
        name_tr="Bankalar",
        name_en="Banks",
    )
    await session.commit()

    row = (
        await session.execute(
            text("SELECT actor_id, name_en FROM ref.sector WHERE sector_id = 'bist:XBANK'")
        )
    ).one()
    assert row.actor_id == "user:editor"
    assert row.name_en == "Banks"

    events = (
        await session.execute(
            text(
                "SELECT operation, actor_id FROM audit.events "
                "WHERE target_table = 'sector' "
                "ORDER BY occurred_at"
            )
        )
    ).all()
    assert [e.operation for e in events] == ["sector.upsert", "sector.upsert"]
    assert events[0].actor_id == "user:first"
    assert events[1].actor_id == "user:editor"

    set_actor(None)


async def test_link_idempotent_hit_preserves_original_attribution(
    session: AsyncSession,
) -> None:
    """Codex F1, 2026-04-29: a second actor calling link with identical
    fields must NOT rewrite the row's audit columns. A subsequent call
    that DOES change a field (weight) is a fresh write — last-writer
    wins on the row's audit cols, recorded as entity_relationship.link."""
    await _wipe(session)
    run = await _new_run(session)
    set_actor(Actor(actor_id="user:first", actor_kind="user"))
    client = EntityRegistryClient(session, ingestion_run_id=run)
    parent = await client.create_entity(
        type="fund", legal_name="P", identifiers={"tefas_founder_code": "ABC"}
    )
    child = await client.create_entity(
        type="fund", legal_name="C", identifiers={"tefas_code": "ABCDEF"}
    )
    await client.link(parent.entity_id, child.entity_id, "fund_of_founder")
    await session.commit()

    # Idempotent retry — same args, different actor.
    set_actor(Actor(actor_id="user:retry", actor_kind="user"))
    await client.link(parent.entity_id, child.entity_id, "fund_of_founder")
    await session.commit()
    row = (
        await session.execute(
            text(
                "SELECT actor_id FROM ref.entity_relationship "
                "WHERE parent_id = :p AND child_id = :c"
            ),
            {"p": parent.entity_id, "c": child.entity_id},
        )
    ).one()
    assert row.actor_id == "user:first"  # frozen on first writer

    # Now actually mutate (different weight) — fresh write, last-writer
    # wins on the row's audit cols.
    set_actor(Actor(actor_id="user:mutator", actor_kind="user"))
    await client.link(parent.entity_id, child.entity_id, "fund_of_founder", weight=Decimal("0.5"))
    await session.commit()
    row2 = (
        await session.execute(
            text(
                "SELECT actor_id, weight FROM ref.entity_relationship "
                "WHERE parent_id = :p AND child_id = :c"
            ),
            {"p": parent.entity_id, "c": child.entity_id},
        )
    ).one()
    assert row2.actor_id == "user:mutator"
    assert row2.weight == Decimal("0.5")

    events = (
        await session.execute(
            text(
                "SELECT operation, actor_id FROM audit.events "
                "WHERE target_table = 'entity_relationship' "
                "ORDER BY occurred_at"
            )
        )
    ).all()
    assert [e.operation for e in events] == [
        "entity_relationship.link",
        "entity_relationship.idempotent_hit",
        "entity_relationship.link",
    ]
    assert events[0].actor_id == "user:first"
    assert events[1].actor_id == "user:retry"
    assert events[2].actor_id == "user:mutator"

    set_actor(None)
