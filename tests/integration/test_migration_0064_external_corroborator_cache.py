"""Migration 0064 — audit.external_corroborator_cache.

Asserts the table shape, the fetch_status CHECK constraint, the
(source, entity_ticker, fetched_at DESC) index, and the dashboard role's
SELECT-only privilege boundary.

NG6 (workspace CLAUDE.md autonomy directive): backs the firecrawl
external-corroborator panel on /dq/spot-check/<sample_id>. The cache
is append-only — UPDATE is never granted to any role; refresh writes a
new row.
"""

from __future__ import annotations

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def test_external_corroborator_cache_columns(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        cols = (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'audit' "
                    "  AND table_name = 'external_corroborator_cache'"
                )
            )
        ).all()
    names = {c.column_name for c in cols}
    assert {
        "cache_id",
        "source",
        "entity_ticker",
        "fetched_at",
        "cached_payload",
        "fetch_url",
        "fetch_latency_ms",
        "fetch_status",
        "error_summary",
        "recorded_at",
    } == names, f"unexpected columns: {names}"


async def test_fetch_status_check_constraint(engine: AsyncEngine) -> None:
    """Only the four canonical fetch_status values are accepted."""
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM audit.external_corroborator_cache"))
        # valid inserts cover every allowed status
        for status in ("ok", "error", "rate_limited", "blocked"):
            await conn.execute(
                text(
                    "INSERT INTO audit.external_corroborator_cache("
                    "  source, entity_ticker, fetched_at, cached_payload, "
                    "  fetch_url, fetch_latency_ms, fetch_status"
                    ") VALUES ("
                    "  :src, 'AKBNK', now(), CAST(:pl AS JSONB), "
                    "  'https://example/x', 100, :st"
                    ")"
                ),
                {"src": f"investing_com_{status}", "pl": "{}", "st": status},
            )
    # invalid status
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO audit.external_corroborator_cache("
                    "  source, entity_ticker, fetched_at, cached_payload, "
                    "  fetch_url, fetch_latency_ms, fetch_status"
                    ") VALUES ("
                    "  'investing_com', 'AKBNK', now(), CAST('{}' AS JSONB), "
                    "  'https://example/x', 100, 'totally-invalid'"
                    ")"
                )
            )
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM audit.external_corroborator_cache"))


async def test_index_exists(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        idx = (
            await conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname = 'audit' "
                    "  AND tablename = 'external_corroborator_cache' "
                    "  AND indexname = 'ecc_source_ticker'"
                )
            )
        ).scalar_one_or_none()
    assert idx == "ecc_source_ticker"


async def test_dashboard_role_can_select(
    aslan_dashboard_conn: asyncpg.Connection,
) -> None:
    """The aslan_dashboard role has SELECT on the cache."""
    n = await aslan_dashboard_conn.fetchval(
        "SELECT count(*) FROM audit.external_corroborator_cache"
    )
    assert int(n) >= 0


_DENY_WRITE_ERRORS = (
    asyncpg.exceptions.InsufficientPrivilegeError,
    asyncpg.exceptions.ReadOnlySQLTransactionError,
)


async def test_dashboard_role_cannot_insert(
    aslan_dashboard_conn: asyncpg.Connection,
) -> None:
    """Dashboard role does NOT have INSERT — the corroborator refresh
    POST runs the firecrawl call under audit_writer."""
    with pytest.raises(_DENY_WRITE_ERRORS):
        await aslan_dashboard_conn.execute(
            "INSERT INTO audit.external_corroborator_cache("
            "  source, entity_ticker, fetched_at, cached_payload, "
            "  fetch_url, fetch_latency_ms, fetch_status"
            ") VALUES ("
            "  'investing_com', 'AKBNK', now(), '{}'::jsonb, "
            "  'https://example/x', 100, 'ok'"
            ")"
        )


async def test_dashboard_role_cannot_update(
    aslan_dashboard_conn: asyncpg.Connection,
    engine: AsyncEngine,
) -> None:
    """UPDATE is intentionally never granted — the cache is append-only.
    A refresh writes a new row rather than mutating an existing one."""
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM audit.external_corroborator_cache"))
        await conn.execute(
            text(
                "INSERT INTO audit.external_corroborator_cache("
                "  source, entity_ticker, fetched_at, cached_payload, "
                "  fetch_url, fetch_latency_ms, fetch_status"
                ") VALUES ("
                "  'investing_com', 'AKBNK', now(), CAST('{}' AS JSONB), "
                "  'https://example/x', 100, 'ok'"
                ")"
            )
        )
    with pytest.raises(_DENY_WRITE_ERRORS):
        await aslan_dashboard_conn.execute(
            "UPDATE audit.external_corroborator_cache "
            "SET fetch_status = 'error' WHERE source = 'investing_com'"
        )
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM audit.external_corroborator_cache"))
