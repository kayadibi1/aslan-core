from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import (
    AuditRecord,
    assert_actor_or_strict_raise,
    current_actor,
)
from aslan_core.audit import record as audit_record
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
        (source_id, source_filing_ref) — including filings where the ref
        appears as an alias in ``metadata.republished_as``.

        Codex 2026-04-29 (F6): when put_filing hits primary-hash dedup
        with a new source_filing_ref (spec §5.4 case B), the new ref is
        merged into the original row's ``metadata.republished_as`` array
        rather than creating a new row. Without alias resolution, a
        caller looking up by the new ref would get None — the filing
        becomes invisible via the documented lookup path. Resolve
        aliases here so source_ref idempotency holds end-to-end.

        Direct-ref matches outrank alias matches in the ORDER BY, so a
        ref that is BOTH a canonical source_filing_ref on one row AND
        an alias on another still resolves to the canonical row.
        """
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
                    "WHERE source_id = :sid AND ("
                    "       source_filing_ref = :ref "
                    "    OR metadata->'republished_as' @> to_jsonb(CAST(:ref AS TEXT))"
                    ") "
                    "ORDER BY (source_filing_ref = :ref) DESC, revision_no DESC LIMIT 1"
                ),
                {"sid": source_id, "ref": source_filing_ref},
            )
        ).one_or_none()
        if row is None:
            return None
        return _row_to_filing(row)

    async def _find_canonical_by_ref(
        self, *, source_id: str, source_filing_ref: str
    ) -> Filing | None:
        """Internal helper: latest revision with EXACT ``source_filing_ref``,
        ignoring republished aliases. Used by put_filing for the §5.4 case
        C auto-link — a republished alias should not chain a new revision
        across content boundaries."""
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

        Strict-mode early check (codex F3): raises AuditMissingActor
        BEFORE any blob upload or DB INSERT when audit_strict=True and
        no actor is set. This is the procurement-grade gate — strict
        mode is meaningful only if it catches missing actors before
        any side effect runs.
        """
        # Strict-mode early check fires before validation so a missing
        # actor takes precedence over arg-validation errors (the
        # caller learns about the contract violation first).
        assert_actor_or_strict_raise()
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

        # Codex 2026-04-29 (F3): object keys are disambiguated by
        # `attachments/{sequence:03d}/{filename}`, so two attachments
        # collide only when they share BOTH sequence AND filename. Reject
        # at the API boundary — silent overwrite would otherwise leave
        # one DB row's bytes stored under another row's key.
        #
        # Codex 2026-04-29 (F5): also reject duplicate-content
        # attachments (same sha256). The DB UNIQUE on (filing_id,
        # sha256) makes the second INSERT a DO NOTHING, but the
        # attachment was already uploaded under its disambiguated key,
        # leaving an orphan blob with no DB pointer. Cleanup paths
        # that reconstruct keys from doc.filing_attachment cannot find
        # it after a normal commit. Caller should dedup the list.
        if attachments:
            seen_pairs: set[tuple[int, str]] = set()
            seen_hashes: set[str] = set()
            for att in attachments:
                pair = (att.sequence, att.filename)
                if pair in seen_pairs:
                    raise ValueError(
                        "duplicate attachment sequence+filename: "
                        f"sequence={att.sequence}, filename={att.filename!r}. "
                        "Each attachment must have a unique (sequence, filename)."
                    )
                seen_pairs.add(pair)
                att_hash = hashlib.sha256(att.bytes).hexdigest()
                if att_hash in seen_hashes:
                    raise ValueError(
                        "duplicate attachment content (same sha256) within "
                        f"one filing: filename={att.filename!r}, "
                        f"sequence={att.sequence}. Two attachments with "
                        "identical bytes leave one upload orphaned by the "
                        "DB UNIQUE (filing_id, sha256) constraint. Dedup "
                        "the attachment list before passing."
                    )
                seen_hashes.add(att_hash)

        bucket = self._bucket_for.get(source_id, "aslan-filings")
        sha256 = hashlib.sha256(primary_bytes).hexdigest()
        filing_id = uuid4()
        primary_key = _format_object_key(
            source_id=source_id,
            entity_id=entity_id,
            published_at=published_at,
            filing_id=filing_id,
            role="primary",
            filename=primary_filename,
        )

        _uploaded_keys: list[str] = []

        # Steps 2-6 wrapped in ONE try block. Codex 2026-04-29 (F7):
        # the primary upload now lives INSIDE this try too, and the
        # key is appended to `_uploaded_keys` BEFORE the upload —
        # otherwise a cancellation that arrives between put_object's
        # bytes-stored ack and `.append()` (worker shutdown,
        # asyncio.CancelledError, etc.) would orphan the blob with
        # no manifest entry. delete_object is S3-idempotent so a key
        # tracked-but-not-uploaded is a harmless no-op delete on
        # cleanup.
        try:
            # Step 2: primary upload.
            _uploaded_keys.append(primary_key)
            try:
                await self._oc.put_object(
                    bucket=bucket,
                    key=primary_key,
                    body=primary_bytes,
                    content_type=primary_mime,
                )
            except Exception as e:
                raise ObjectStoreError(f"put_object failed: {e}") from e
            # Step 3: advisory lock — serializes amendment writers per
            # source_ref.
            await self._s.execute(
                text("SELECT pg_advisory_xact_lock(  hashtextextended(:src || ':' || :ref, 0))"),
                {"src": source_id, "ref": source_filing_ref},
            )

            # Step 3a (spec §5.4 case C): auto-detect prior latest revision
            # AFTER taking the advisory lock so the read is serialized with
            # concurrent writers. Fills caller-defaulted chain fields only —
            # an explicit non-default value is honored unchanged. If the new
            # bytes match a prior row's hash, ON CONFLICT (source_id,
            # primary_sha256) fires and these values are discarded.
            #
            # Use the canonical (direct-only) lookup, NOT the alias-resolving
            # public find_by_source_ref. A republished alias is content-
            # equivalent to its canonical row; new bytes under the alias
            # describe a different document and must not chain to the
            # canonical row's content history.
            prior = await self._find_canonical_by_ref(
                source_id=source_id, source_filing_ref=source_filing_ref
            )
            if prior is not None and prior.primary_sha256 != sha256:
                if previous_filing_id is None:
                    previous_filing_id = prior.filing_id
                if not is_amendment:
                    is_amendment = True

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
                    role="xbrl",
                    filename=xbrl_filename,
                )
                # Track BEFORE upload (codex F7 cancellation-resilience).
                _uploaded_keys.append(xbrl_key)
                await self._oc.put_object(
                    bucket=bucket,
                    key=xbrl_key,
                    body=xbrl_bytes,
                    content_type="application/xml",
                )

            # Step 3b (audit, codex F1+F7): capture the pre-state of any
            # canonical row that ON CONFLICT would touch. Three reasons:
            #   1) Distinguish case A (pure rerun → no-op, audit cols
            #      MUST stay frozen on the original creator) from case B
            #      (republished_as alias add → row IS mutated, audit
            #      cols last-writer-win on this call's actor).
            #   2) Build the case-B audit event's `before` snapshot so
            #      the alias-add event is self-contained for forensics
            #      even if the original filing.put event is pruned by
            #      retention later (codex F7).
            #   3) Skip the case-A no-op write entirely so audit cols
            #      never rewrite on a same-bytes same-source-ref retry.
            pre = (
                await self._s.execute(
                    text(
                        "SELECT filing_id, source_filing_ref, metadata, "
                        "       actor_id, actor_kind, client_ip, "
                        "       user_agent, request_id "
                        "FROM doc.filing "
                        "WHERE source_id = :src AND primary_sha256 = :sha "
                        "FOR UPDATE"
                    ),
                    {"src": source_id, "sha": sha256},
                )
            ).one_or_none()
            ac = _audit_cols()

            # Step 4: atomic upsert with revision_no advancement.
            # The DO UPDATE branch's audit-col SET is wrapped in the
            # same CASE as `metadata`: case A and the
            # already-in-republished_as branch keep doc.filing.actor_id;
            # case B (alias add) overwrites with EXCLUDED.actor_id so
            # the row's denormalised audit cols reflect the alias-adder
            # (last-writer-wins per spec §"row audit cols = last
            # writer").
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
                        "    metadata, ingestion_run_id, revision_no, "
                        "    actor_id, actor_kind, client_ip, user_agent, request_id"
                        ") "
                        "VALUES ("
                        "    :fid, :src, :ref, :eid, :kind, :subkind, :title, :lang, :pub, "
                        "    :ps, :pe, :url, :amend, :prev, :ok, :mime, :sha, :bytes, "
                        "    :hx, :xkey, COALESCE(:md, '{}')::jsonb, :run, "
                        "    (SELECT rn FROM next_rev), "
                        "    :actor_id, :actor_kind, :client_ip, :user_agent, :request_id"
                        ") "
                        "ON CONFLICT (source_id, primary_sha256) DO UPDATE "
                        "SET metadata = CASE "
                        # Pure rerun (case A): same source_filing_ref, same
                        # bytes — no-op merge.
                        "    WHEN doc.filing.source_filing_ref "
                        "         = EXCLUDED.source_filing_ref "
                        "    THEN doc.filing.metadata "
                        # Idempotency: the new ref is already recorded in
                        # republished_as → no-op.
                        "    WHEN COALESCE("
                        "             doc.filing.metadata->'republished_as', "
                        "             '[]'::jsonb"
                        "         ) @> to_jsonb(ARRAY[EXCLUDED.source_filing_ref]) "
                        "    THEN doc.filing.metadata "
                        # Case B: identical bytes, NEW source_filing_ref —
                        # append into republished_as. Original metadata
                        # preserved (jsonb_set with create_missing=true only
                        # sets the one key).
                        "    ELSE jsonb_set("
                        "        doc.filing.metadata, "
                        "        '{republished_as}', "
                        "        COALESCE("
                        "            doc.filing.metadata->'republished_as', "
                        "            '[]'::jsonb"
                        "        ) || to_jsonb(EXCLUDED.source_filing_ref), "
                        "        true"
                        "    ) "
                        "END, "
                        # Audit cols mirror the metadata CASE: only case B
                        # (alias add) is a real mutation; case A and
                        # already-aliased calls preserve the canonical
                        # row's audit cols.
                        "actor_id = CASE "
                        "    WHEN doc.filing.source_filing_ref "
                        "         = EXCLUDED.source_filing_ref "
                        "    THEN doc.filing.actor_id "
                        "    WHEN COALESCE("
                        "             doc.filing.metadata->'republished_as', "
                        "             '[]'::jsonb"
                        "         ) @> to_jsonb(ARRAY[EXCLUDED.source_filing_ref]) "
                        "    THEN doc.filing.actor_id "
                        "    ELSE EXCLUDED.actor_id "
                        "END, "
                        "actor_kind = CASE "
                        "    WHEN doc.filing.source_filing_ref "
                        "         = EXCLUDED.source_filing_ref "
                        "    THEN doc.filing.actor_kind "
                        "    WHEN COALESCE("
                        "             doc.filing.metadata->'republished_as', "
                        "             '[]'::jsonb"
                        "         ) @> to_jsonb(ARRAY[EXCLUDED.source_filing_ref]) "
                        "    THEN doc.filing.actor_kind "
                        "    ELSE EXCLUDED.actor_kind "
                        "END, "
                        "client_ip = CASE "
                        "    WHEN doc.filing.source_filing_ref "
                        "         = EXCLUDED.source_filing_ref "
                        "    THEN doc.filing.client_ip "
                        "    WHEN COALESCE("
                        "             doc.filing.metadata->'republished_as', "
                        "             '[]'::jsonb"
                        "         ) @> to_jsonb(ARRAY[EXCLUDED.source_filing_ref]) "
                        "    THEN doc.filing.client_ip "
                        "    ELSE EXCLUDED.client_ip "
                        "END, "
                        "user_agent = CASE "
                        "    WHEN doc.filing.source_filing_ref "
                        "         = EXCLUDED.source_filing_ref "
                        "    THEN doc.filing.user_agent "
                        "    WHEN COALESCE("
                        "             doc.filing.metadata->'republished_as', "
                        "             '[]'::jsonb"
                        "         ) @> to_jsonb(ARRAY[EXCLUDED.source_filing_ref]) "
                        "    THEN doc.filing.user_agent "
                        "    ELSE EXCLUDED.user_agent "
                        "END, "
                        "request_id = CASE "
                        "    WHEN doc.filing.source_filing_ref "
                        "         = EXCLUDED.source_filing_ref "
                        "    THEN doc.filing.request_id "
                        "    WHEN COALESCE("
                        "             doc.filing.metadata->'republished_as', "
                        "             '[]'::jsonb"
                        "         ) @> to_jsonb(ARRAY[EXCLUDED.source_filing_ref]) "
                        "    THEN doc.filing.request_id "
                        "    ELSE EXCLUDED.request_id "
                        "END "
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
                        **ac,
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
            #
            # Dedup-hit guard (codex 2026-04-29): skip attachments entirely
            # when `created=False`. The existing filing already owns its
            # attachments; running the attachment phase here would (a) build
            # deterministic keys from the existing filing_id and overwrite
            # blobs the existing row owns if filenames collide, and (b)
            # append those keys to `_uploaded_keys`, luring `release(result)`
            # into deleting blobs the caller never inserted. Case A (pure
            # rerun) and case B (republished_as) both have "this content
            # already exists; we are not adding anything new" semantics.
            if attachments and created:
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

            # Step 7 (audit): emit the appropriate event based on which
            # case fired. Event categories per spec §5.4:
            #
            #   - created=True              → filing.put (fresh INSERT, may
            #                                 be revision_no > 1 case C
            #                                 chain advance)
            #   - created=False, pre.ref==new ref → filing.dedup_hit (case A,
            #                                 true no-op)
            #   - created=False, pre.ref!=new ref → filing.republished_alias_added
            #                                 (case B, alias added; row's
            #                                 audit cols just got
            #                                 overwritten by this actor)
            after_audit = _filing_audit_payload(filing)
            after_audit["attachments"] = [
                {"sequence": a.sequence, "filename": a.filename, "bytes": len(a.bytes)}
                for a in (attachments or [])
            ]
            if created:
                op = "filing.put"
                before_audit: dict[str, Any] | None = None
            else:
                # pre is non-None on the not-created path: a hash-dedup
                # hit means an existing canonical row matched.
                assert pre is not None
                before_audit = {
                    "filing_id": str(pre.filing_id),
                    "source_filing_ref": pre.source_filing_ref,
                    "metadata": pre.metadata or {},
                    "actor_id": pre.actor_id,
                    "actor_kind": pre.actor_kind,
                }
                if pre.source_filing_ref == source_filing_ref:
                    op = "filing.dedup_hit"
                else:
                    op = "filing.republished_alias_added"
            await audit_record(
                self._s,
                record=AuditRecord(
                    operation=op,
                    target_schema="doc",
                    target_table="filing",
                    target_pk={"filing_id": str(actual_filing_id)},
                    before=before_audit,
                    after=after_audit,
                    metadata={
                        "revision_no": revision_no,
                        "primary_sha256": sha256,
                        "primary_size_bytes": len(primary_bytes),
                        "returned_existing": not created,
                    },
                    ingestion_run_id=self._run_id,
                ),
            )

            return PutFilingResult(
                filing=filing,
                created=created,
                is_revision=is_revision,
                revision_no=revision_no,
                bucket=bucket,
                object_keys=list(_uploaded_keys),
            )

        except BaseException as e:
            # Codex 2026-04-29 (F7): catch BaseException, not Exception,
            # so asyncio.CancelledError (3.11+ inherits BaseException),
            # KeyboardInterrupt, and SystemExit also trigger blob cleanup.
            # Worker shutdowns and request timeouts cancel pending tasks;
            # without this, every cancelled put_filing past the primary
            # upload leaves orphaned blobs with no manifest returned.
            for k in _uploaded_keys:
                try:
                    await self._oc.delete_object(bucket=bucket, key=k)
                except Exception as cleanup_err:
                    _log_orphan_cleanup_failed(bucket, k, cleanup_err)
            # I5 + F7: re-raise typed per spec §5.5. CancelledError /
            # KeyboardInterrupt / SystemExit propagate unchanged — they
            # are control-flow signals, not "DB phase fail" conditions.
            # ObjectStoreError passes through as a storage error.
            # Anything else (Exception subclass) wraps as DocumentDBError.
            if isinstance(e, BaseException) and not isinstance(e, Exception):
                raise
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
                role=f"attachments/{att.sequence:03d}",
                filename=att.filename,
            )
            # Track in manifest BEFORE upload (codex F7 cancellation-
            # resilience) AND before the INSERT so the outer cleanup
            # knows about every uploaded blob even if put_object cancels
            # mid-flight or the subsequent INSERT raises.
            _uploaded_keys.append(att_key)
            await self._oc.put_object(
                bucket=bucket,
                key=att_key,
                body=att.bytes,
                content_type=att.mime,
            )

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

    async def release(self, result: PutFilingResult) -> None:
        """Delete every in-bucket object recorded in the put_filing manifest.

        Caller pattern (outer-transaction rollback):

            result = await store.put_filing(...)
            try:
                ...  # caller's outer work that may need to roll back
            except SomeError:
                await store.release(result)  # clean up bucket FIRST
                raise  # ... and let the rollback discard the DB rows

        Why the manifest (codex 2026-04-28): a Filing-based release that
        queried doc.filing_attachment for keys is unsound, because those
        rows may be invisible (uncommitted) or gone (already rolled back)
        at release-time. PutFilingResult.object_keys captures every
        uploaded key durably during put_filing — works regardless of what
        the surrounding transaction is doing.

        Best-effort per key; logs orphans on individual delete failure but
        does NOT raise.

        Emits a ``filing.release`` audit event BEFORE the blob deletes so
        the manifest is preserved as the forensic snapshot. If the
        caller's outer transaction is rolled back the event rolls back
        with it (consistent with the doc.filing row's lifecycle); if
        the caller has already committed, the audit event is durable.
        """
        assert_actor_or_strict_raise()
        # Emit audit event first so the manifest is durable in the
        # session before any side-effecting blob delete runs.
        await audit_record(
            self._s,
            record=AuditRecord(
                operation="filing.release",
                target_schema="doc",
                target_table="filing",
                target_pk={"filing_id": str(result.filing.filing_id)},
                before={
                    "filing_id": str(result.filing.filing_id),
                    "bucket": result.bucket,
                    "object_keys": list(result.object_keys),
                    "primary_sha256": result.filing.primary_sha256,
                },
                after=None,
                metadata={"key_count": len(result.object_keys)},
                ingestion_run_id=self._run_id,
            ),
        )
        for key in result.object_keys:
            try:
                await self._oc.delete_object(bucket=result.bucket, key=key)
            except Exception as e:
                _log_orphan_cleanup_failed(result.bucket, key, e)

    async def attach_extracted_text(
        self, filing_id: UUID, text_body: str, *, lang: str = "tr"
    ) -> None:
        """Insert or replace the extracted text for a filing.

        ON CONFLICT (filing_id) DO UPDATE — latest extraction wins.
        Touches doc.filing_body.extracted_at on every call.

        Each call is a fresh write of body content (extraction
        algorithms improve over time → re-running is meaningful), so
        last-writer-wins on the row's audit cols and a fresh
        filing_body.create / filing_body.update audit event is emitted
        per call. The ``after`` snapshot records body length only — not
        the body text itself — so audit rows stay compact.
        """
        assert_actor_or_strict_raise()
        ac = _audit_cols()
        result = (
            await self._s.execute(
                text(
                    "INSERT INTO doc.filing_body "
                    "  (filing_id, body_text, body_lang, extracted_at, "
                    "   actor_id, actor_kind, client_ip, user_agent, request_id) "
                    "VALUES (:fid, :body, :lang, now(), "
                    "        :actor_id, :actor_kind, :client_ip, "
                    "        :user_agent, :request_id) "
                    "ON CONFLICT (filing_id) DO UPDATE "
                    "  SET body_text    = EXCLUDED.body_text, "
                    "      body_lang    = EXCLUDED.body_lang, "
                    "      extracted_at = now(), "
                    "      actor_id     = EXCLUDED.actor_id, "
                    "      actor_kind   = EXCLUDED.actor_kind, "
                    "      client_ip    = EXCLUDED.client_ip, "
                    "      user_agent   = EXCLUDED.user_agent, "
                    "      request_id   = EXCLUDED.request_id "
                    "RETURNING (xmax = 0) AS created"
                ),
                {"fid": filing_id, "body": text_body, "lang": lang, **ac},
            )
        ).one()
        created = bool(result.created)
        op = "filing_body.create" if created else "filing_body.update"
        await audit_record(
            self._s,
            record=AuditRecord(
                operation=op,
                target_schema="doc",
                target_table="filing_body",
                target_pk={"filing_id": str(filing_id)},
                before=None,
                after={
                    "filing_id": str(filing_id),
                    "body_lang": lang,
                    "body_size_chars": len(text_body),
                },
                metadata={"returned_existing": False},
                ingestion_run_id=self._run_id,
            ),
        )


