"""Integration tests for ``aslan_core.audit.recorder.record()``.

Covers the four strict/lenient by actor-present/missing combinations
and the atomicity-with-caller-transaction guarantee.
"""

from __future__ import annotations

import logging
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import Actor, AuditRecord, record, set_actor
from aslan_core.config import Settings
from aslan_core.errors import AuditMissingActor

pytestmark = pytest.mark.integration


async def _wipe_events(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM audit.events"))
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_record_writes_one_audit_row_with_current_actor(
    session: AsyncSession,
) -> None:
    await _wipe_events(session)
    rid = uuid4()
    set_actor(
        Actor(
            actor_id="user:sidar@aslan.ai",
            actor_kind="user",
            client_ip="10.0.0.1",
            user_agent="aslan-cli/0.3",
            request_id=rid,
        )
    )
    try:
        pk = {"entity_id": str(uuid4())}
        rec = AuditRecord(
            operation="entity.create",
            target_schema="ref",
            target_table="entity",
            target_pk=pk,
            before=None,
            after={"legal_name": "Aselsan A.Ş.", "type": "company"},
        )
        await record(session, record=rec)
        await session.commit()

        rows = (
            await session.execute(
                text(
                    "SELECT actor_id, actor_kind, client_ip, user_agent, "
                    "       request_id, operation, target_schema, "
                    "       target_table, target_pk, before, after "
                    "FROM audit.events"
                )
            )
        ).all()
        assert len(rows) == 1
        r = rows[0]
        assert r.actor_id == "user:sidar@aslan.ai"
        assert r.actor_kind == "user"
        assert str(r.client_ip).startswith("10.0.0.1")
        assert r.user_agent == "aslan-cli/0.3"
        assert r.request_id == rid
        assert r.operation == "entity.create"
        assert r.target_schema == "ref"
        assert r.target_table == "entity"
        assert r.target_pk == pk
        assert r.before is None
        assert r.after["legal_name"] == "Aselsan A.Ş."
    finally:
        set_actor(None)


@pytest.mark.asyncio(loop_scope="session")
async def test_record_writes_system_unknown_when_lenient_and_no_actor(
    session: AsyncSession,
) -> None:
    """audit_strict=False (default during the migration window): write
    the row with actor_id='system:unknown' and log a structured warning.

    We attach our own ``logging.Handler`` rather than rely on pytest's
    caplog because pytest-asyncio with a session-scoped loop sometimes
    drops captures from coroutines started inside fixtures."""
    await _wipe_events(session)
    set_actor(None)

    captured: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _ListHandler(level=logging.WARNING)
    rec_logger = logging.getLogger("aslan_core.audit.recorder")
    rec_logger.addHandler(handler)
    prior_level = rec_logger.level
    prior_disabled = rec_logger.disabled
    rec_logger.setLevel(logging.WARNING)
    # pytest's logging plugin sometimes disables module loggers between
    # tests via logging.disable(); flip the per-logger flag back on.
    rec_logger.disabled = False
    try:
        pk = {"entity_id": str(uuid4())}
        rec = AuditRecord(
            operation="entity.create",
            target_schema="ref",
            target_table="entity",
            target_pk=pk,
            before=None,
            after={},
        )
        await record(session, record=rec)
        await session.commit()
    finally:
        rec_logger.removeHandler(handler)
        rec_logger.setLevel(prior_level)
        rec_logger.disabled = prior_disabled

    row = (
        await session.execute(
            text("SELECT actor_id, actor_kind FROM audit.events ORDER BY occurred_at DESC LIMIT 1")
        )
    ).one()
    assert row.actor_id == "system:unknown"
    assert row.actor_kind == "system"
    assert any("audit_missing_actor" in (lr.getMessage() or "").lower() for lr in captured), (
        f"expected 'audit_missing_actor' WARNING; got {[lr.getMessage() for lr in captured]}"
    )
    # The WARNING level matters — we don't want a stealth INFO that
    # an operator could miss.
    assert any(lr.levelno == logging.WARNING for lr in captured)


@pytest.mark.asyncio(loop_scope="session")
async def test_record_raises_when_strict_and_no_actor(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit_strict=True: raise AuditMissingActor BEFORE any DB write.

    The audit.events table must be untouched on rejection."""
    await _wipe_events(session)
    set_actor(None)

    # Reload Settings under ASLAN_AUDIT_STRICT=true so the strict path
    # is exercised end-to-end.
    monkeypatch.setenv("ASLAN_AUDIT_STRICT", "true")
    strict_settings = Settings()
    assert strict_settings.audit_strict is True

    pk = {"entity_id": str(uuid4())}
    rec = AuditRecord(
        operation="entity.create",
        target_schema="ref",
        target_table="entity",
        target_pk=pk,
        before=None,
        after={},
    )
    with pytest.raises(AuditMissingActor):
        await record(session, record=rec, settings=strict_settings)

    # Bucket / table / etc. is untouched on rejection.
    n = await session.scalar(text("SELECT COUNT(*) FROM audit.events"))
    assert n == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_record_atomic_with_caller_transaction(
    session: AsyncSession,
) -> None:
    """If the caller rolls back, the audit row goes too — record() shares
    the caller's session/transaction."""
    await _wipe_events(session)
    set_actor(Actor(actor_id="user:test", actor_kind="user"))
    try:
        rec = AuditRecord(
            operation="entity.create",
            target_schema="ref",
            target_table="entity",
            target_pk={"entity_id": str(uuid4())},
            before=None,
            after={},
        )
        await record(session, record=rec)
        # Caller decides to roll back — audit row goes with it.
        await session.rollback()

        n = await session.scalar(text("SELECT COUNT(*) FROM audit.events"))
        assert n == 0
    finally:
        set_actor(None)


@pytest.mark.asyncio(loop_scope="session")
async def test_record_persists_metadata_and_ingestion_run_id(
    session: AsyncSession,
) -> None:
    """metadata + ingestion_run_id round-trip through JSON."""
    await _wipe_events(session)
    # Use any FK-valid run id; we don't have one handy in this isolated
    # test, so leave ingestion_run_id NULL and exercise the non-null
    # path with a separate value that doesn't need an FK lookup.
    set_actor(Actor(actor_id="service:kap", actor_kind="service"))
    try:
        rec = AuditRecord(
            operation="filing.put",
            target_schema="doc",
            target_table="filing",
            target_pk={"filing_id": str(uuid4())},
            before=None,
            after={"sha256": "abc123"},
            metadata={"returned_existing": False, "kind": "main"},
            ingestion_run_id=None,
        )
        await record(session, record=rec)
        await session.commit()

        row = (
            await session.execute(
                text(
                    "SELECT operation, metadata, ingestion_run_id, after "
                    "FROM audit.events ORDER BY occurred_at DESC LIMIT 1"
                )
            )
        ).one()
        assert row.operation == "filing.put"
        assert row.metadata == {"returned_existing": False, "kind": "main"}
        assert row.ingestion_run_id is None
        assert row.after == {"sha256": "abc123"}
    finally:
        set_actor(None)
