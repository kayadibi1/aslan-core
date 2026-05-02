from __future__ import annotations

import asyncio
import json
import mimetypes
from collections.abc import Callable
from datetime import date as _date
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import click
from sqlalchemy import text

from aslan_core.config import Settings
from aslan_core.db.engine import create_engine
from aslan_core.db.session import create_session_factory, session_scope
from aslan_core.documents.client import _DEFAULT_BUCKETS, DocumentStore
from aslan_core.documents.object_storage import (
    Aioboto3ObjectStorageClient,
    ObjectStorageClient,
)
from aslan_core.ingestion.run import ingestion_run


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
        access_key=s.s3_access_key.get_secret_value(),
        secret_key=s.s3_secret_key.get_secret_value(),
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


# ─── Task 29: put (manual filing insert) ──────────────────────────────────


def _parse_published_at(s: str) -> datetime:
    """Parse an ISO-8601 timestamp; reject naive datetimes (UTC required)."""
    # ``datetime.fromisoformat`` accepts trailing 'Z' on Python 3.11+.
    dt = (
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        if s.endswith("Z")
        else (datetime.fromisoformat(s))
    )
    if dt.tzinfo is None:
        raise click.BadParameter(
            "--published-at must be timezone-aware (UTC); append 'Z' or '+00:00'"
        )
    return dt


@doc.command("put")
@click.option("--source-id", required=True, help="Must reference an existing src.source row.")
@click.option("--source-ref", required=True, help="Source's per-row identifier.")
@click.option("--kind", required=True, help="e.g. 'news', 'material_event', 'financial_report'.")
@click.option("--title", required=True)
@click.option(
    "--published-at",
    "published_at_str",
    required=True,
    metavar="ISO-8601",
    help="Tz-aware ISO-8601 timestamp (e.g. 2026-04-28T12:00:00Z).",
)
@click.option(
    "--primary-file",
    "primary_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Local path to the filing's primary document.",
)
@click.option(
    "--primary-mime",
    "primary_mime",
    default=None,
    help="MIME type. Inferred from filename when omitted.",
)
@click.option("--language", default="tr", show_default=True)
@click.option("--subkind", default=None)
@click.option("--source-url", default=None)
@click.option("--entity", "entity_id_str", default=None, help="UUID of the resolved entity.")
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON manifest.")
def put_cmd(
    source_id: str,
    source_ref: str,
    kind: str,
    title: str,
    published_at_str: str,
    primary_file: str,
    primary_mime: str | None,
    language: str,
    subkind: str | None,
    source_url: str | None,
    entity_id_str: str | None,
    json_flag: bool,
) -> None:
    """Manually insert a filing from a local file.

    Reads ``--primary-file`` into memory, infers MIME if ``--primary-mime``
    is not passed, opens an ``ingestion_run`` row tied to ``--source-id``
    so the FK on ``doc.filing.ingestion_run_id`` is satisfied, and calls
    ``DocumentStore.put_filing``.
    """
    published_at = _parse_published_at(published_at_str)
    path = Path(primary_file)
    body = path.read_bytes()
    mime = primary_mime or _guess_mime(path)
    entity_id = UUID(entity_id_str) if entity_id_str else None
    asyncio.run(
        _put_impl(
            source_id=source_id,
            source_ref=source_ref,
            kind=kind,
            title=title,
            published_at=published_at,
            primary_filename=path.name,
            primary_bytes=body,
            primary_mime=mime,
            language=language,
            subkind=subkind,
            source_url=source_url,
            entity_id=entity_id,
            json_flag=json_flag,
        )
    )


def _guess_mime(path: Path) -> str:
    """Filename → MIME via stdlib ``mimetypes``; falls back to octet-stream."""
    guessed, _enc = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


async def _put_impl(
    *,
    source_id: str,
    source_ref: str,
    kind: str,
    title: str,
    published_at: datetime,
    primary_filename: str,
    primary_bytes: bytes,
    primary_mime: str,
    language: str,
    subkind: str | None,
    source_url: str | None,
    entity_id: UUID | None,
    json_flag: bool,
) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        oc = _object_client_factory()
        async with (
            ingestion_run(engine, source_id=source_id, job_name="cli.doc.put") as run,
            session_scope(factory) as s,
        ):
            store = DocumentStore(s, object_client=oc, ingestion_run_id=run.id)
            result = await store.put_filing(
                source_id=source_id,
                source_filing_ref=source_ref,
                entity_id=entity_id,
                kind=kind,
                title=title,
                published_at=published_at,
                primary_bytes=primary_bytes,
                primary_mime=primary_mime,
                primary_filename=primary_filename,
                language=language,
                subkind=subkind,
                source_url=source_url,
            )
            await run.increment_docs(1)
            await run.increment_bytes(len(primary_bytes))
        payload = {
            "filing_id": str(result.filing.filing_id),
            "created": result.created,
            "is_revision": result.is_revision,
            "revision_no": result.revision_no,
            "bucket": result.bucket,
            "object_keys": list(result.object_keys),
        }
        if json_flag:
            click.echo(json.dumps(payload))
        else:
            click.echo(
                f"{payload['filing_id']}\tcreated={payload['created']}\t"
                f"v{payload['revision_no']}\tbucket={payload['bucket']}"
            )
    finally:
        await engine.dispose()