# ─── Module-private helpers ────────────────────────────────────────────


def _format_object_key(
    *,
    source_id: str,
    entity_id: UUID | None,
    published_at: datetime,
    filing_id: UUID,
    role: str,
    filename: str,
) -> str:
    """Build the bucket object key for a filing component.

    The ``role`` segment disambiguates primary / xbrl / per-attachment
    blobs that share a filename. Without it, an attachment named
    ``main.html`` would overwrite the primary ``main.html`` blob,
    leaving DB metadata pointing at the wrong bytes (codex 2026-04-29).

    Use ``role="primary"`` / ``role="xbrl"`` for those phases. For
    attachments, embed the sequence so two attachments with the same
    filename but different sequences do not collide:
    ``role=f"attachments/{att.sequence:03d}"``.
    """
    eid = str(entity_id) if entity_id else "_unresolved"
    return (
        f"{source_id}/{eid}/"
        f"{published_at.year:04d}/{published_at.month:02d}/{published_at.day:02d}/"
        f"{filing_id}/{role}/{filename}"
    )


def _json_metadata(d: dict[str, Any] | None) -> str | None:
    if d is None:
        return None
    return json.dumps(d)


def _audit_cols() -> dict[str, Any]:
    """Build actor_id/actor_kind/client_ip/user_agent/request_id bind
    params for the current ContextVar actor (None-safe).

    Same pattern as ``aslan_core.registry.client._audit_cols``: stamps
    audit cols inside the row INSERT/UPDATE so we never need a
    post-write second statement (codex F1).
    """
    a = current_actor()
    if a is None:
        return {
            "actor_id": None,
            "actor_kind": None,
            "client_ip": None,
            "user_agent": None,
            "request_id": None,
        }
    return {
        "actor_id": a.actor_id,
        "actor_kind": a.actor_kind,
        "client_ip": a.client_ip,
        "user_agent": a.user_agent,
        "request_id": a.request_id,
    }


def _filing_audit_payload(f: Filing) -> dict[str, Any]:
    """Subset of the Filing row safe to embed in audit.events.after.

    Excludes ``primary_bytes`` / large blobs by definition (Filing
    schema only carries the size int, not the body) — keeps audit
    rows compact and avoids duplicating filing payloads in the audit
    log.
    """
    return {
        "filing_id": str(f.filing_id),
        "source_id": f.source_id,
        "source_filing_ref": f.source_filing_ref,
        "entity_id": str(f.entity_id) if f.entity_id else None,
        "kind": f.kind,
        "subkind": f.subkind,
        "title": f.title,
        "language": f.language,
        "published_at": f.published_at.isoformat(),
        "primary_object_key": f.primary_object_key,
        "primary_mime": f.primary_mime,
        "primary_sha256": f.primary_sha256,
        "primary_size_bytes": f.primary_bytes,
        "has_xbrl": f.has_xbrl,
        "xbrl_object_key": f.xbrl_object_key,
        "metadata": f.metadata or {},
        "revision_no": f.revision_no,
        "is_amendment": f.is_amendment,
        "previous_filing_id": str(f.previous_filing_id) if f.previous_filing_id else None,
    }


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
