from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import date as _date
from typing import Any
from uuid import UUID

import click
from sqlalchemy import text

from aslan_core.config import Settings
from aslan_core.db.engine import create_engine
from aslan_core.db.session import create_session_factory, session_scope
from aslan_core.documents.client import DocumentStore
from aslan_core.documents.object_storage import (
    Aioboto3ObjectStorageClient,
    ObjectStorageClient,
)


def _default_object_client() -> ObjectStorageClient:
    """Build the default Aioboto3 object client from environment.

    Tests monkeypatch ``_object_client_factory`` (the module-level name
    bound to this function) to inject an ``InMemoryFake``. The reads do
    not call the bucket, but ``DocumentStore.__init__`` requires a client
    instance.
    """
    s = Settings()
    return Aioboto3ObjectStorageClient(
        endpoint_url=s.s3_endpoint,
        access_key=s.s3_access_key,
        secret_key=s.s3_secret_key,
        region=s.s3_region,
    )


# Module-level factory hook. Tests patch this attribute via
# ``monkeypatch.setattr(aslan_core.cli.doc, "_object_client_factory", lambda: fake)``
# so the in-process CliRunner uses an InMemoryFake instead of real S3.
_object_client_factory: Callable[[], ObjectStorageClient] = _default_object_client


@click.group()
def doc() -> None:
    """Document store reads, queries, and manual writes."""


# ─── Helpers ──────────────────────────────────────────────────────────────


def _json_or_human(payload: Any, json_flag: bool, *, human: str | None = None) -> None:
    """Emit ``payload`` as JSON when ``json_flag`` is set, otherwise the
    human-readable string ``human`` (defaults to ``str(payload)``)."""
    if json_flag:
        click.echo(json.dumps(payload, default=str))
    else:
        click.echo(human if human is not None else str(payload))


def _filing_to_jsonable(filing: Any) -> dict[str, Any]:
    """Pydantic v2 ``model_dump(mode='json')`` returns JSON-serializable types
    (UUIDs → str, datetimes → ISO strings)."""
    return dict(filing.model_dump(mode="json"))


# ─── Task 27: read commands ──────────────────────────────────────────────


@doc.command("get")
@click.argument("filing_id")
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON object.")
def get_cmd(filing_id: str, json_flag: bool) -> None:
    """Get a single filing by ``filing_id``."""
    asyncio.run(_get_impl(UUID(filing_id), json_flag))


async def _get_impl(filing_id: UUID, json_flag: bool) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        oc = _object_client_factory()
        async with session_scope(factory) as s:
            store = DocumentStore(s, object_client=oc, ingestion_run_id=0)
            filing = await store.get_filing(filing_id)
        payload = _filing_to_jsonable(filing)
        if json_flag:
            click.echo(json.dumps(payload))
        else:
            click.echo(f"{filing.filing_id}\t{filing.source_id}\t{filing.title}")
    finally:
        await engine.dispose()


@doc.command("find")
@click.option("--source-id", required=True)
@click.option("--source-ref", required=True)
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON object.")
def find_cmd(source_id: str, source_ref: str, json_flag: bool) -> None:
    """Find the latest revision for ``(source_id, source_filing_ref)``."""
    asyncio.run(_find_impl(source_id, source_ref, json_flag))


async def _find_impl(source_id: str, source_ref: str, json_flag: bool) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        oc = _object_client_factory()
        async with session_scope(factory) as s:
            store = DocumentStore(s, object_client=oc, ingestion_run_id=0)
            filing = await store.find_by_source_ref(
                source_id=source_id, source_filing_ref=source_ref
            )
        if filing is None:
            if json_flag:
                click.echo(json.dumps(None))
            else:
                click.echo("(no match)")
            return
        payload = _filing_to_jsonable(filing)
        if json_flag:
            click.echo(json.dumps(payload))
        else:
            click.echo(f"{filing.filing_id}\t{filing.source_id}\t{filing.title}")
    finally:
        await engine.dispose()


@doc.command("chain")
@click.argument("filing_id")
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON array.")
def chain_cmd(filing_id: str, json_flag: bool) -> None:
    """List every revision of ``filing_id``'s source_ref, oldest first."""
    asyncio.run(_chain_impl(UUID(filing_id), json_flag))


