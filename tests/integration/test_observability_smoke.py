"""Smoke tests for the v0.3.0 observability wiring.

These tests require the ``aslan-core[obs]`` extra to be installed.
They are skipped on a base venv (the lazy-import shim makes the
imports succeed but the tests need the real prometheus_client +
opentelemetry SDK to assert end-to-end behavior).

Coverage:

* **Task 14:** SQLAlchemyInstrumentor + AsyncPGInstrumentor produce a
  span when a query runs after the same instrumentation
  ``setup_tracing()`` wires for the OTLP path.
* **Task 16:** ``DocumentStore.put_filing`` increments
  ``aslan_filing_puts_total{source_id, kind, created}``.
* **Task 16:** ``audit.recorder.record`` increments
  ``aslan_audit_events_total{operation, actor_kind}`` after the
  underlying SQL INSERT succeeds.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

prometheus_client = pytest.importorskip("prometheus_client")
opentelemetry_sdk = pytest.importorskip("opentelemetry.sdk.trace")

pytestmark = pytest.mark.integration


def _counter_value(handle: Any, **labels: str) -> float:
    """Read the current value of a labelled lazy-counter handle."""
    impl = handle._ensure_impl()
    if impl is None:  # pragma: no cover — fixed-skip via importorskip
        return 0.0
    return float(impl.labels(**labels)._value.get())


def _histogram_count(handle: Any, **labels: str) -> int:
    """Read the count of samples observed for a labelled histogram."""
    impl = handle._ensure_impl()
    if impl is None:  # pragma: no cover
        return 0
    child = impl.labels(**labels)
    # Iterate _metrics — the prom client stores per-bucket and _count
    # samples on the child. The simplest reliable read is via collect().
    for metric_family in handle._ensure_impl().collect():
        for sample in metric_family.samples:
            if sample.name.endswith("_count") and sample.labels.get("operation") == labels.get(
                "operation"
            ):
                return int(sample.value)
    return int(child._count.get())


@pytest.mark.asyncio(loop_scope="session")
async def test_filing_puts_counter_increments_on_create(
    session: AsyncSession,
    object_storage_fake: Any,
) -> None:
    """A successful put_filing increments the counter for that
    (source_id, kind, created='true') labelset."""
    from aslan_core.documents.client import DocumentStore
    from aslan_core.observability import metrics

    sid = "kap"
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES (:s, :n, 'scraper', 'open') ON CONFLICT DO NOTHING"
        ),
        {"s": sid, "n": "KAP"},
    )
    rid: int = await session.scalar(
        text(
            "INSERT INTO src.ingestion_run (source_id, job_name, status) "
            "VALUES (:s, :j, 'started') RETURNING ingestion_run_id"
        ),
        {"s": sid, "j": "obs-smoke"},
    )
    await session.commit()

    store = DocumentStore(session, object_client=object_storage_fake, ingestion_run_id=rid)

    before = _counter_value(metrics.filing_puts, source_id=sid, kind="news", created="true")
    await store.put_filing(
        source_id=sid,
        source_filing_ref=f"obs-smoke-{uuid4().hex}",
        entity_id=None,
        kind="news",
        title="t",
        published_at=datetime.now(UTC),
        primary_bytes=b"<html></html>",
        primary_mime="text/html",
        primary_filename="main.html",
    )
    await session.commit()
    after = _counter_value(metrics.filing_puts, source_id=sid, kind="news", created="true")
    assert after == before + 1.0


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_events_counter_increments_on_record(
    session: AsyncSession,
) -> None:
    """audit.recorder.record bumps aslan_audit_events_total."""
    from aslan_core.audit import AuditRecord, record
    from aslan_core.observability import metrics

    await session.execute(text("DELETE FROM audit.events"))
    await session.commit()

    # Use a real allow-listed operation — the metric label is bounded
    # via _normalize_metric_label, so an unknown smoke string would
    # land in operation=other and the per-op counter would not move
    # (codex Batch 4 cardinality fix).
    op = "entity.create"
    before = _counter_value(metrics.audit_events, operation=op, actor_kind="user")
    await record(
        session,
        record=AuditRecord(
            operation=op,
            target_schema="ref",
            target_table="entity",
            target_pk={"entity_id": str(uuid4())},
            before=None,
            after={"smoke": True},
        ),
    )
    await session.commit()
    after = _counter_value(metrics.audit_events, operation=op, actor_kind="user")
    assert after == before + 1.0


@pytest.mark.asyncio(loop_scope="session")
async def test_traced_decorator_observes_db_query_duration() -> None:
    """The @traced decorator's histogram observation actually records a
    sample. We assert the count went up by 1 — wall-clock value is
    intentionally not asserted (timing variance)."""
    from aslan_core.observability import metrics, traced

    class Probe:
        @traced(operation_name="probe.run")
        async def run(self) -> int:
            return 42

    count_before = _histogram_count(metrics.db_query_duration, operation="probe.run")
    await Probe().run()
    count_after = _histogram_count(metrics.db_query_duration, operation="probe.run")
    assert count_after == count_before + 1


@pytest.mark.asyncio(loop_scope="session")
async def test_setup_tracing_with_inmemory_exporter_produces_sql_span(
    session: AsyncSession,
) -> None:
    """Codex Task 14 smoke: SQLAlchemy / asyncpg auto-instrumentation
    captures spans for SQL queries.

    This test wires an in-memory exporter (the OTLP exporter that
    setup_tracing uses cannot be smoke-tested in-process), then
    invokes SQLAlchemyInstrumentor + AsyncPGInstrumentor — the same
    instrumentation our setup_tracing() function calls. Asserts at
    least one span was captured for the SELECT statement.
    """
    from opentelemetry import trace
    from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "aslan-core-smoke"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    SQLAlchemyInstrumentor().instrument()
    AsyncPGInstrumentor().instrument()

    try:
        # Run a query distinct enough to spot in the span list.
        await session.execute(text("SELECT 1 AS aslan_obs_smoke_marker, current_timestamp"))
        provider.force_flush(timeout_millis=2000)
        spans = exporter.get_finished_spans()
        # At least one span captured (auto-instrumentation produces
        # spans for connect / BEGIN / SELECT). The strict claim of
        # this smoke is "spans were emitted" — not a specific name.
        assert spans, "no spans captured by SQLAlchemy / asyncpg auto-instrumentation"
    finally:
        SQLAlchemyInstrumentor().uninstrument()
        AsyncPGInstrumentor().uninstrument()
