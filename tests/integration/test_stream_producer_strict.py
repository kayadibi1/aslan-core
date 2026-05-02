"""Codex F3 + critical-contract item 3: strict-mode rejection of
``StreamProducer.publish`` when no actor is set in the ContextVar.

Procurement-grade contract: the strict raise must fire BEFORE any DB
I/O (no outbox INSERT, no audit row). The test spies on
``session.execute`` / ``session.flush`` and asserts neither was called.

This file opts OUT of the conftest's autouse default-actor fixture by
declaring a local autouse fixture that runs AFTER conftest's. Without
that, every mutation here would inherit ``user:pytest`` and the strict
raise would never fire.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import set_actor
from aslan_core.config import Settings
from aslan_core.errors import AuditMissingActor
from aslan_core.streams import FilingNewEvent, StreamProducer

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_default_actor() -> Iterator[None]:
    """Clear the autouse default actor for the duration of these tests
    so the strict-mode raise can actually fire.
    """
    set_actor(None)
    yield
    set_actor(None)


def _minimal_event() -> FilingNewEvent:
    return FilingNewEvent(
        schema_version=1,
        event_id=uuid4(),
        produced_at=datetime.now(UTC),
        producer_run_id=0,
        source_id="kap",
        filing_id=uuid4(),
        entity_id=None,
        filing_kind="material_event",
        title="t",
        published_at=datetime.now(UTC),
        primary_object_key="k",
        bucket="b",
        is_revision=False,
        revision_no=1,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_publish_strict_mode_rejects_without_actor_before_io(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex spec §5 + v0.3 contract: spy assertion that
    ``session.execute`` / ``session.flush`` are never called when
    ``audit_strict=True`` and ``current_actor()`` is None.
    """
    # Seed the run separately via direct execute BEFORE the spy starts,
    # so the spy sees zero invocations against ``producer.publish``'s
    # session.
    await session.execute(
        text(
            "INSERT INTO src.source(source_id, name, kind, license_status) "
            "VALUES('kap','KAP','scraper','open') ON CONFLICT DO NOTHING"
        )
    )
    rid: int = (
        await session.execute(
            text(
                "INSERT INTO src.ingestion_run(source_id, job_name, status) "
                "VALUES('kap','strict_stream_test','succeeded') "
                "RETURNING ingestion_run_id"
            )
        )
    ).scalar_one()
    await session.commit()

    monkeypatch.setenv("ASLAN_AUDIT_STRICT", "true")
    assert Settings(_env_file=None).audit_strict is True

    producer = StreamProducer(session=session, ingestion_run_id=rid)
    with (
        patch.object(session, "execute", new_callable=AsyncMock) as exec_spy,
        patch.object(session, "flush", new_callable=AsyncMock) as flush_spy,
        pytest.raises(AuditMissingActor),
    ):
        await producer.publish(_minimal_event())
    exec_spy.assert_not_called()
    flush_spy.assert_not_called()
