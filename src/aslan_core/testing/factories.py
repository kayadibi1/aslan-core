"""Helper factories for consumer test suites.

Each factory does the minimum INSERT to give a test a usable row.
Callers must `await session.commit()` if they need cross-session visibility.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def ingestion_run_factory(
    session: AsyncSession,
    *,
    source_id: str = "test",
    job_name: str = "test_job",
    status: str = "running",
) -> int:
    """Insert a `src.source` row (if missing) + a `src.ingestion_run` row.

    Returns the run id. Caller commits if cross-session visibility is needed.
    """
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES (:sid, :sid, 'manual', 'open') ON CONFLICT DO NOTHING"
        ),
        {"sid": source_id},
    )
    run_id: int = (
        await session.execute(
            text(
                "INSERT INTO src.ingestion_run (source_id, job_name, status) "
                "VALUES (:sid, :job, :status) "
                "RETURNING ingestion_run_id"
            ),
            {"sid": source_id, "job": job_name, "status": status},
        )
    ).scalar_one()
    return run_id


async def entity_factory(
    session: AsyncSession,
    *,
    ingestion_run_id: int,
    source_id: str = "test",
    entity_type: str = "company",
    legal_name: str | None = None,
    short_name: str | None = None,
    status: str = "active",
    metadata: dict[str, Any] | None = None,
) -> UUID:
    """Insert a `ref.entity` row with sensible defaults. Returns the entity_id.

    Caller commits if cross-session visibility is needed.
    """
    legal_name = legal_name or f"Test Co {uuid4().hex[:8]}"
    md_json = json.dumps(metadata) if metadata is not None else None
    eid: UUID = (
        await session.execute(
            text(
                "INSERT INTO ref.entity "
                "  (entity_type, legal_name, short_name, status, "
                "   source_id, ingestion_run_id, metadata) "
                "VALUES (:type, :ln, :sn, :status, :sid, :run, "
                "        COALESCE(:md, '{}')::jsonb) "
                "RETURNING entity_id"
            ),
            {
                "type": entity_type,
                "ln": legal_name,
                "sn": short_name,
                "status": status,
                "sid": source_id,
                "run": ingestion_run_id,
                "md": md_json,
            },
        )
    ).scalar_one()
    return eid
