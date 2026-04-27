from __future__ import annotations

import asyncio
import json
from datetime import date as _date
from uuid import UUID

import click

from aslan_core.db.engine import create_engine
from aslan_core.db.session import create_session_factory, session_scope
from aslan_core.ingestion.run import ingestion_run
from aslan_core.registry.client import EntityRegistryClient


@click.group()
def registry() -> None:
    """Registry reads and mutations."""


def _parse_id(s: str) -> tuple[str, str]:
    if "=" not in s:
        raise click.BadParameter("must be namespace=value")
    ns, val = s.split("=", 1)
    return ns, val


# ─── create-entity ───


@registry.command("create-entity")
@click.option("--type", "type_", required=True)
@click.option("--name", "legal_name", required=True)
@click.option("--short-name", default=None)
@click.option("--status", default="active")
@click.option("--identifier", multiple=True)
def create_entity_cmd(
    type_: str,
    legal_name: str,
    short_name: str | None,
    status: str,
    identifier: tuple[str, ...],
) -> None:
    """Create a new entity (or auto-merge with same-source matches)."""
    ids = dict(_parse_id(s) for s in identifier)
    asyncio.run(_create_entity(type_, legal_name, short_name, status, ids))


async def _create_entity(
    type_: str,
    legal_name: str,
    short_name: str | None,
    status: str,
    ids: dict[str, str],
) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with (
            ingestion_run(
                engine,
                source_id="manual",
                job_name="cli.registry.create_entity",
            ) as run,
            session_scope(factory) as s,
        ):
            client = EntityRegistryClient(s, ingestion_run_id=run.id)
            e = await client.create_entity(
                type=type_,
                legal_name=legal_name,
                short_name=short_name,
                status=status,
                identifiers=ids,
            )
            await run.increment_rows(1)
            click.echo(json.dumps({"entity_id": str(e.entity_id)}))
    finally:
        await engine.dispose()


# ─── add-identifier ───


@registry.command("add-identifier")
@click.option("--entity", "entity_id", required=True)
@click.option("--namespace", required=True)
@click.option("--value", required=True)
def add_identifier_cmd(entity_id: str, namespace: str, value: str) -> None:
    """Attach an identifier to an existing entity."""
    asyncio.run(_add_identifier(UUID(entity_id), namespace, value))


async def _add_identifier(eid: UUID, ns: str, val: str) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with (
            ingestion_run(
                engine,
                source_id="manual",
                job_name="cli.registry.add_identifier",
            ) as run,
            session_scope(factory) as s,
        ):
            await EntityRegistryClient(s, run.id).add_identifier(eid, ns, val)
    finally:
        await engine.dispose()


# ─── resolve ───


@registry.command("resolve")
@click.option("--namespace", required=True)
@click.option("--value", required=True)
@click.option("--as-of", "as_of_str", default=None)
@click.option("--json", "json_out", is_flag=True)
def resolve_cmd(namespace: str, value: str, as_of_str: str | None, json_out: bool) -> None:
    """Resolve (namespace, value) to an entity_id."""
    as_of = _date.fromisoformat(as_of_str) if as_of_str else None
    asyncio.run(_resolve(namespace, value, as_of, json_out))


async def _resolve(ns: str, val: str, as_of: _date | None, json_out: bool) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as s:
            # Reads do not need ingestion_run attribution; pass 0 since the
            # client's read paths never reference _run_id.
            client = EntityRegistryClient(s, ingestion_run_id=0)
            eid = await client.resolve(ns, val, as_of=as_of)
        if json_out:
            click.echo(json.dumps({"entity_id": str(eid) if eid else None}))
        else:
            click.echo(str(eid) if eid else "(no match)")
    finally:
        await engine.dispose()


# ─── identifiers ───


@registry.command("identifiers")
@click.option("--entity", "entity_id", required=True)
@click.option("--as-of", "as_of_str", default=None)
@click.option("--json", "json_out", is_flag=True)
def identifiers_cmd(entity_id: str, as_of_str: str | None, json_out: bool) -> None:
    """List active identifiers for an entity."""
    as_of = _date.fromisoformat(as_of_str) if as_of_str else None
    asyncio.run(_identifiers(UUID(entity_id), as_of, json_out))


async def _identifiers(eid: UUID, as_of: _date | None, json_out: bool) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as s:
            ids = await EntityRegistryClient(s, ingestion_run_id=0).identifiers_for(eid, as_of)
        if json_out:
            click.echo(json.dumps(ids))
        else:
            click.echo("\n".join(f"{k}={v}" for k, v in ids.items()))
    finally:
        await engine.dispose()


# ─── search ───


@registry.command("search")
@click.argument("query")
@click.option("--type", "type_", multiple=True)
@click.option("--limit", type=int, default=20)
@click.option("--json", "json_out", is_flag=True)
def search_cmd(query: str, type_: tuple[str, ...], limit: int, json_out: bool) -> None:
    """Trigram-similarity search over legal/short names."""
    asyncio.run(_search(query, list(type_) or None, limit, json_out))


async def _search(query: str, types: list[str] | None, limit: int, json_out: bool) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as s:
            res = await EntityRegistryClient(s, 0).search(query, types=types, limit=limit)
        if json_out:
            payload = [
                {
                    "entity_id": str(m.entity.entity_id),
                    "legal_name": m.entity.legal_name,
                    "similarity": m.similarity,
                }
                for m in res
            ]
            click.echo(json.dumps(payload))
        else:
            for m in res:
                click.echo(f"{m.entity.entity_id}\t{m.similarity:.3f}\t{m.entity.legal_name}")
    finally:
        await engine.dispose()


# ─── expire-identifier ───


@registry.command("expire-identifier")
@click.option("--namespace", required=True)
@click.option("--value", required=True)
@click.option("--as-of", "as_of_str", required=True)
def expire_identifier_cmd(namespace: str, value: str, as_of_str: str) -> None:
    """Set valid_to=as_of (first invalid day) on the active identifier."""
    asyncio.run(_expire(namespace, value, _date.fromisoformat(as_of_str)))


async def _expire(ns: str, val: str, as_of: _date) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with (
            ingestion_run(
                engine,
                source_id="manual",
                job_name="cli.registry.expire_identifier",
            ) as run,
            session_scope(factory) as s,
        ):
            await EntityRegistryClient(s, run.id).expire_identifier(ns, val, as_of)
    finally:
        await engine.dispose()


# ─── assign-sector ───


@registry.command("assign-sector")
@click.option("--entity", "entity_id", required=True)
@click.option("--sector", "sector_id", required=True)
@click.option("--primary", is_flag=True)
def assign_sector_cmd(entity_id: str, sector_id: str, primary: bool) -> None:
    """Assign a sector to an entity (idempotent)."""
    asyncio.run(_assign(UUID(entity_id), sector_id, primary))


async def _assign(eid: UUID, sid: str, primary: bool) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with (
            ingestion_run(
                engine,
                source_id="manual",
                job_name="cli.registry.assign_sector",
            ) as run,
            session_scope(factory) as s,
        ):
            await EntityRegistryClient(s, run.id).assign_sector(eid, sid, is_primary=primary)
    finally:
        await engine.dispose()
