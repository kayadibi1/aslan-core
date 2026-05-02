"""Public API for the ``agg`` schema.

Exposes:
- ``refresh_entity_snapshot`` — concurrent matview refresh
- ``get_restatement_configs`` — read all configs
- ``upsert_restatement_config`` — insert or update by name
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.models.agg import RestatementConfig
from aslan_core.schemas.agg import RestatementConfigSchema


async def refresh_entity_snapshot(session: AsyncSession) -> int:
    """REFRESH MATERIALIZED VIEW CONCURRENTLY agg.entity_latest_snapshot.

    Returns the row count after refresh.
    """
    await session.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY agg.entity_latest_snapshot"))
    await session.commit()
    result = await session.execute(text("SELECT count(*) FROM agg.entity_latest_snapshot"))
    return result.scalar() or 0


async def get_restatement_configs(
    session: AsyncSession,
) -> list[RestatementConfigSchema]:
    """Read all restatement configs."""
    result = await session.execute(select(RestatementConfig).order_by(RestatementConfig.name))
    rows = result.scalars().all()
    return [
        RestatementConfigSchema(
            config_id=r.config_id,
            name=r.name,
            cpi_series_code=r.cpi_series_code,
            base_date=r.base_date,
            applies_from=r.applies_from,
            applies_to=r.applies_to,
            method=r.method,
            description=r.description,
            created_at=r.created_at,
        )
        for r in rows
    ]


async def upsert_restatement_config(
    session: AsyncSession,
    *,
    name: str,
    cpi_series_code: str,
    base_date: date,
    applies_from: date,
    method: str,
    applies_to: date = date(9999, 12, 31),
    description: str | None = None,
    actor_id: str | None = None,
    actor_kind: str | None = None,
) -> RestatementConfigSchema:
    """Insert or update a restatement config by name."""
    ins = insert(RestatementConfig).values(
        name=name,
        cpi_series_code=cpi_series_code,
        base_date=base_date,
        applies_from=applies_from,
        applies_to=applies_to,
        method=method,
        description=description,
        created_at=datetime.now(UTC),
        actor_id=actor_id,
        actor_kind=actor_kind,
    )
    stmt = ins.on_conflict_do_update(
        index_elements=["name"],
        set_={
            "cpi_series_code": ins.excluded.cpi_series_code,
            "base_date": ins.excluded.base_date,
            "applies_from": ins.excluded.applies_from,
            "applies_to": ins.excluded.applies_to,
            "method": ins.excluded.method,
            "description": ins.excluded.description,
            "actor_id": ins.excluded.actor_id,
            "actor_kind": ins.excluded.actor_kind,
        },
    ).returning(RestatementConfig)
    result = await session.execute(stmt)
    row = result.scalar_one()
    await session.flush()
    return RestatementConfigSchema(
        config_id=row.config_id,
        name=row.name,
        cpi_series_code=row.cpi_series_code,
        base_date=row.base_date,
        applies_from=row.applies_from,
        applies_to=row.applies_to,
        method=row.method,
        description=row.description,
        created_at=row.created_at,
    )
