from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import Actor, set_actor
from aslan_core.errors import EntityMergeRequired
from aslan_core.registry.client import EntityRegistryClient

pytestmark = pytest.mark.integration


async def _wipe(session: AsyncSession) -> None:
    for stmt in [
        "DELETE FROM audit.events",
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


async def _new_run(session: AsyncSession, source_id: str = "kap", job: str = "seed") -> int:
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
                "VALUES (:sid, :job, 'succeeded') RETURNING ingestion_run_id"
            ),
            {"sid": source_id, "job": job},
        )
    ).scalar_one()
    await session.commit()
    return run_id


async def test_create_entity_inserts_with_identifiers(session: AsyncSession) -> None:
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    e = await client.create_entity(
        type="company",
        legal_name="Aselsan A.Ş.",
        identifiers={"kap_entity_code": "19387", "bist_ticker": "ASELS"},
    )
    await session.commit()
    assert e.legal_name == "Aselsan A.Ş."
    assert await client.resolve("bist_ticker", "ASELS") == e.entity_id


async def test_create_entity_idempotent_same_source(session: AsyncSession) -> None:
    await _wipe(session)
    run = await _new_run(session)
    client = EntityRegistryClient(session, ingestion_run_id=run)
    e1 = await client.create_entity(
        type="company",
        legal_name="Aselsan A.Ş.",
        identifiers={"kap_entity_code": "19387"},
    )
    await session.commit()

    run2 = await _new_run(session, source_id="kap", job="seed_again")
    client2 = EntityRegistryClient(session, ingestion_run_id=run2)
    e2 = await client2.create_entity(
        type="company",
        legal_name="Aselsan A.Ş.",
        identifiers={"kap_entity_code": "19387", "bist_ticker": "ASELS"},
    )
    await session.commit()
    assert e1.entity_id == e2.entity_id
    assert await client2.resolve("bist_ticker", "ASELS") == e2.entity_id


async def test_create_entity_cross_source_match_raises(session: AsyncSession) -> None:
    await _wipe(session)
    run_kap = await _new_run(session, source_id="kap")
    client_kap = EntityRegistryClient(session, ingestion_run_id=run_kap)
    await client_kap.create_entity(
        type="company",
        legal_name="Aselsan A.Ş.",
        identifiers={"bist_ticker": "ASELS"},
    )
    await session.commit()

    run_manual = await _new_run(session, source_id="manual")
    client_manual = EntityRegistryClient(session, ingestion_run_id=run_manual)
    with pytest.raises(EntityMergeRequired):
        await client_manual.create_entity(
            type="company",
            legal_name="Aselsan A.Ş.",
            identifiers={"bist_ticker": "ASELS"},
        )


async def test_create_entity_multi_entity_match_raises(session: AsyncSession) -> None:
    """Two identifiers each currently resolving to a *different* entity."""
    await _wipe(session)
    run = await _new_run(session, source_id="kap")
    client = EntityRegistryClient(session, ingestion_run_id=run)
    await client.create_entity(
        type="company",
        legal_name="Foo A.Ş.",
        identifiers={"kap_entity_code": "1", "bist_ticker": "FOO"},
    )
    await client.create_entity(
        type="company",
        legal_name="Bar A.Ş.",
        identifiers={"kap_entity_code": "2", "bist_ticker": "BAR"},
    )
    await session.commit()

    with pytest.raises(EntityMergeRequired):
        await client.create_entity(
            type="company",
            legal_name="Foo or Bar?",
            identifiers={"bist_ticker": "FOO", "kap_entity_code": "2"},
        )


async def test_create_entity_writes_audit_row_and_stamps_row_audit_cols(
    session: AsyncSession,
) -> None:
    """Fresh-create path: one ``entity.create`` event AND the row's own
    audit columns reflect the actor — both written in the same INSERT
    (no separate post-write UPDATE)."""
    await _wipe(session)
    run = await _new_run(session)
    set_actor(Actor(actor_id="user:audit-test", actor_kind="user"))
    client = EntityRegistryClient(session, ingestion_run_id=run)

    e = await client.create_entity(
        type="company",
        legal_name="X",
        identifiers={"kap_entity_code": "1"},
    )
    await session.commit()

    rows = (
        await session.execute(
            text(
                "SELECT operation, target_pk, after, actor_id "
                "FROM audit.events "
                "WHERE target_schema = 'ref' AND target_table = 'entity' "
                "ORDER BY occurred_at"
            )
        )
    ).all()
    assert len(rows) == 1
    r = rows[0]
    assert r.operation == "entity.create"
    assert r.target_pk == {"entity_id": str(e.entity_id)}
    assert r.after["legal_name"] == "X"
    assert r.actor_id == "user:audit-test"

    # And the row's own denormalised audit columns reflect the actor.
    e_row = (
        await session.execute(
            text("SELECT actor_id, actor_kind FROM ref.entity WHERE entity_id = :id"),
            {"id": e.entity_id},
        )
    ).one()
    assert e_row.actor_id == "user:audit-test"
    assert e_row.actor_kind == "user"

    set_actor(None)


