"""Migration 0063 — public_status_reader role + minimal GRANTs.

Asserts:

  * the role exists and can LOGIN;
  * default_transaction_read_only=on is set at role level;
  * SELECT works on audit.recency_observation + audit.coverage_snapshot;
  * USAGE on schema audit is granted;
  * USAGE on every OTHER schema is NOT granted;
  * SELECT on every OTHER audit.* table is NOT granted (defense-in-depth
    — one accidental GRANT to the role would not blow the boundary
    open the way a misconfigured aslan_dashboard would).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

# Migration 0063 installs this fallback when ASLAN_PUBLIC_STATUS_PASSWORD
# is unset and ASLAN_ENV is "dev" (the default in the test environment).
_PUBLIC_STATUS_DEV_PASSWORD = "DEV_ONLY_REPLACE_ME"


@pytest.fixture
def public_status_dsn(pg_dsn: str) -> str:
    """Rewrite the testcontainer DSN to authenticate as
    ``public_status_reader``."""
    raw = pg_dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    parsed = urlparse(raw)
    netloc = f"public_status_reader:{_PUBLIC_STATUS_DEV_PASSWORD}@{parsed.hostname}:{parsed.port}"
    return urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


@pytest_asyncio.fixture(loop_scope="session")
async def public_status_conn(
    public_status_dsn: str,
) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(dsn=public_status_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def test_role_exists(session: AsyncSession) -> None:
    role = (
        await session.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'public_status_reader'"))
    ).scalar_one_or_none()
    assert role is not None


async def test_role_can_login(session: AsyncSession) -> None:
    rolcanlogin = (
        await session.execute(
            text("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'public_status_reader'")
        )
    ).scalar_one()
    assert rolcanlogin is True


async def test_role_default_transaction_read_only(
    session: AsyncSession,
) -> None:
    setting = (
        await session.execute(
            text(
                "SELECT setconfig FROM pg_db_role_setting "
                "JOIN pg_roles ON pg_db_role_setting.setrole = pg_roles.oid "
                "WHERE rolname = 'public_status_reader'"
            )
        )
    ).scalar_one_or_none()
    assert setting is not None
    assert any("default_transaction_read_only=on" in s for s in setting)


async def test_role_can_select_recency_observation(
    public_status_conn: asyncpg.Connection,
) -> None:
    """The role MUST be able to SELECT from audit.recency_observation."""
    n = await public_status_conn.fetchval("SELECT count(*) FROM audit.recency_observation")
    assert int(n) >= 0


async def test_role_can_select_coverage_snapshot(
    public_status_conn: asyncpg.Connection,
) -> None:
    """The role MUST be able to SELECT from audit.coverage_snapshot."""
    n = await public_status_conn.fetchval("SELECT count(*) FROM audit.coverage_snapshot")
    assert int(n) >= 0


_DENY_ERRORS = (
    asyncpg.exceptions.InsufficientPrivilegeError,
    asyncpg.exceptions.ReadOnlySQLTransactionError,
)


@pytest.mark.parametrize(
    "table",
    [
        "audit.event",
        "audit.alert_dispatch",
        "audit.severity_rule",
        "audit.sync_log",
        "audit.scorecard_snapshot",
    ],
)
async def test_role_cannot_select_other_audit_tables(
    public_status_conn: asyncpg.Connection,
    table: str,
) -> None:
    """Defense-in-depth: every other audit.* table the role might
    accidentally reach raises InsufficientPrivilegeError. SELECT on
    these tables would expose internal alert payloads, severity rules,
    scorecard notes, etc.

    The f-string here interpolates a parametrized constant (closed
    list above) — not user input — so the SQL-injection lint warning
    does not apply.
    """
    with pytest.raises(_DENY_ERRORS):
        await public_status_conn.fetchval(f"SELECT count(*) FROM {table}")  # noqa: S608


# Probe table per non-audit schema. Closed dict — every value is a
# table-name literal that we know exists post-migration.
_SCHEMA_TO_TABLE = {
    "streams": "streams.outbox",
    "doc": "doc.filing",
    "ts": "ts.observation",
    "src": "src.source",
    "ref": "ref.entity",
    "agg": "agg.filing_event",
}


@pytest.mark.parametrize("schema", list(_SCHEMA_TO_TABLE))
async def test_role_lacks_usage_on_other_schemas(
    public_status_conn: asyncpg.Connection,
    schema: str,
) -> None:
    """Every other schema is unreachable — the role lacks USAGE so a
    SELECT against a known table in that schema fails at the schema
    gate.

    The f-string interpolates a parametrized constant from
    ``_SCHEMA_TO_TABLE`` — not user input — so the SQL-injection
    lint warning does not apply.
    """
    target = _SCHEMA_TO_TABLE[schema]
    with pytest.raises(_DENY_ERRORS):
        await public_status_conn.fetchval(f"SELECT 1 FROM {target} LIMIT 1")  # noqa: S608


async def test_role_cannot_insert_recency(
    public_status_conn: asyncpg.Connection,
) -> None:
    """Defense-in-depth: SELECT-only. An accidental INSERT path must
    fail at the privilege layer (or at the read-only-transaction layer
    via default_transaction_read_only=on)."""
    with pytest.raises(_DENY_ERRORS):
        await public_status_conn.execute(
            "INSERT INTO audit.recency_observation"
            "(source, observed_at, upstream_latest_at, db_latest_at, "
            " sla_target_seconds) "
            "VALUES ('kap', now(), now(), now(), 300)"
        )


async def test_role_cannot_insert_coverage(
    public_status_conn: asyncpg.Connection,
) -> None:
    with pytest.raises(_DENY_ERRORS):
        await public_status_conn.execute(
            "INSERT INTO audit.coverage_snapshot"
            "(source, dimension, observed_at, expected_count, actual_count) "
            "VALUES ('kap', 'd', now(), 100, 100)"
        )
