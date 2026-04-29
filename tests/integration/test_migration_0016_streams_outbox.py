"""Migration 0016 — streams.outbox table + indexes (v0.5.0 Task 4)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(loop_scope="session")
async def _seed_test_ingestion_run(session: AsyncSession) -> AsyncIterator[None]:
    """Ensures at least one ``src.ingestion_run`` row exists for FK
    references in this module's tests."""
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('kap', 'KAP test', 'scraper', 'open') ON CONFLICT DO NOTHING"
        )
    )
    await session.execute(
        text(
            "INSERT INTO src.ingestion_run (source_id, job_name, started_at, status) "
            "VALUES ('kap', 'test', now(), 'success') ON CONFLICT DO NOTHING"
        )
    )
    await session.commit()
    yield


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_columns(engine: AsyncEngine) -> None:
    expected = {
        "outbox_id": "bigint",
        "stream_name": "text",
        "event_id": "uuid",
        "schema_version": "integer",
        "payload": "jsonb",
        "producer_run_id": "bigint",
        "source_id": "text",
        "created_at": "timestamp with time zone",
        "published_at": "timestamp with time zone",
        "redis_message_id": "text",
        "publish_attempts": "integer",
        "last_attempt_at": "timestamp with time zone",
        "last_error": "text",
        "actor_id": "text",
        "actor_kind": "text",
        "client_ip": "inet",
        "user_agent": "text",
        "request_id": "uuid",
    }
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema='streams' AND table_name='outbox'"
                )
            )
        ).all()
    actual: dict[str, str] = {r[0]: r[1] for r in rows}
    for name, dtype in expected.items():
        assert name in actual, f"missing column {name}"
        assert actual[name] == dtype, f"{name}: {actual[name]} != {dtype}"


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_event_id_unique(
    engine: AsyncEngine,
    _seed_test_ingestion_run: None,
) -> None:
    """UNIQUE constraint on event_id is the dedup anchor (codex spec §2)."""
    eid = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO streams.outbox "
                "(stream_name, event_id, schema_version, payload, "
                " producer_run_id, source_id) "
                "VALUES ('s', :eid, 1, '{}'::jsonb, "
                "        (SELECT ingestion_run_id FROM src.ingestion_run LIMIT 1), "
                "        'kap')"
            ),
            {"eid": str(eid)},
        )
    with pytest.raises(Exception, match=r"duplicate key|unique"):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO streams.outbox "
                    "(stream_name, event_id, schema_version, payload, "
                    " producer_run_id, source_id) "
                    "VALUES ('s', :eid, 1, '{}'::jsonb, "
                    "        (SELECT ingestion_run_id FROM src.ingestion_run LIMIT 1), "
                    "        'kap')"
                ),
                {"eid": str(eid)},
            )


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_pending_partial_index(engine: AsyncEngine) -> None:
    """The partial index keeps the drainer scan cheap when the table grows
    to millions of rows."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname='streams' AND indexname='outbox_pending'"
            )
        )
        defn = result.scalar_one()
    assert "WHERE" in defn.upper()
    assert "published_at" in defn


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_indexes_present(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT indexname FROM pg_indexes WHERE schemaname='streams' AND tablename='outbox'"
            )
        )
        names = {r[0] for r in result.all()}
    expected = {"outbox_pending", "outbox_event_id", "outbox_stream_name", "outbox_run"}
    missing = expected - names
    assert not missing, f"missing indexes: {missing}"


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_actor_kind_check_constraint(
    engine: AsyncEngine,
    _seed_test_ingestion_run: None,
) -> None:
    """v0.3 audit-cols contract: ``actor_kind`` IN ('user','service','system')."""
    with pytest.raises(Exception, match=r"check|actor_kind"):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO streams.outbox "
                    "(stream_name, event_id, schema_version, payload, "
                    " producer_run_id, source_id, actor_kind) "
                    "VALUES ('s', :eid, 1, '{}'::jsonb, "
                    "        (SELECT ingestion_run_id FROM src.ingestion_run LIMIT 1), "
                    "        'kap', 'banana')"
                ),
                {"eid": str(uuid4())},
            )


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_event_id_not_null(engine: AsyncEngine) -> None:
    """``event_id`` is the dedup anchor and must be NOT NULL."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema='streams' AND table_name='outbox' "
                "AND column_name='event_id'"
            )
        )
        nullable = result.scalar_one()
    assert nullable == "NO"


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_payload_not_null(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema='streams' AND table_name='outbox' "
                "AND column_name='payload'"
            )
        )
        nullable = result.scalar_one()
    assert nullable == "NO"