# ─── Task 30: release ─────────────────────────────────────────────────────


@doc.command("release")
@click.argument("filing_id")
@click.option(
    "--keep-row",
    is_flag=True,
    help="Delete the bucket objects but leave the doc.filing row intact.",
)
def release_cmd(filing_id: str, keep_row: bool) -> None:
    """Delete a filing's bucket objects and (by default) its DB row.

    Reconstructs the put-time manifest from ``doc.filing`` columns
    (``primary_object_key``, ``xbrl_object_key``) and ``doc.filing_attachment``
    (every ``object_key`` for the filing), then deletes each from the bucket
    via ``oc.delete_object``. Each delete is best-effort: missing keys are
    fine (S3 semantics), other errors are logged via the same orphan-cleanup
    path that ``DocumentStore.release`` uses.

    Unlike ``DocumentStore.release(PutFilingResult)`` — which is the in-flight
    rollback path that runs against the put_filing manifest in memory — this
    CLI command queries the DB for the manifest, so it is safe to call any
    time after the put_filing transaction has committed. ``DELETE FROM
    doc.filing`` cascades through ``doc.filing_attachment`` and
    ``doc.filing_body`` (both declare ``ON DELETE CASCADE`` on ``filing_id``
    — see migrations 0006 and 0007).
    """
    asyncio.run(_release_impl(UUID(filing_id), keep_row))


async def _release_impl(filing_id: UUID, keep_row: bool) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        oc = _object_client_factory()
        async with session_scope(factory) as s:
            store = DocumentStore(s, object_client=oc, ingestion_run_id=0)
            # Raises DocumentNotFound on unknown filing_id — surfaced as a
            # nonzero exit by Click's default exception handler.
            filing = await store.get_filing(filing_id)
            bucket = _DEFAULT_BUCKETS.get(filing.source_id, "aslan-filings")

            keys: list[str] = [filing.primary_object_key]
            if filing.xbrl_object_key:
                keys.append(filing.xbrl_object_key)
            att_rows = (
                await s.execute(
                    text(
                        "SELECT object_key FROM doc.filing_attachment "
                        "WHERE filing_id = :fid ORDER BY sequence, attachment_id"
                    ),
                    {"fid": filing_id},
                )
            ).all()
            keys.extend(r.object_key for r in att_rows)

            if not keep_row:
                # Codex 2026-04-29 (F2): preflight the chain-FK before
                # any destructive op. doc.filing.previous_filing_id has
                # no ON DELETE CASCADE, so a release that orphans a
                # downstream revision raises IntegrityError on the
                # row DELETE. Block it here with a clear UX so neither
                # the bucket nor the row is touched.
                downstream = await s.scalar(
                    text("SELECT 1 FROM doc.filing WHERE previous_filing_id = :fid LIMIT 1"),
                    {"fid": filing_id},
                )
                if downstream:
                    raise click.ClickException(
                        f"cannot release {filing_id}: a later revision "
                        "references this filing via previous_filing_id. "
                        "Release the leaf revision first, or pass "
                        "--keep-row to keep the DB row and only clean "
                        "up bucket objects."
                    )

            # Codex 2026-04-29 (F4): blobs first, abort DB delete on
            # any blob failure. If we deleted the row first and then
            # a blob delete failed, the row + attachment manifest would
            # be gone and the orphan blob would be unrecoverable from
            # the operator's side. Order:
            #   1. delete blobs (best-effort, track failures)
            #   2. if any failed → abort DB delete; row + manifest
            #      stay queryable, operator reruns once storage is
            #      back. delete_object is S3-idempotent, so the
            #      already-deleted blobs no-op on retry.
            #   3. all blobs gone → DELETE rows + COMMIT.
            failed: list[tuple[str, str]] = []
            for key in keys:
                try:
                    await oc.delete_object(bucket=bucket, key=key)
                except Exception as e:
                    failed.append((key, str(e)))

            if failed:
                # Abort BEFORE touching the DB rows. The session_scope
                # context manager will rollback cleanly on raise.
                for key, err in failed:
                    click.echo(f"warning: failed to delete {key}: {err}", err=True)
                raise click.ClickException(
                    f"release {filing_id}: {len(failed)}/{len(keys)} blob delete(s) "
                    "failed — DB row left intact for retry. Re-run `aslan doc "
                    "release` once the storage error clears."
                )

            if not keep_row:
                # doc.filing_body and doc.filing_attachment both declare
                # ON DELETE CASCADE on filing_id (migrations 0006 + 0007),
                # so a single DELETE on doc.filing tears down the full row
                # set. The previous explicit DELETE on doc.filing_body was
                # redundant.
                await s.execute(
                    text("DELETE FROM doc.filing WHERE filing_id = :fid"),
                    {"fid": filing_id},
                )
                # session_scope commits at exit; explicit commit here
                # makes the row delete durable before the success echo.
                await s.commit()

        click.echo(
            f"released {filing_id}: deleted {len(keys)} object(s)"
            + (" (DB row kept)" if keep_row else " + DB row")
        )
    finally:
        await engine.dispose()
