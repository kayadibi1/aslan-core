from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.documents.object_storage import ObjectStorageClient
from aslan_core.errors import DocumentDBError, DocumentNotFound, ObjectStoreError
from aslan_core.schemas.filing import AttachmentIn, Filing, PutFilingResult

_log = structlog.get_logger(__name__)

_DEFAULT_BUCKETS: dict[str, str] = {
    "kap": "aslan-filings",
    "tefas": "aslan-prospectuses",
    "news": "aslan-news",
    "manual": "aslan-filings",
}


class DocumentStore:
    """Persist filings to Postgres + object storage with the per-source-ref
    advisory-lock single-head invariant + content-addressed dedup.

    See ``crawl/docs/aslan-core-spec.md`` §5.5 for the full atomicity
    contract. See ``crawl/plans/aslan-core-v0.2.0-doc.md`` Architecture
    summary for the cleanup-at-every-failure-path invariant (manifest in
    PutFilingResult.object_keys).
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        object_client: ObjectStorageClient,
        ingestion_run_id: int,
        bucket_for: dict[str, str] | None = None,
    ) -> None:
        self._s = session
        self._oc = object_client
        self._run_id = ingestion_run_id
        self._bucket_for = bucket_for or _DEFAULT_BUCKETS

    # ─── reads ─────────────────────────────────────────────────────────

    async def get_filing(self, filing_id: UUID) -> Filing:
        row = (
            await self._s.execute(
                text(
                    "SELECT filing_id, source_id, source_filing_ref, entity_id, "
                    "       kind, subkind, title, language, published_at, "
                    "       period_start, period_end, source_url, is_amendment, "
                    "       previous_filing_id, primary_object_key, primary_mime, "
                    "       primary_sha256, primary_bytes, has_xbrl, xbrl_object_key, "
                    "       metadata, discovered_at, revision_no "
                    "FROM doc.filing WHERE filing_id = :fid"
                ),
                {"fid": filing_id},
            )
        ).one_or_none()
        if row is None:
            raise DocumentNotFound(str(filing_id))
        return _row_to_filing(row)

    async def find_by_source_ref(self, *, source_id: str, source_filing_ref: str) -> Filing | None:
        """Return the latest revision (highest revision_no) for the given
        (source_id, source_filing_ref). None if no revision exists."""
        row = (
            await self._s.execute(
                text(
                    "SELECT filing_id, source_id, source_filing_ref, entity_id, "
                    "       kind, subkind, title, language, published_at, "
                    "       period_start, period_end, source_url, is_amendment, "
                    "       previous_filing_id, primary_object_key, primary_mime, "
                    "       primary_sha256, primary_bytes, has_xbrl, xbrl_object_key, "
                    "       metadata, discovered_at, revision_no "
                    "FROM doc.filing "
                    "WHERE source_id = :sid AND source_filing_ref = :ref "
                    "ORDER BY revision_no DESC LIMIT 1"
                ),
                {"sid": source_id, "ref": source_filing_ref},
            )
        ).one_or_none()
        if row is None:
            return None
        return _row_to_filing(row)

    async def amendment_chain(self, filing_id: UUID) -> list[Filing]:
        """Return all revisions for the filing's source_ref, oldest first.
        Works regardless of which member of the chain is passed in."""
        rows = (
            await self._s.execute(
                text(
                    "SELECT filing_id, source_id, source_filing_ref, entity_id, "
                    "       kind, subkind, title, language, published_at, "
                    "       period_start, period_end, source_url, is_amendment, "
                    "       previous_filing_id, primary_object_key, primary_mime, "
                    "       primary_sha256, primary_bytes, has_xbrl, xbrl_object_key, "
                    "       metadata, discovered_at, revision_no "
                    "FROM doc.filing "
                    "WHERE (source_id, source_filing_ref) = ("
                    "    SELECT source_id, source_filing_ref "
                    "    FROM doc.filing WHERE filing_id = :fid"
                    ") "
                    "ORDER BY revision_no ASC"
                ),
                {"fid": filing_id},
            )
        ).all()
        if not rows:
            raise DocumentNotFound(str(filing_id))
        return [_row_to_filing(r) for r in rows]

    # ─── writes ────────────────────────────────────────────────────────

    async def put_filing(
        self,
        *,
        source_id: str,
        source_filing_ref: str,
        entity_id: UUID | None,
        kind: str,
        title: str,
        published_at: datetime,
        primary_bytes: bytes,
        primary_mime: str,
        primary_filename: str,
        attachments: list[AttachmentIn] | None = None,
        period_start: date | None = None,
        period_end: date | None = None,
        subkind: str | None = None,
        source_url: str | None = None,
        is_amendment: bool = False,
        previous_filing_id: UUID | None = None,
        has_xbrl: bool = False,
        xbrl_bytes: bytes | None = None,
        xbrl_filename: str | None = None,
        language: str = "tr",
        metadata: dict[str, Any] | None = None,
    ) -> PutFilingResult:
        """Atomic upsert on (source_id, primary_sha256) per spec §5.4 + §5.5,
        gated by a per-(source_id, source_filing_ref) advisory lock so
        concurrent amendment writers cannot fork the chain.

        Cleanup contract (codex 2026-04-28 review): every uploaded bucket
        key is recorded in ``_uploaded_keys`` (primary + xbrl + every
        attachment, once Task 15 lands). On ANY exception inside steps
        3-6, all tracked keys are best-effort deleted before re-raising.
        The returned ``PutFilingResult.object_keys`` exposes the manifest
        so the caller can pass it to ``release()`` on outer-transaction
        rollback. ``release()`` MUST NOT depend on ``doc.filing_attachment``
        rows, which may be invisible/gone post-rollback.
        """
        # I3: validate xbrl argument consistency before any I/O.
        if has_xbrl and (xbrl_bytes is None or xbrl_filename is None):
            raise ValueError(
                "has_xbrl=True requires both xbrl_bytes and xbrl_filename to be provided"
            )
        if (xbrl_bytes is not None or xbrl_filename is not None) and not has_xbrl:
            raise ValueError(
                "xbrl_bytes/xbrl_filename provided but has_xbrl=False — "
                "set has_xbrl=True or omit the xbrl arguments"
            )

        bucket = self._bucket_for.get(source_id, "aslan-filings")
        sha256 = hashlib.sha256(primary_bytes).hexdigest()
        filing_id = uuid4()
        primary_key = _format_object_key(
            source_id=source_id,
            entity_id=entity_id,
            published_at=published_at,
            filing_id=filing_id,
            filename=primary_filename,
        )

        _uploaded_keys: list[str] = []

        # Step 2: primary upload (failure here = no DB write attempted,
        # no cleanup needed; raise ObjectStoreError directly).
        try:
            await self._oc.put_object(
                bucket=bucket,
                key=primary_key,
                body=primary_bytes,
                content_type=primary_mime,
            )
        except Exception as e:
            raise ObjectStoreError(f"put_object failed: {e}") from e
        _uploaded_keys.append(primary_key)

        # Steps 3-6 wrapped in ONE try block: any failure deletes EVERY
        # uploaded key before re-raising.
        try:
            # Step 3: advisory lock — serializes amendment writers per
            # source_ref.
            await self._s.execute(
                text("SELECT pg_advisory_xact_lock(  hashtextextended(:src || ':' || :ref, 0))"),
                {"src": source_id, "ref": source_filing_ref},
            )

            # Step 3.5: optional XBRL upload (must happen BEFORE the
            # upsert so its key is bound to xbrl_object_key in the
            # INSERT). Tracked in _uploaded_keys for cleanup on failure.
            xbrl_key: str | None = None
            if has_xbrl and xbrl_bytes is not None and xbrl_filename is not None:
                xbrl_key = _format_object_key(
                    source_id=source_id,
                    entity_id=entity_id,
                    published_at=published_at,
                    filing_id=filing_id,
                    filename=xbrl_filename,
                )
                await self._oc.put_object(
                    bucket=bucket,
                    key=xbrl_key,
                    body=xbrl_bytes,
                    content_type="application/xml",
                )
                _uploaded_keys.append(xbrl_key)

            # Step 4: atomic upsert with revision_no advancement.
            result_row = (
                await self._s.execute(
                    text(
                        "WITH next_rev AS ("
                        "    SELECT COALESCE(MAX(revision_no), 0) + 1 AS rn "
                        "    FROM doc.filing "
                        "    WHERE source_id = :src AND source_filing_ref = :ref"
                        ") "
                        "INSERT INTO doc.filing ("
                        "    filing_id, source_id, source_filing_ref, entity_id, "
                        "    kind, subkind, title, language, published_at, "
                        "    period_start, period_end, source_url, is_amendment, "
                        "    previous_filing_id, primary_object_key, primary_mime, "
                        "    primary_sha256, primary_bytes, has_xbrl, xbrl_object_key, "
                        "    metadata, ingestion_run_id, revision_no"
                        ") "
                        "VALUES ("
                        "    :fid, :src, :ref, :eid, :kind, :subkind, :title, :lang, :pub, "
                        "    :ps, :pe, :url, :amend, :prev, :ok, :mime, :sha, :bytes, "
                        "    :hx, :xkey, COALESCE(:md, '{}')::jsonb, :run, "
                        "    (SELECT rn FROM next_rev)"
                        ") "
                        "ON CONFLICT (source_id, primary_sha256) DO UPDATE "
                        "SET metadata = doc.filing.metadata "
                        "RETURNING filing_id, revision_no, (xmax = 0) AS created"
                    ),
                    {
                        "fid": filing_id,
                        "src": source_id,
                        "ref": source_filing_ref,
                        "eid": entity_id,
                        "kind": kind,
                        "subkind": subkind,
                        "title": title,
                        "lang": language,
                        "pub": published_at,
                        "ps": period_start,
                        "pe": period_end,
                        "url": source_url,
                        "amend": is_amendment,
                        "prev": previous_filing_id,
                        "ok": primary_key,
                        "mime": primary_mime,
                        "sha": sha256,
                        "bytes": len(primary_bytes),
                        "hx": has_xbrl,
                        "xkey": xbrl_key,
                        "md": _json_metadata(metadata),
                        "run": self._run_id,
                    },
                )
            ).one()

            actual_filing_id: UUID = result_row.filing_id
            revision_no: int = result_row.revision_no
            created: bool = bool(result_row.created)
            # I4: is_revision=True only when a NEW row was inserted (created)
            # AND it's not the first revision. Dedup hits → False regardless.
            is_revision: bool = created and revision_no > 1

            # Step 5: hash-dedup hit — delete just-uploaded primary AND
            # xbrl blobs (they're duplicates of the existing row's).
            # Tracked keys are removed from the manifest.
            if not created:
                for k in list(_uploaded_keys):
                    try:
                        await self._oc.delete_object(bucket=bucket, key=k)
                    except Exception as e:
                        _log_orphan_cleanup_failed(bucket, k, e)
                    _uploaded_keys.remove(k)

            # Step 6: attachments — per-attachment upload + INSERT.
            # NO per-attachment cleanup; failures bubble to the outer except
            # which deletes EVERY uploaded key (primary + xbrl + however many
            # attachments uploaded). codex 2026-04-28: per-attachment cleanup
            # is unsound because the filing INSERT and attachment INSERTs all
            # live in the same uncommitted transaction.
            if attachments:
                await self._persist_attachments(
                    filing_id=actual_filing_id,
                    attachments=attachments,
                    bucket=bucket,
                    source_id=source_id,
                    entity_id=entity_id,
                    published_at=published_at,
                    _uploaded_keys=_uploaded_keys,
                )

            filing = await self.get_filing(actual_filing_id)
            return PutFilingResult(
                filing=filing,
                created=created,
                is_revision=is_revision,
                revision_no=revision_no,
                bucket=bucket,
                object_keys=list(_uploaded_keys),
            )

        except Exception as e:
            # Any failure in steps 3-6: delete every uploaded key.
            for k in _uploaded_keys:
                try:
                    await self._oc.delete_object(bucket=bucket, key=k)
                except Exception as cleanup_err:
                    _log_orphan_cleanup_failed(bucket, k, cleanup_err)
            # I5: Re-raise typed per spec §5.5. ObjectStoreError (e.g. from
            # the inner XBRL upload) passes through as a storage error; any
            # other exception is wrapped as DocumentDBError ("DB phase fail").
            if isinstance(e, ObjectStoreError):
                raise
            raise DocumentDBError(f"DocumentStore.put_filing failed during DB phase: {e}") from e

    async def _persist_attachments(
        self,
        *,
        filing_id: UUID,
        attachments: list[AttachmentIn],
        bucket: str,
        source_id: str,
        entity_id: UUID | None,
        published_at: datetime,
        _uploaded_keys: list[str],
    ) -> None:
        """Upload + INSERT each attachment. Each upload appends to
        _uploaded_keys BEFORE its INSERT so put_filing's outer cleanup loop
        (in the except block) sees every uploaded blob, even if a later
        attachment's INSERT raises.

        ON CONFLICT (filing_id, sha256) DO NOTHING makes per-attachment
        INSERTs idempotent — retries after partial failure reconcile by
        sha256 without duplicate rows.

        Does NOT have its own try/except — failures bubble to put_filing.
        """
        for att in attachments:
            att_sha = hashlib.sha256(att.bytes).hexdigest()
            att_key = _format_object_key(
                source_id=source_id,
                entity_id=entity_id,
                published_at=published_at,
                filing_id=filing_id,
                filename=att.filename,
            )
            # Upload first; track in manifest BEFORE the INSERT so the outer
            # cleanup knows about every uploaded blob even if the subsequent
            # INSERT raises.
            await self._oc.put_object(
                bucket=bucket,
                key=att_key,
                body=att.bytes,
                content_type=att.mime,
            )
            _uploaded_keys.append(att_key)

            await self._s.execute(
                text(
                    "INSERT INTO doc.filing_attachment ("
                    "  filing_id, object_key, mime, sha256, bytes, role, sequence"
                    ") VALUES ("
                    "  :fid, :ok, :mime, :sha, :bytes, :role, :seq"
                    ") "
                    "ON CONFLICT (filing_id, sha256) DO NOTHING"
                ),
                {
                    "fid": filing_id,
                    "ok": att_key,
                    "mime": att.mime,
                    "sha": att_sha,
                    "bytes": len(att.bytes),
                    "role": att.role,
                    "seq": att.sequence,
                },
            )

    async def attach_extracted_text(
        self, filing_id: UUID, text_body: str, *, lang: str = "tr"
    ) -> None:
        """Insert or replace the extracted text for a filing.

        ON CONFLICT (filing_id) DO UPDATE — latest extraction wins.
        Touches doc.filing_body.extracted_at on every call.
        """
        await self._s.execute(
            text(
                "INSERT INTO doc.filing_body (filing_id, body_text, body_lang, extracted_at) "
                "VALUES (:fid, :body, :lang, now()) "
                "ON CONFLICT (filing_id) DO UPDATE "
                "  SET body_text = EXCLUDED.body_text, "
                "      body_lang = EXCLUDED.body_lang, "
                "      extracted_at = now()"
            ),
            {"fid": filing_id, "body": text_body, "lang": lang},
        )


# ─── Module-private helpers ────────────────────────────────────────────


def _format_object_key(
    *,
    source_id: str,
    entity_id: UUID | None,
    published_at: datetime,
    filing_id: UUID,
    filename: str,
) -> str:
    eid = str(entity_id) if entity_id else "_unresolved"
    return (
        f"{source_id}/{eid}/"
        f"{published_at.year:04d}/{published_at.month:02d}/{published_at.day:02d}/"
        f"{filing_id}/{filename}"
    )


def _json_metadata(d: dict[str, Any] | None) -> str | None:
    if d is None:
        return None
    return json.dumps(d)


def _log_orphan_cleanup_failed(bucket: str, key: str, exc: Exception) -> None:
    """Structured-log helper for orphan cleanup failures."""
    _log.warning(
        "orphan_cleanup_failed",
        bucket=bucket,
        key=key,
        error=str(exc),
        error_type=type(exc).__name__,
    )


def _row_to_filing(row: Any) -> Filing:
    return Filing(
        filing_id=row.filing_id,
        source_id=row.source_id,
        source_filing_ref=row.source_filing_ref,
        entity_id=row.entity_id,
        kind=row.kind,
        subkind=row.subkind,
        title=row.title,
        language=row.language,
        published_at=row.published_at,
        period_start=row.period_start,
        period_end=row.period_end,
        source_url=row.source_url,
        is_amendment=row.is_amendment,
        previous_filing_id=row.previous_filing_id,
        primary_object_key=row.primary_object_key,
        primary_mime=row.primary_mime,
        primary_sha256=row.primary_sha256,
        primary_bytes=row.primary_bytes,
        has_xbrl=row.has_xbrl,
        xbrl_object_key=row.xbrl_object_key,
        metadata=row.metadata or {},
        discovered_at=row.discovered_at,
        revision_no=row.revision_no,
    )
