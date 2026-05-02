"""``aslan agg`` CLI group — v0.7.0.

Subcommands:
- ``refresh-snapshot`` — refresh the entity_latest_snapshot matview
- ``seed-restatement`` — upsert restatement configs from YAML
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any, cast

import click
import yaml

from aslan_core.agg import refresh_entity_snapshot, upsert_restatement_config
from aslan_core.audit import current_actor
from aslan_core.db.engine import create_engine
from aslan_core.db.session import create_session_factory


@click.group()
def agg() -> None:
    """Aggregate schema operations."""


@agg.command("refresh-snapshot")
def refresh_snapshot_cmd() -> None:
    """Refresh agg.entity_latest_snapshot (CONCURRENTLY)."""
    count = asyncio.run(_refresh())
    click.echo(f"{count} rows")


@agg.command("seed-restatement")
@click.option(
    "--file",
    "path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
)
def seed_restatement_cmd(path: str) -> None:
    """Upsert restatement configs from a YAML manifest."""
    doc = cast(
        dict[str, Any],
        yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {},
    )
    rows: list[dict[str, Any]] = doc.get("restatement_configs", [])
    asyncio.run(_seed_restatement(rows))


async def _refresh() -> int:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as s:
            return await refresh_entity_snapshot(s)
    finally:
        await engine.dispose()


def _parse_date(val: Any) -> date:
    if isinstance(val, date):
        return val
    return date.fromisoformat(str(val))


async def _seed_restatement(rows: list[dict[str, Any]]) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    actor = current_actor()
    try:
        async with factory() as s:
            for r in rows:
                result = await upsert_restatement_config(
                    s,
                    name=r["name"],
                    cpi_series_code=r["cpi_series_code"],
                    base_date=_parse_date(r["base_date"]),
                    applies_from=_parse_date(r["applies_from"]),
                    applies_to=_parse_date(r.get("applies_to", "9999-12-31")),
                    method=r["method"],
                    description=r.get("description"),
                    actor_id=actor.actor_id if actor else None,
                    actor_kind=actor.actor_kind if actor else None,
                )
                click.echo(f"  {result.name} (config_id={result.config_id})")
            await s.commit()
    finally:
        await engine.dispose()