async def _chain_impl(filing_id: UUID, json_flag: bool) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        oc = _object_client_factory()
        async with session_scope(factory) as s:
            store = DocumentStore(s, object_client=oc, ingestion_run_id=0)
            chain = await store.amendment_chain(filing_id)
        rows = [_filing_to_jsonable(f) for f in chain]
        if json_flag:
            click.echo(json.dumps(rows))
        else:
            for f in chain:
                click.echo(f"{f.revision_no}\t{f.filing_id}\t{f.title}")
    finally:
        await engine.dispose()


# ─── Task 28: query commands (list, stats) ───────────────────────────────


@doc.command("list")
@click.option("--limit", type=int, default=20, show_default=True)
@click.option("--kind", "kind", default=None, help="Filter by ``doc.filing.kind``.")
@click.option(
    "--since",
    "since_str",
    default=None,
    metavar="YYYY-MM-DD",
    help="Only filings with ``published_at >= :since``.",
)
@click.option(
    "--entity",
    "entity_id",
    default=None,
    help="Filter by ``doc.filing.entity_id`` (UUID).",
)
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON array.")
def list_cmd(
    limit: int,
    kind: str | None,
    since_str: str | None,
    entity_id: str | None,
    json_flag: bool,
) -> None:
    """List filings with optional kind / since / entity filters."""
    since = _date.fromisoformat(since_str) if since_str else None
    eid = UUID(entity_id) if entity_id else None
    asyncio.run(_list_impl(limit, kind, since, eid, json_flag))


async def _list_impl(
    limit: int,
    kind: str | None,
    since: _date | None,
    entity_id: UUID | None,
    json_flag: bool,
) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as s:
            sql = (
                "SELECT filing_id, source_id, source_filing_ref, entity_id, kind, "
                "       title, published_at, revision_no, is_amendment "
                "FROM doc.filing WHERE 1=1"
            )
            params: dict[str, Any] = {}
            if kind is not None:
                sql += " AND kind = :kind"
                params["kind"] = kind
            if since is not None:
                sql += " AND published_at >= :since"
                params["since"] = since
            if entity_id is not None:
                sql += " AND entity_id = :eid"
                params["eid"] = entity_id
            sql += " ORDER BY published_at DESC, filing_id LIMIT :limit"
            params["limit"] = limit
            rows = (await s.execute(text(sql), params)).mappings().all()
        payload = [
            {
                "filing_id": str(r["filing_id"]),
                "source_id": r["source_id"],
                "source_filing_ref": r["source_filing_ref"],
                "entity_id": str(r["entity_id"]) if r["entity_id"] else None,
                "kind": r["kind"],
                "title": r["title"],
                "published_at": r["published_at"].isoformat() if r["published_at"] else None,
                "revision_no": r["revision_no"],
                "is_amendment": r["is_amendment"],
            }
            for r in rows
        ]
        if json_flag:
            click.echo(json.dumps(payload))
        else:
            for r in payload:
                click.echo(
                    f"{r['filing_id']}\t{r['source_id']}\t{r['kind']}\t"
                    f"v{r['revision_no']}\t{r['title']}"
                )
    finally:
        await engine.dispose()


@doc.command("stats")
@click.option(
    "--since",
    "since_str",
    default=None,
    metavar="YYYY-MM-DD",
    help="Only count filings with ``published_at >= :since``.",
)
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON array.")
def stats_cmd(since_str: str | None, json_flag: bool) -> None:
    """Counts grouped by ``(source_id, kind)``."""
    since = _date.fromisoformat(since_str) if since_str else None
    asyncio.run(_stats_impl(since, json_flag))


async def _stats_impl(since: _date | None, json_flag: bool) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as s:
            sql = "SELECT source_id, kind, COUNT(*) AS cnt FROM doc.filing"
            params: dict[str, Any] = {}
            if since is not None:
                sql += " WHERE published_at >= :since"
                params["since"] = since
            sql += " GROUP BY source_id, kind ORDER BY source_id, kind"
            rows = (await s.execute(text(sql), params)).mappings().all()
        payload = [
            {"source_id": r["source_id"], "kind": r["kind"], "count": int(r["cnt"])} for r in rows
        ]
        if json_flag:
            click.echo(json.dumps(payload))
        else:
            for r in payload:
                click.echo(f"{r['source_id']}\t{r['kind']}\t{r['count']}")
    finally:
        await engine.dispose()
