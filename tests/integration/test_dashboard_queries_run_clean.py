"""Each query helper executes cleanly against the testcontainer DB.

Smoke test for Task 5: every helper in ``queries.py`` runs without
SQL error against the empty testcontainer schema. Catches typos,
missing columns, and incompatible PostgreSQL function calls before
the page handlers in Tasks 8–9 wire them up.

This test connects as the testcontainer superuser (the default
``session`` fixture). The full role-boundary contract — every helper
runs cleanly under the ``aslan_dashboard`` LOGIN — is exercised
indirectly by the Task 2 boundary suite plus the integration tests
landing in Tasks 8–9 (which connect via the dashboard's own session
factory).
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

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


@pytest.mark.asyncio(loop_scope="session")
async def test_overview_runs(session: AsyncSession) -> None:
    vm = await queries.overview(session)
    assert isinstance(vm, OverviewVM)


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_recent_runs(session: AsyncSession) -> None:
    vm = await queries.outbox_recent(session, limit=10, offset=0)
    assert isinstance(vm, OutboxVM)
    assert vm.pending_total >= 0


@pytest.mark.asyncio(loop_scope="session")
async def test_deadletter_recent_runs(session: AsyncSession) -> None:
    rows, total = await queries.deadletter_recent(session, limit=10, offset=0)
    assert isinstance(rows, list)
    assert isinstance(total, int)
    assert total >= 0


@pytest.mark.asyncio(loop_scope="session")
async def test_ingestion_recent_runs(session: AsyncSession) -> None:
    vm = await queries.ingestion_recent(session, limit=10)
    assert isinstance(vm, IngestionVM)


@pytest.mark.asyncio(loop_scope="session")
async def test_documents_recent_runs(session: AsyncSession) -> None:
    vm = await queries.documents_recent(session, limit=10)
    assert isinstance(vm, DocumentsVM)


@pytest.mark.asyncio(loop_scope="session")
async def test_timeseries_overview_runs(session: AsyncSession) -> None:
    vm = await queries.timeseries_overview(session, limit=10)
    assert isinstance(vm, TimeseriesVM)


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_recent_runs(session: AsyncSession) -> None:
    vm = await queries.audit_recent(session, limit=10)
    assert isinstance(vm, AuditVM)


@pytest.mark.asyncio(loop_scope="session")
async def test_redactions_recent_runs(session: AsyncSession) -> None:
    vm = await queries.redactions_recent(session, limit=10)
    assert isinstance(vm, RedactionsVM)


def test_streams_static_list_returns_canonical_streams() -> None:
    names = queries.streams_static_list()
    assert "aslan.kap.filings.new" in names
    assert "aslan.bist.ticks" in names
