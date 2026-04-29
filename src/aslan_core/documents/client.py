from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.documents.object_storage import ObjectStorageClient
from aslan_core.errors import DocumentNotFound
from aslan_core.schemas.filing import Filing

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
