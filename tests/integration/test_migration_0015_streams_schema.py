"""Migration 0015 — streams schema (v0.5.0 Task 3)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


@pytest.mark.asyncio(loop_scope="session")
async def test_streams_schema_exists(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'streams'"
            )
        )
        rows = result.all()
    assert len(rows) == 1, "expected streams schema to exist"


@pytest.mark.asyncio(loop_scope="session")
async def test_streams_schema_owned_by_migration_user(engine: AsyncEngine) -> None:
    """Sanity-check the schema is owned by the test container's role; the
    ``aslan_app`` role created in migration 0018 is NOT the owner — it is
    a grantee."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT schema_owner FROM information_schema.schemata WHERE schema_name = 'streams'"
            )
        )
        owner = result.scalar_one()
    assert owner is not None
    assert owner != "aslan_app"
