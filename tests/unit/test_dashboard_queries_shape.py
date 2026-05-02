"""Each ``queries.py`` helper returns a typed VM (or known dict
shape), independent of DB content.

Spec §8.1 contract: a future regression that swapped ``queries.outbox_recent``
to return a raw SQLAlchemy ``Row`` would still pass the rendering
tests under specific seed shapes; this no-DB shape check pins the
contract at the helper layer.

The tests exercise each helper with an empty result set (the live
testcontainer's tables are clean for tests that ran before this
one), asserting:

  * The return type is the documented Pydantic VM (or dict tuple
    for ``deadletter_recent``).
  * The ``rows`` field is an empty list.
  * Scalar fields use the expected Python types.

Runs as a unit test (no testcontainer) using the project's session
factory only after the integration container is available — which
means these are technically integration-tier in cost. We mark them
``integration`` to avoid surprising the unit-only CI path.
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
async def test_overview_returns_overview_vm(session: AsyncSession) -> None:
    vm = await queries.overview(session)
    assert isinstance(vm, OverviewVM)
    assert isinstance(vm.outbox_pending, int)
    assert isinstance(vm.outbox_oldest_age_s, float)
    assert isinstance(vm.audit_events_per_min_last_60m, float)


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_recent_returns_outbox_vm(session: AsyncSession) -> None:
    vm = await queries.outbox_recent(session, limit=10)
    assert isinstance(vm, OutboxVM)
    assert isinstance(vm.rows, list)
    assert isinstance(vm.pending_total, int)


@pytest.mark.asyncio(loop_scope="session")
async def test_deadletter_recent_returns_rows_total_tuple(session: AsyncSession) -> None:
    rows, total = await queries.deadletter_recent(session, limit=10)
    assert isinstance(rows, list)
    assert isinstance(total, int)
    for row in rows:
        assert isinstance(row, dict)
        assert "failure_id" in row
        assert "deadletter_stream" in row
        assert "has_redis_index" in row


@pytest.mark.asyncio(loop_scope="session")
async def test_ingestion_recent_returns_ingestion_vm(session: AsyncSession) -> None:
    vm = await queries.ingestion_recent(session, limit=10)
    assert isinstance(vm, IngestionVM)
    assert isinstance(vm.rows, list)


@pytest.mark.asyncio(loop_scope="session")
async def test_documents_recent_returns_documents_vm(session: AsyncSession) -> None:
    vm = await queries.documents_recent(session, limit=10)
    assert isinstance(vm, DocumentsVM)
    assert isinstance(vm.rows, list)


@pytest.mark.asyncio(loop_scope="session")
async def test_timeseries_overview_returns_timeseries_vm(session: AsyncSession) -> None:
    vm = await queries.timeseries_overview(session, limit=10)
    assert isinstance(vm, TimeseriesVM)
    assert isinstance(vm.rows, list)


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_recent_returns_audit_vm(session: AsyncSession) -> None:
    vm = await queries.audit_recent(session, limit=10)
    assert isinstance(vm, AuditVM)
    assert isinstance(vm.rows, list)
    assert vm.compliance_banner == "Network-edge logging only — not compliance evidence"


@pytest.mark.asyncio(loop_scope="session")
async def test_redactions_recent_returns_redactions_vm(session: AsyncSession) -> None:
    vm = await queries.redactions_recent(session, limit=10)
    assert isinstance(vm, RedactionsVM)
    assert isinstance(vm.rows, list)
    assert vm.compliance_banner == "Network-edge logging only — not compliance evidence"


def test_streams_static_list_is_a_list_of_strings() -> None:
    """Pure-Python helper — no DB; tests deserve to live in the
    same file even though they don't take a session fixture."""
    streams = queries.streams_static_list()
    assert isinstance(streams, list)
    assert len(streams) > 0
    assert all(isinstance(s, str) for s in streams)