async def test_create_entity_idempotent_hit_preserves_original_attribution(
    session: AsyncSession,
) -> None:
    """Codex F1, 2026-04-29: an idempotent retry by a different actor
    must NOT rewrite the row's audit columns AND must NOT emit
    ``entity.create``. It emits ``entity.idempotent_hit`` whose event row
    carries the new actor's identity, while the entity row's denormalised
    audit columns stay frozen on the original creator."""
    await _wipe(session)
    run = await _new_run(session)

    set_actor(Actor(actor_id="user:original-creator", actor_kind="user"))
    client_a = EntityRegistryClient(session, ingestion_run_id=run)
    e1 = await client_a.create_entity(
        type="company",
        legal_name="X",
        identifiers={"kap_entity_code": "1"},
    )
    await session.commit()

    # Second actor retries the same call — idempotent hit.
    set_actor(Actor(actor_id="user:retry-actor", actor_kind="user"))
    client_b = EntityRegistryClient(session, ingestion_run_id=run)
    e2 = await client_b.create_entity(
        type="company",
        legal_name="X",
        identifiers={"kap_entity_code": "1"},
    )
    await session.commit()

    assert e1.entity_id == e2.entity_id

    # The row's actor_id is STILL the original creator, not the retrier.
    row = (
        await session.execute(
            text("SELECT actor_id FROM ref.entity WHERE entity_id = :id"),
            {"id": e1.entity_id},
        )
    ).one()
    assert row.actor_id == "user:original-creator"

    # Two audit events on ref.entity: one entity.create, one entity.idempotent_hit.
    events = (
        await session.execute(
            text(
                "SELECT operation, actor_id FROM audit.events "
                "WHERE target_schema = 'ref' AND target_table = 'entity' "
                "ORDER BY occurred_at"
            )
        )
    ).all()
    assert [e.operation for e in events] == [
        "entity.create",
        "entity.idempotent_hit",
    ]
    assert events[0].actor_id == "user:original-creator"
    assert events[1].actor_id == "user:retry-actor"

    set_actor(None)


async def test_create_entity_laundered_cross_source_match_raises(
    session: AsyncSession,
) -> None:
    """Source B can't launder a cross-source merge by attaching its own
    identifier to source A's entity first.

    Spec §2: auto-merge is intentionally same-source only. The trust
    boundary is the *entity's* source, not just the identifier row's source.
    """
    await _wipe(session)

    run_kap = await _new_run(session, source_id="kap")
    client_kap = EntityRegistryClient(session, ingestion_run_id=run_kap)
    entity_kap = await client_kap.create_entity(
        type="company",
        legal_name="Aselsan A.Ş.",
        identifiers={"kap_entity_code": "19387"},
    )
    await session.commit()

    # Source `manual` attaches its own identifier to source `kap`'s entity.
    # add_identifier(...) is intentionally permissive (operator path), so
    # this succeeds and writes an identifier row owned by source `manual`.
    run_manual_attach = await _new_run(session, source_id="manual", job="attach")
    await EntityRegistryClient(session, run_manual_attach).add_identifier(
        entity_kap.entity_id, "bist_ticker", "ASELS"
    )
    await session.commit()

    # Now source `manual` calls create_entity with that same identifier.
    # The identifier row's source_id matches `manual`, but the target entity
    # was created by `kap`. This must still raise.
    run_manual_create = await _new_run(session, source_id="manual", job="create")
    client_manual = EntityRegistryClient(session, run_manual_create)
    with pytest.raises(EntityMergeRequired):
        await client_manual.create_entity(
            type="company",
            legal_name="Aselsan",
            identifiers={"bist_ticker": "ASELS"},
        )
