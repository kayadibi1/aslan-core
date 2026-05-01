"""Every query helper runs cleanly under the actual ``aslan_dashboard`` LOGIN.

Codex branch-state F-4: ``test_dashboard_queries_run_clean.py``
exercises the helpers under the testcontainer superuser, which
bypasses every column-allowlist GRANT. A future query that projects
a forbidden column would still pass that smoke test. This file
opens a SQLAlchemy AsyncEngine authenticated as ``aslan_dashboard``
and re-runs each helper end-to-end — catches missing GRANTs,
revoked columns, and SECURITY DEFINER ACL drift before pages land
in Tasks 8-9.

The session here logs in directly as ``aslan_dashboard`` (LOGIN
PASSWORD), the same path production uses. ``SET ROLE`` is NOT used
— the goal is to exercise the actual privilege boundary, not a
superuser impersonating the role.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from aslan_core.dashboard import queries
from aslan_core.dashboard.view_models import (
    AuditVM,
    DocumentsVM,
    IngestionVM,
    OutboxVM,
    OverviewVM,
    RedactionsVM,
    TimeseriesVM,
)

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(loop_scope="session")
async def aslan_dashboard_engine(
    aslan_dashboard_dsn: str,
) -> AsyncIterator[AsyncEngine]:
    """SQLAlchemy AsyncEngine authenticated as ``aslan_dashboard``.

    The DSN comes from ``conftest.py``'s ``aslan_dashboard_dsn``
    fixture (a postgres:// URL with the dev-fallback password).
    SQLAlchemy needs the ``+asyncpg`` driver suffix added back."""
    engine = create_async_engine(
        aslan_dashboard_dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def aslan_dashboard_session(
    aslan_dashboard_engine: AsyncEngine,
) -> AsyncIterator[AsyncSession]:
    """Function-scoped session for the dashboard role. Rolled back
    after each test (the role's ``default_transaction_read_only=on``
    means there's nothing to commit anyway)."""
    factory = async_sessionmaker(aslan_dashboard_engine, expire_on_commit=False)
    async with factory() as s:
        try:
            yield s
        finally:
            await s.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_overview_runs_under_aslan_dashboard(
    aslan_dashboard_session: AsyncSession,
) -> None:
    vm = await queries.overview(aslan_dashboard_session)
    assert isinstance(vm, OverviewVM)


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_recent_runs_under_aslan_dashboard(
    aslan_dashboard_session: AsyncSession,
) -> None:
    vm = await queries.outbox_recent(aslan_dashboard_session, limit=10, offset=0)
    assert isinstance(vm, OutboxVM)


@pytest.mark.asyncio(loop_scope="session")
async def test_deadletter_recent_runs_under_aslan_dashboard(
    aslan_dashboard_session: AsyncSession,
) -> None:
    rows, total = await queries.deadletter_recent(aslan_dashboard_session, limit=10, offset=0)
    assert isinstance(rows, list)
    assert isinstance(total, int)


@pytest.mark.asyncio(loop_scope="session")
async def test_ingestion_recent_runs_under_aslan_dashboard(
    aslan_dashboard_session: AsyncSession,
) -> None:
    vm = await queries.ingestion_recent(aslan_dashboard_session, limit=10)
    assert isinstance(vm, IngestionVM)


@pytest.mark.asyncio(loop_scope="session")
async def test_documents_recent_runs_under_aslan_dashboard(
    aslan_dashboard_session: AsyncSession,
) -> None:
    vm = await queries.documents_recent(aslan_dashboard_session, limit=10)
    assert isinstance(vm, DocumentsVM)


@pytest.mark.asyncio(loop_scope="session")
async def test_timeseries_overview_runs_under_aslan_dashboard(
    aslan_dashboard_session: AsyncSession,
) -> None:
    vm = await queries.timeseries_overview(aslan_dashboard_session, limit=10)
    assert isinstance(vm, TimeseriesVM)


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_recent_runs_under_aslan_dashboard(
    aslan_dashboard_session: AsyncSession,
) -> None:
    """Exercises the full SECURITY DEFINER chain end-to-end:
    ``audit.event_client_ip_truncated`` (migration 0022) and
    ``audit.event_metadata_key_count`` (migration 0020) both run
    under the dashboard role's GRANT EXECUTE."""
    vm = await queries.audit_recent(aslan_dashboard_session, limit=10)
    assert isinstance(vm, AuditVM)


@pytest.mark.asyncio(loop_scope="session")
async def test_redactions_recent_runs_under_aslan_dashboard(
    aslan_dashboard_session: AsyncSession,
) -> None:
    vm = await queries.redactions_recent(aslan_dashboard_session, limit=10)
    assert isinstance(vm, RedactionsVM)
