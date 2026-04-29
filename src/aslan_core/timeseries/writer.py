"""``ObservationWriter`` — fresh path (v0.4.0 Task 8).

Idempotent + field-change branches land in Tasks 9 + 10. Subjects
round-trip lands in Task 12. PII guards land in Task 11. Strict-mode
+ Prometheus + ``@traced`` land in Task 13.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import (
    Actor,
    AuditRecord,
    assert_actor_or_strict_raise,
    current_actor,
)
from aslan_core.audit import record as audit_record
from aslan_core.errors import (
    IdentifyingSeriesMetadataPii,
    IdentifyingSeriesMissingSubject,
    IdentifyingSeriesPiiInClearText,
    MetadataSchemaViolation,
    ObservationConflict,
    SeriesCodeConflict,
)
from aslan_core.observability import metrics
from aslan_core.observability.tracing import traced
from aslan_core.schemas.timeseries import (
    Frequency,
    ObservationIn,
    PiiClass,
    RestatementBasis,
    SeriesUpsertResult,
    SubjectRef,
    WriteCount,
)
from aslan_core.timeseries.pii import (
    find_pii_in_clear_text,
    find_pii_in_metadata,
)

# Codex F24, 2026-04-29 — numeric-string object keys are
# indistinguishable from array indices once flattened by
# `path_to_jsonb_set_text_array` for `jsonb_set`. Forbid them at
# upsert so the path-tuple → text[] conversion is lossless. Mirrors
# `pii._NUMERIC_STRING_KEY_RE` (deliberate duplication so the
# deletion-runtime helpers don't depend on writer.py).
_NUMERIC_KEY_RE: re.Pattern[str] = re.compile(r"^\d+$")


def _validate_no_numeric_string_keys(obj: Any, path: tuple[str | int, ...] = ()) -> None:
    """Codex F24, 2026-04-29 — recursively reject any object key (at
    any nesting depth in ``metadata``) whose string value matches
    ``^\\d+$``.

    Such keys are indistinguishable from array indices once flattened
    by :func:`path_to_jsonb_set_text_array` for ``jsonb_set``, and the
    Art. 17 scrub UPDATE could write the wrong leaf because Postgres
    ``jsonb_set`` dispatches by container kind at evaluation time, not
    by Python type. ``{"fields": {"0": "bob@b.com"}}`` (numeric-string
    object key) and ``{"fields": ["bob@b.com"]}`` (array index 0) both
    flatten to ``['fields', '0']``.

    Use a non-numeric prefix (``"item_0"``, ``"row_0"``, ``"_0"``)
    when you need to key by an ordinal-shaped string.

    Applies to ALL series regardless of ``pii_class`` so the metadata
    shape is uniform across the deletion runtime's surface (a
    ``pii_class='none'`` series may later be reclassified as
    identifying — keeping the validator unconditional avoids a
    backfill at reclassification time).
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                # Pydantic / asyncpg should have caught this upstream;
                # belt-and-braces in case raw JSON arrived via a future
                # bypass path.
                raise MetadataSchemaViolation(f"Non-string dict key at path {path}: {k!r}")
            if _NUMERIC_KEY_RE.match(k):
                raise MetadataSchemaViolation(
                    f"Numeric-string object keys forbidden in metadata at "
                    f"path {(*path, k)}: jsonb_set cannot disambiguate "
                    f"them from array indices during Art. 17 scrubbing. "
                    f"Use a non-numeric prefix (e.g. 'item_0' instead "
                    f"of '0')."
                )
            _validate_no_numeric_string_keys(v, (*path, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _validate_no_numeric_string_keys(v, (*path, i))
    # Primitives (int / float / bool / None / str): nothing to validate.


def _raise_if_pii_clear_text(field_name: str, value: str | None) -> None:
    findings = find_pii_in_clear_text(field_name, value)
    if findings:
        f = findings[0]
        raise IdentifyingSeriesPiiInClearText(
            f"{f.json_path} contains a {f.matched_pattern}-shaped pattern; "
            f"PII must live in ts.series_subject + metadata.subjects, not "
            f"clear-text columns. Matched: {f.matched_text!r}"
        )


def _raise_if_pii_in_metadata(metadata: dict[str, Any] | None) -> None:
    findings = find_pii_in_metadata(metadata)
    if findings:
        f = findings[0]
        raise IdentifyingSeriesMetadataPii(
            f"metadata[{f.json_path!r}] contains a {f.matched_pattern}-shaped "
            f"pattern; PII must live in metadata.subjects/series_subject, "
            f"not free-form metadata. Matched: {f.matched_text!r}"
        )


class ObservationWriter:
    """Writes into ``ts.series_catalog`` and ``ts.observation``.

    Single transaction per call. Mirrors ``DocumentStore`` /
    ``EntityRegistryClient``: the caller owns commit/rollback boundaries
    and the writer attaches its mutations + audit rows to the supplied
    :class:`AsyncSession`.
    """

    def __init__(self, session: AsyncSession, ingestion_run_id: int) -> None:
        self._s = session
        self._run_id = ingestion_run_id

    @traced("ObservationWriter.upsert_series")
    async def upsert_series(
        self,
        series_code: str,
        *,
        source_id: str,
        metric: str,
        frequency: Frequency,
        unit: str,
        entity_id: UUID | None = None,
        currency_code: str | None = None,
        restatement_basis: RestatementBasis = "nominal",
        accounting_standard: str | None = None,
        consolidation: str | None = None,
        period_type: str | None = None,
        description: str | None = None,
        pii_class: PiiClass = "none",
        metadata: dict[str, Any] | None = None,
        subjects: tuple[SubjectRef, ...] = (),
    ) -> SeriesUpsertResult:
        """Insert or update a row in ``ts.series_catalog``.

        Returns :class:`SeriesUpsertResult` with ``created=True`` on a
        fresh INSERT and ``created=False`` on idempotent / field-change
        paths (Tasks 9 + 10).
        """
        # Strict-mode early check (codex F3 from v0.3): raise BEFORE
        # any DB I/O if audit_strict=True and no actor is set. Lenient
        # mode falls through and audit.record() writes system:unknown.
        assert_actor_or_strict_raise()
        actor = current_actor()
        meta: dict[str, Any] = metadata if metadata is not None else {}

        # Codex F24, 2026-04-29 — runs for ALL pii_class values, not
        # just identifying. Numeric-string object keys are forbidden
        # everywhere because the metadata shape must be uniform across
        # the deletion runtime's surface.
        if metadata is not None:
            _validate_no_numeric_string_keys(metadata)

        if pii_class == "identifying":
            # Codex F4 + F18: forbid PII patterns in clear-text columns
            # and require at least one structured SubjectRef so Art. 17
            # deletion has a handle.
            _raise_if_pii_clear_text("series_code", series_code)
            _raise_if_pii_clear_text("description", description)
            if not subjects:
                # Note: the "no surviving subjects" path (subjects empty
                # AND no existing rows) collapses to "subjects is empty"
                # because ON CONFLICT DO NOTHING never deletes rows. If
                # a later v0.5 path adds explicit subject removal,
                # re-check the post-upsert ts.series_subject row count.
                raise IdentifyingSeriesMissingSubject(
                    "pii_class='identifying' requires at least one SubjectRef; "
                    "an identifying series with no subject handle is "
                    "structurally undeletable under Art. 17."
                )
            # Codex F18 + F19: scan metadata recursively (only top-level
            # ``subjects`` is skipped — ``fields`` IS scanned).
            _raise_if_pii_in_metadata(metadata)

        # Prometheus: count one upsert per call (fresh + idempotent +
        # field-change paths combined; the per-path outcome is already
        # captured in ``aslan_audit_events_total{operation=series.*}``).
        # The ``frequency`` label is bounded by the closed allow-list
        # ``_KNOWN_FREQUENCIES``; values outside the spec literal
        # collapse to ``"other"``.
        metrics.series_upserts.labels(
            source_id=source_id,
            frequency=metrics._normalize_metric_label(frequency, metrics._KNOWN_FREQUENCIES),
        ).inc()

        # Look up existing row by series_code.
        existing = (
            await self._s.execute(
                text(
                    "SELECT series_id, source_id, entity_id, metric, frequency, "
                    "       unit, currency_code, restatement_basis, "
                    "       accounting_standard, consolidation, period_type, "
                    "       description, pii_class, metadata, actor_id "
                    "FROM ts.series_catalog WHERE series_code = :code"
                ),
                {"code": series_code},
            )
        ).first()

        if existing is None:
            return await self._upsert_series_fresh(
                series_code=series_code,
                source_id=source_id,
                metric=metric,
                frequency=frequency,
                unit=unit,
                entity_id=entity_id,
                currency_code=currency_code,
                restatement_basis=restatement_basis,
                accounting_standard=accounting_standard,
                consolidation=consolidation,
                period_type=period_type,
                description=description,
                pii_class=pii_class,
                metadata=meta,
                actor=actor,
                subjects=subjects,
            )

        return await self._reconcile_existing(
            existing=existing,
            series_code=series_code,
            source_id=source_id,
            metric=metric,
            frequency=frequency,
            unit=unit,
            entity_id=entity_id,
            currency_code=currency_code,
            restatement_basis=restatement_basis,
            accounting_standard=accounting_standard,
            consolidation=consolidation,
            period_type=period_type,
            description=description,
            pii_class=pii_class,
            metadata=meta,
            actor=actor,
            subjects=subjects,
        )

    async def _reconcile_existing(
        self,
        *,
        existing: Any,
        series_code: str,
        source_id: str,
        metric: str,
        frequency: str,
        unit: str,
        entity_id: UUID | None,
        currency_code: str | None,
        restatement_basis: str,
        accounting_standard: str | None,
        consolidation: str | None,
        period_type: str | None,
        description: str | None,
        pii_class: str,
        metadata: dict[str, Any],
        actor: Actor | None,
        subjects: tuple[SubjectRef, ...],
    ) -> SeriesUpsertResult:
        """Reconcile a caller's intent against an existing
        ``ts.series_catalog`` row.

        Either:
        * idempotent hit → emit ``series.idempotent_hit``, no row update;
        * field change → run ``_upsert_series_field_change`` (UPDATE +
          ``series.update`` audit event), gated by the immutable-field
          guard when observations exist.

        Called from two sites: the ordinary "row already existed when we
        looked" branch of :meth:`upsert_series`, and the post-race
        fallback in :meth:`_upsert_series_fresh` when ``INSERT ... ON
        CONFLICT DO NOTHING`` returns no row (codex 2026-04-29 — another
        writer beat us between our SELECT and INSERT). Centralising the
        logic guarantees the race fallback walks identical idempotent /
        field-change branches as the ordinary path.
        """
        # Compare every relevant field. If all match, idempotent path.
        same = (
            existing.source_id == source_id
            and existing.entity_id == entity_id
            and existing.metric == metric
            and existing.frequency == frequency
            and existing.unit == unit
            and existing.currency_code == currency_code
            and existing.restatement_basis == restatement_basis
            and existing.accounting_standard == accounting_standard
            and existing.consolidation == consolidation
            and existing.period_type == period_type
            and existing.description == description
            and existing.pii_class == pii_class
            and existing.metadata == metadata
        )
        if same:
            # Subjects round-trip even on the idempotent catalog path so
            # newly-passed subject handles append (the catalog row is
            # untouched per F1, but ts.series_subject is additive via
            # ON CONFLICT DO NOTHING).
            await self._persist_subjects(int(existing.series_id), subjects, actor)
            existing_payload: dict[str, Any] = {
                "series_id": existing.series_id,
                "series_code": series_code,
                "source_id": existing.source_id,
                "metric": existing.metric,
                "frequency": existing.frequency,
                "unit": existing.unit,
                "pii_class": existing.pii_class,
                "restatement_basis": existing.restatement_basis,
                "metadata": existing.metadata,
            }
            # Codex F1: NO row update. Original creator's audit cols are
            # preserved forever. The event row carries the retrying
            # actor so forensics still see "user X retried at time Z".
            await audit_record(
                self._s,
                record=AuditRecord(
                    operation="series.idempotent_hit",
                    target_schema="ts",
                    target_table="series_catalog",
                    target_pk={"series_id": existing.series_id},
                    before=existing_payload,
                    after=existing_payload,
                    ingestion_run_id=self._run_id,
                    metadata={"returned_existing": True},
                ),
            )
            return SeriesUpsertResult(series_id=int(existing.series_id), created=False)

        return await self._upsert_series_field_change(
            existing=existing,
            series_code=series_code,
            source_id=source_id,
            metric=metric,
            frequency=frequency,
            unit=unit,
            entity_id=entity_id,
            currency_code=currency_code,
            restatement_basis=restatement_basis,
            accounting_standard=accounting_standard,
            consolidation=consolidation,
            period_type=period_type,
            description=description,
            pii_class=pii_class,
            metadata=metadata,
            actor=actor,
            subjects=subjects,
        )

    async def _upsert_series_field_change(
        self,
        *,
        existing: Any,
        series_code: str,
        source_id: str,
        metric: str,
        frequency: str,
        unit: str,
        entity_id: UUID | None,
        currency_code: str | None,
        restatement_basis: str,
        accounting_standard: str | None,
        consolidation: str | None,
        period_type: str | None,
        description: str | None,
        pii_class: str,
        metadata: dict[str, Any],
        actor: Actor | None,
        subjects: tuple[SubjectRef, ...],
    ) -> SeriesUpsertResult:
        """UPDATE-with-immutable-field-guard branch of
        :meth:`upsert_series`.

        Extracted from the inline body so the post-race fallback in
        :meth:`_upsert_series_fresh` can dispatch to the same code path
        without duplicating the immutable-field guard or the audit
        payload-assembly.
        """
        # If observations exist, block changing immutable-once-written
        # fields (source_id / frequency / unit — changing any of these
        # silently breaks time-series semantics).
        immutable_changes: list[str] = []
        if existing.source_id != source_id:
            immutable_changes.append("source_id")
        if existing.frequency != frequency:
            immutable_changes.append("frequency")
        if existing.unit != unit:
            immutable_changes.append("unit")
        if immutable_changes:
            obs_count = await self._s.scalar(
                text("SELECT COUNT(*) FROM ts.observation WHERE series_id = :sid"),
                {"sid": existing.series_id},
            )
            if obs_count and obs_count > 0:
                raise SeriesCodeConflict(
                    f"cannot change {', '.join(immutable_changes)} on "
                    f"series_code={series_code!r}: {obs_count} observations exist"
                )

        before_payload: dict[str, Any] = {
            "series_id": existing.series_id,
            "series_code": series_code,
            "source_id": existing.source_id,
            "metric": existing.metric,
            "frequency": existing.frequency,
            "unit": existing.unit,
            "currency_code": existing.currency_code,
            "restatement_basis": existing.restatement_basis,
            "accounting_standard": existing.accounting_standard,
            "consolidation": existing.consolidation,
            "period_type": existing.period_type,
            "description": existing.description,
            "pii_class": existing.pii_class,
            "metadata": existing.metadata,
        }
        await self._s.execute(
            text(
                "UPDATE ts.series_catalog SET "
                "  source_id = :sid, entity_id = :eid, metric = :metric, "
                "  frequency = :freq, unit = :unit, currency_code = :ccy, "
                "  restatement_basis = :rb, accounting_standard = :acct, "
                "  consolidation = :consol, period_type = :pt, "
                "  description = :desc, pii_class = :pii, "
                "  metadata = CAST(:meta AS JSONB), "
                "  actor_id = :aid, actor_kind = :ak, client_ip = :cip, "
                "  user_agent = :ua, request_id = :rid, "
                "  updated_at = now() "
                "WHERE series_id = :series_id"
            ),
            {
                "series_id": existing.series_id,
                "sid": source_id,
                "eid": entity_id,
                "metric": metric,
                "freq": frequency,
                "unit": unit,
                "ccy": currency_code,
                "rb": restatement_basis,
                "acct": accounting_standard,
                "consol": consolidation,
                "pt": period_type,
                "desc": description,
                "pii": pii_class,
                "meta": json.dumps(metadata),
                "aid": actor.actor_id if actor else None,
                "ak": actor.actor_kind if actor else None,
                "cip": actor.client_ip if actor else None,
                "ua": actor.user_agent if actor else None,
                "rid": actor.request_id if actor else None,
            },
        )
        await self._persist_subjects(int(existing.series_id), subjects, actor)
        after_payload: dict[str, Any] = {
            **before_payload,
            "source_id": source_id,
            "metric": metric,
            "frequency": frequency,
            "unit": unit,
            "currency_code": currency_code,
            "restatement_basis": restatement_basis,
            "accounting_standard": accounting_standard,
            "consolidation": consolidation,
            "period_type": period_type,
            "description": description,
            "pii_class": pii_class,
            "metadata": metadata,
        }
        await audit_record(
            self._s,
            record=AuditRecord(
                operation="series.update",
                target_schema="ts",
                target_table="series_catalog",
                target_pk={"series_id": existing.series_id},
                before=before_payload,
                after=after_payload,
                ingestion_run_id=self._run_id,
            ),
        )
        return SeriesUpsertResult(series_id=int(existing.series_id), created=False)

    async def _upsert_series_fresh(
        self,
        *,
        series_code: str,
        source_id: str,
        metric: str,
        frequency: str,
        unit: str,
        entity_id: UUID | None,
        currency_code: str | None,
        restatement_basis: str,
        accounting_standard: str | None,
        consolidation: str | None,
        period_type: str | None,
        description: str | None,
        pii_class: str,
        metadata: dict[str, Any],
        actor: Actor | None,
        subjects: tuple[SubjectRef, ...],
    ) -> SeriesUpsertResult:
        """Race-safe INSERT with audit cols stamped IN the statement.

        Codex F1: never a post-write UPDATE — that would let an
        idempotent retry rewrite the original creator's attribution.

        Codex 2026-04-29 Batch 2: ``ON CONFLICT (series_code) DO
        NOTHING`` makes the fresh path tolerant of two writers racing
        on the same new ``series_code``. Without it, the loser saw
        ``existing=None`` from the prior SELECT, then hit
        ``UniqueViolation`` on ``series_catalog_series_code_key``
        instead of falling through to the idempotent path. Postgres
        guarantees ``ON CONFLICT DO NOTHING`` blocks on an uncommitted
        conflicting INSERT until the other transaction commits or
        rolls back, so the post-race ``SELECT`` here always sees the
        winner's committed row.
        """
        sid_raw = await self._s.scalar(
            text(
                "INSERT INTO ts.series_catalog ("
                "  series_code, source_id, entity_id, metric, frequency, unit, "
                "  currency_code, restatement_basis, accounting_standard, "
                "  consolidation, period_type, description, pii_class, metadata, "
                "  actor_id, actor_kind, client_ip, user_agent, request_id"
                ") VALUES ("
                "  :code, :sid, :eid, :metric, :freq, :unit, "
                "  :ccy, :rb, :acct, :consol, :pt, :desc, :pii, "
                "  CAST(:meta AS JSONB), "
                "  :aid, :ak, :cip, :ua, :rid"
                ") ON CONFLICT (series_code) DO NOTHING "
                "RETURNING series_id"
            ),
            {
                "code": series_code,
                "sid": source_id,
                "eid": entity_id,
                "metric": metric,
                "freq": frequency,
                "unit": unit,
                "ccy": currency_code,
                "rb": restatement_basis,
                "acct": accounting_standard,
                "consol": consolidation,
                "pt": period_type,
                "desc": description,
                "pii": pii_class,
                "meta": json.dumps(metadata),
                "aid": actor.actor_id if actor else None,
                "ak": actor.actor_kind if actor else None,
                "cip": actor.client_ip if actor else None,
                "ua": actor.user_agent if actor else None,
                "rid": actor.request_id if actor else None,
            },
        )

        if sid_raw is None:
            # We lost the race. Another writer's INSERT on the same
            # ``series_code`` committed between our prior SELECT (which
            # saw None) and this INSERT. Re-fetch the now-committed
            # winner row and dispatch to ``_reconcile_existing`` so the
            # losing call walks identical idempotent / field-change
            # branches as the ordinary "row already existed" path.
            existing = (
                await self._s.execute(
                    text(
                        "SELECT series_id, source_id, entity_id, metric, frequency, "
                        "       unit, currency_code, restatement_basis, "
                        "       accounting_standard, consolidation, period_type, "
                        "       description, pii_class, metadata, actor_id "
                        "FROM ts.series_catalog WHERE series_code = :code"
                    ),
                    {"code": series_code},
                )
            ).first()
            # ``existing`` should never be None here: the only way
            # ON CONFLICT DO NOTHING fires is a committed conflicting
            # row (uncommitted conflicts block until the other tx
            # commits or rolls back; on rollback our INSERT proceeds).
            assert existing is not None, (
                f"ON CONFLICT DO NOTHING fired but no row found for series_code={series_code!r}"
            )
            return await self._reconcile_existing(
                existing=existing,
                series_code=series_code,
                source_id=source_id,
                metric=metric,
                frequency=frequency,
                unit=unit,
                entity_id=entity_id,
                currency_code=currency_code,
                restatement_basis=restatement_basis,
                accounting_standard=accounting_standard,
                consolidation=consolidation,
                period_type=period_type,
                description=description,
                pii_class=pii_class,
                metadata=metadata,
                actor=actor,
                subjects=subjects,
            )

        sid = int(sid_raw)
        await self._persist_subjects(sid, subjects, actor)
        after_payload: dict[str, Any] = {
            "series_id": sid,
            "series_code": series_code,
            "source_id": source_id,
            "metric": metric,
            "frequency": frequency,
            "unit": unit,
            "pii_class": pii_class,
            "restatement_basis": restatement_basis,
            "metadata": metadata,
        }
        await audit_record(
            self._s,
            record=AuditRecord(
                operation="series.upsert",
                target_schema="ts",
                target_table="series_catalog",
                target_pk={"series_id": sid},
                before=None,
                after=after_payload,
                ingestion_run_id=self._run_id,
            ),
        )
        return SeriesUpsertResult(series_id=sid, created=True)

    @traced("ObservationWriter.write")
    async def write(
        self,
        series_id: int,
        observations: Iterable[ObservationIn],
    ) -> WriteCount:
        """Bulk write into ``ts.observation``.

        Three-phase contract per spec §2 (codex F2 + F5 + F14 + F25):

        Phase 0 — in-memory dedup + pre-flight validation BEFORE any DB
        I/O. Each ``ObservationIn.metadata`` is walked for the
        forbidden-key contract (``^\\d+$`` numeric-string keys) and the
        batch is deduped by ``(series_id, ts, as_of)``. Same key with
        identical ``payload_hash()`` → drop the duplicate; same key
        with different ``payload_hash()`` → raise
        :class:`ObservationConflict` immediately.

        Phase 1 — per-``series_id`` advisory lock so concurrent writers
        on the same series serialise; concurrent writers on different
        series do not contend.

        Phase 2 — chunked ``INSERT ... ON CONFLICT DO NOTHING RETURNING
        ts, as_of`` (chunks of 500). For any chunk-key that did not
        return as inserted, SELECT the existing row's ``payload_hash``
        and either count it as ``unchanged`` (matching hash) or raise
        :class:`ObservationConflict` (mismatch). Whole batch rolls
        back on raise — partial writes corrupt forensic history.
        """
        # Strict-mode early-reject (codex F3 from v0.3): raise BEFORE
        # any I/O if audit_strict=True and no actor is set. Lenient
        # mode falls through.
        assert_actor_or_strict_raise()
        actor = current_actor()

        obs_list = list(observations)
        attempted = len(obs_list)

        # Phase 0 — in-memory dedup + pre-flight validation BEFORE
        # any DB I/O (codex F5 + F14 + F25, 2026-04-29).
        #
        # tz-aware enforcement and value/value_text exactly-one are
        # already enforced at ObservationIn construction by the
        # Pydantic validators. The numeric-string-key check on
        # observation metadata runs HERE because metadata is a
        # free-form dict that bypasses Pydantic value-level validation.
        # The forbidden-key contract (codex F24) applies uniformly to
        # BOTH ts.series_catalog.metadata AND ts.observation.metadata —
        # the Art. 17 deletion runtime walks both.
        seen: dict[tuple[int, datetime, datetime], str] = {}
        deduped: list[ObservationIn] = []
        for o in obs_list:
            if o.metadata is not None:
                _validate_no_numeric_string_keys(o.metadata)
            key = (series_id, o.ts, o.as_of)
            ph = o.payload_hash()
            if key in seen:
                if seen[key] != ph:
                    raise ObservationConflict(
                        "intra-batch same-key different-payload",
                        key=key,
                        existing_hash=seen[key],
                        new_hash=ph,
                    )
                # identical duplicate — drop the redundant copy.
                continue
            seen[key] = ph
            deduped.append(o)

        # Histogram + counter wired AFTER Phase 0 so a structurally
        # invalid batch (caught in Phase 0) does not bump the metric.
        metrics.observation_write_batch_size.observe(attempted)

        if not deduped:
            metrics.observation_writes.labels(source_id="unknown", kind="bulk").inc(attempted)
            return WriteCount(attempted=attempted, inserted=0, updated=0, unchanged=0)

        # Resolve source_id for the metric labels NOW (after Phase 0,
        # before Phase 1 + Phase 2 + audit). This is the first DB
        # round-trip — the F14 spy test passes because Phase 0 already
        # returned by this point, so a structurally-invalid batch
        # never reaches this lookup. The label is bounded by the
        # enumerated ``src.source.source_id`` set per spec.
        source_id_for_metrics_raw = await self._s.scalar(
            text("SELECT source_id FROM ts.series_catalog WHERE series_id = :sid"),
            {"sid": series_id},
        )
        source_id_for_metrics = source_id_for_metrics_raw or "unknown"

        # Wall-clock duration timer wraps Phase 1 + Phase 2 + audit so
        # operators see end-to-end latency including lock-wait time.
        # Observed in a finally so a raised ObservationConflict still
        # records its (failing) duration — the histogram label is the
        # source_id, not a success bool, so the failure case shows up
        # as a tail-bucket sample.
        _start = time.monotonic()
        try:
            # Phase 1 — per-series advisory lock for the txn lifetime so
            # concurrent writers on the same series serialise (concurrent
            # writers on different series do not contend).
            await self._s.execute(
                text("SELECT pg_advisory_xact_lock(  hashtextextended('ts.write:' || :sid, 0))"),
                {"sid": str(series_id)},
            )

            # Phase 2 — chunked INSERT in batches of 500. Resolve
            # conflicts by comparing payload_hash against the existing
            # row.
            chunk_size = 500
            inserted_keys_total: set[tuple[datetime, datetime]] = set()
            unchanged_keys_total: set[tuple[datetime, datetime]] = set()
            for chunk in (deduped[i : i + chunk_size] for i in range(0, len(deduped), chunk_size)):
                inserted_keys, unchanged_keys = await self._write_chunk(series_id, chunk, actor)
                inserted_keys_total |= inserted_keys
                unchanged_keys_total |= unchanged_keys

            inserted = len(inserted_keys_total)
            unchanged = len(unchanged_keys_total)

            # Audit emission (codex F3 + F6): one event per write() call,
            # plus per-key forensic rows in audit.observation_batch_keys.
            # Same transaction as the observation INSERTs — if the per-key
            # INSERT fails (e.g., CHECK violation, OOM) the whole batch
            # rolls back atomically.
            await self._emit_write_batch_audit(
                series_id=series_id,
                attempted=attempted,
                deduped=deduped,
                seen=seen,
                inserted_keys=inserted_keys_total,
                unchanged_keys=unchanged_keys_total,
                inserted=inserted,
                unchanged=unchanged,
                actor=actor,
            )
        finally:
            metrics.observation_write_duration.labels(
                source_id=source_id_for_metrics,
            ).observe(time.monotonic() - _start)

        # Prometheus: count rows attempted (NOT batch count). The
        # source_id label is the resolved value above; bounded
        # cardinality.
        metrics.observation_writes.labels(
            source_id=source_id_for_metrics,
            kind="bulk",
        ).inc(attempted)

        return WriteCount(
            attempted=attempted,
            inserted=inserted,
            updated=0,
            unchanged=unchanged,
        )

    async def _write_chunk(
        self,
        series_id: int,
        chunk: list[ObservationIn],
        actor: Actor | None,
    ) -> tuple[set[tuple[datetime, datetime]], set[tuple[datetime, datetime]]]:
        """Insert one chunk via a single multi-row ``INSERT ... VALUES
        (...), (...) ON CONFLICT DO NOTHING RETURNING``.

        Why multi-row VALUES (not ``executemany``): SQLAlchemy 2.0
        ``executemany`` over a raw ``text()`` statement drops
        ``RETURNING`` rows on the asyncpg dialect (the cursor returns
        ``ResourceClosedError`` on ``fetchall``). A single multi-row
        VALUES statement reliably preserves ``RETURNING`` and runs in
        one round-trip, which is what we want for chunks of 500 rows.

        Returns ``(inserted_keys, unchanged_keys)`` so the caller can
        tag per-key audit rows with the right ``action``. Same-key
        different-payload raises :class:`ObservationConflict` and rolls
        back the whole transaction.
        """
        chunk_hash_by_key: dict[tuple[datetime, datetime], str] = {}
        bind_params: dict[str, Any] = {"sid": series_id, "rid": self._run_id}
        actor_id = actor.actor_id if actor else None
        actor_kind = actor.actor_kind if actor else None
        client_ip = str(actor.client_ip) if (actor and actor.client_ip) else None
        user_agent = actor.user_agent if actor else None
        request_id = actor.request_id if actor else None
        bind_params.update(
            {
                "aid": actor_id,
                "ak": actor_kind,
                "cip": client_ip,
                "ua": user_agent,
                "rqid": request_id,
            }
        )
        rows_sql: list[str] = []
        for i, o in enumerate(chunk):
            ph = o.payload_hash()
            chunk_hash_by_key[(o.ts, o.as_of)] = ph
            bind_params[f"ts_{i}"] = o.ts
            bind_params[f"as_of_{i}"] = o.as_of
            bind_params[f"value_{i}"] = o.value
            bind_params[f"value_text_{i}"] = o.value_text
            bind_params[f"qf_{i}"] = o.quality_flag
            bind_params[f"ph_{i}"] = ph
            bind_params[f"meta_{i}"] = json.dumps(o.metadata)
            rows_sql.append(
                f"(:sid, :ts_{i}, :as_of_{i}, :value_{i}, :value_text_{i}, "
                f":qf_{i}, :rid, :ph_{i}, CAST(:meta_{i} AS JSONB), "
                f":aid, :ak, CAST(:cip AS INET), :ua, :rqid)"
            )
        # The VALUES clause is constructed from internal-only ``:name``
        # bind placeholders (e.g. ``:ts_42``); every user-supplied value
        # is bound via ``bind_params``. The dynamic concatenation only
        # affects the count of placeholder rows, not the placeholder
        # text itself — there is no path for caller input to reach the
        # SQL string. Mirrors registry/client.py's established
        # S608-suppression precedent for dynamic SET / IN-clause composition.
        result = await self._s.execute(
            text(
                "INSERT INTO ts.observation ("  # noqa: S608
                "  series_id, ts, as_of, value, value_text, quality_flag, "
                "  ingestion_run_id, payload_hash, metadata, "
                "  actor_id, actor_kind, client_ip, user_agent, request_id"
                ") VALUES "
                + ", ".join(rows_sql)
                + " ON CONFLICT (series_id, ts, as_of) DO NOTHING "
                "RETURNING ts, as_of"
            ),
            bind_params,
        )
        inserted_keys: set[tuple[datetime, datetime]] = {(r.ts, r.as_of) for r in result.fetchall()}
        chunk_keys = set(chunk_hash_by_key.keys())
        conflicted = chunk_keys - inserted_keys
        if not conflicted:
            return inserted_keys, set()

        # Read back existing rows' payload_hash for the conflicted keys.
        select_params: dict[str, Any] = {"sid": series_id}
        in_clauses: list[str] = []
        for i, (ts_v, as_of_v) in enumerate(conflicted):
            select_params[f"ts_{i}"] = ts_v
            select_params[f"as_of_{i}"] = as_of_v
            in_clauses.append(f"(:ts_{i}, :as_of_{i})")
        # IN-clause composed from internal index-derived placeholder
        # names; values flow through bind_params. See registry/client.py
        # for the same S608-suppression precedent.
        rows = (
            await self._s.execute(
                text(
                    "SELECT ts, as_of, payload_hash FROM ts.observation "  # noqa: S608
                    "WHERE series_id = :sid AND (ts, as_of) IN (" + ",".join(in_clauses) + ")"
                ),
                select_params,
            )
        ).all()
        existing_by_key = {(r.ts, r.as_of): r.payload_hash for r in rows}
        unchanged_keys: set[tuple[datetime, datetime]] = set()
        for k in conflicted:
            ph_new = chunk_hash_by_key[k]
            ph_existing = existing_by_key.get(k)
            if ph_existing is None:
                # Should not happen — the DO NOTHING fired but no row
                # found? Surface as a forensic-grade ObservationConflict.
                raise ObservationConflict(
                    "post-INSERT readback missing key — DB consistency bug",
                    key=(series_id, k[0], k[1]),
                    existing_hash=None,
                    new_hash=ph_new,
                )
            ph_existing_norm = ph_existing.strip()
            if ph_existing_norm != ph_new:
                raise ObservationConflict(
                    "DB row exists with different payload",
                    key=(series_id, k[0], k[1]),
                    existing_hash=ph_existing_norm,
                    new_hash=ph_new,
                )
            unchanged_keys.add(k)
        return inserted_keys, unchanged_keys

    async def _emit_write_batch_audit(
        self,
        *,
        series_id: int,
        attempted: int,
        deduped: list[ObservationIn],
        seen: dict[tuple[int, datetime, datetime], str],
        inserted_keys: set[tuple[datetime, datetime]],
        unchanged_keys: set[tuple[datetime, datetime]],
        inserted: int,
        unchanged: int,
        actor: Actor | None,
    ) -> None:
        """Emit one ``observation.write_batch`` audit event + per-key
        forensic rows (codex F3 + F6 + F16).

        Same transaction as the observation INSERTs — atomic
        rollback. The per-key INSERT runs as ``executemany``; if it
        fails (CHECK violation, connection drop) the audit.events row
        and the observation rows roll back together.
        """
        ts_values = [o.ts for o in deduped]
        as_of_values = [o.as_of for o in deduped]
        ts_min, ts_max = min(ts_values), max(ts_values)
        as_of_min, as_of_max = min(as_of_values), max(as_of_values)

        # Deterministic batch hash: sha256 of sorted-by-key
        # concatenation of per-row payload_hash bytes — independent of
        # input ordering. Same caller replaying the same batch
        # produces the same value.
        sorted_hashes = sorted(seen.values())
        batch_payload_hash = hashlib.sha256("".join(sorted_hashes).encode("ascii")).hexdigest()

        # Mirror occurred_at into both audit.events and
        # audit.observation_batch_keys so investigators can join on
        # (event_id, occurred_at) without ambiguity (codex F6).
        occurred_at = datetime.now(UTC)

        actor_id = actor.actor_id if actor else "system:unknown"
        actor_kind = actor.actor_kind if actor else "system"
        client_ip = str(actor.client_ip) if (actor and actor.client_ip) else None
        user_agent = actor.user_agent if actor else None
        request_id = actor.request_id if actor else None

        # Codex Batch 3 F1, 2026-04-29: ``batch_size`` MUST equal the
        # post-dedup row count so the invariant
        # ``COUNT(*) FROM observation_batch_keys WHERE event_id=:eid
        #   == metadata.batch_size`` holds. Previous shape conflated
        # the caller's request size with the persisted-key count and
        # broke the invariant for any batch containing intra-batch
        # identical-payload duplicates. ``attempted`` keeps the
        # caller's original request size for forensic visibility, and
        # ``duplicate_count`` makes the gap explicit when present.
        batch_size = len(deduped)
        duplicate_count = attempted - batch_size
        event_metadata: dict[str, Any] = {
            "batch_size": batch_size,
            "attempted": attempted,
            "series_id": series_id,
            "ts_min": ts_min.isoformat(),
            "ts_max": ts_max.isoformat(),
            "as_of_min": as_of_min.isoformat(),
            "as_of_max": as_of_max.isoformat(),
            "batch_payload_hash": batch_payload_hash,
            "inserted": inserted,
            "unchanged": unchanged,
            "ingestion_run_id": self._run_id,
        }
        if duplicate_count > 0:
            event_metadata["duplicate_count"] = duplicate_count
        event_id_raw = await self._s.scalar(
            text(
                "INSERT INTO audit.events ("
                "  occurred_at, actor_id, actor_kind, client_ip, user_agent, "
                "  request_id, ingestion_run_id, operation, target_schema, "
                "  target_table, target_pk, before, after, metadata"
                ") VALUES ("
                "  :oa, :aid, :ak, CAST(:cip AS INET), :ua, :rqid, :run, "
                "  'observation.write_batch', 'ts', 'observation', "
                "  CAST(:pk AS JSONB), NULL, NULL, CAST(:meta AS JSONB)"
                ") RETURNING event_id"
            ),
            {
                "oa": occurred_at,
                "aid": actor_id,
                "ak": actor_kind,
                "cip": client_ip,
                "ua": user_agent,
                "rqid": request_id,
                "run": self._run_id,
                "pk": json.dumps({"series_id": series_id}),
                "meta": json.dumps(event_metadata, default=str),
            },
        )
        event_id = int(event_id_raw) if event_id_raw is not None else 0

        # Bump the audit_events Prometheus counter to mirror the
        # invariant maintained by audit.recorder.record() — emitters
        # that bypass the recorder still count.
        metrics.audit_events.labels(
            operation=metrics._normalize_metric_label(
                "observation.write_batch", metrics._KNOWN_AUDIT_OPERATIONS
            ),
            actor_kind=actor_kind,
        ).inc()

        # Per-key forensic rows. action='inserted' for keys returned by
        # the INSERT RETURNING; 'unchanged' for keys that hit the
        # DO NOTHING path AND matched payload_hash.
        per_key_params: list[dict[str, Any]] = []
        for o in deduped:
            k = (o.ts, o.as_of)
            if k in inserted_keys:
                action = "inserted"
            elif k in unchanged_keys:
                action = "unchanged"
            else:  # pragma: no cover — defensive
                # Phase 2 either inserted or marked-unchanged every key,
                # else it raised ObservationConflict and we never reach
                # here. A miss would indicate a writer bug.
                raise ObservationConflict(
                    "audit-key labelling missed — writer bug",
                    key=(series_id, o.ts, o.as_of),
                    existing_hash=None,
                    new_hash=seen[(series_id, o.ts, o.as_of)],
                )
            per_key_params.append(
                {
                    "eid": event_id,
                    "oa": occurred_at,
                    "sid": series_id,
                    "ts": o.ts,
                    "as_of": o.as_of,
                    "ph": seen[(series_id, o.ts, o.as_of)],
                    "action": action,
                }
            )
        await self._s.execute(
            text(
                "INSERT INTO audit.observation_batch_keys "
                "(event_id, occurred_at, series_id, ts, as_of, payload_hash, action) "
                "VALUES (:eid, :oa, :sid, :ts, :as_of, :ph, :action)"
            ),
            per_key_params,
        )

    async def _persist_subjects(
        self,
        series_id: int,
        subjects: tuple[SubjectRef, ...],
        actor: Actor | None,
    ) -> None:
        """Insert subject handles into ``ts.series_subject`` in the
        same transaction as the catalog row.

        Idempotent at the ``(series_id, subject_id, role)`` level via
        ``ON CONFLICT DO NOTHING``: re-passing the same subjects on a
        subsequent call is a no-op. Removing subjects requires the
        Art. 17 deletion path (NOT v0.4 — lives in aslan-service).

        If a SubjectRef's role fails the migration-0013 CHECK, the
        INSERT raises and rolls back the whole upsert via the caller's
        transaction (codex F4 atomic same-transaction contract).
        """
        if not subjects:
            return
        await self._s.execute(
            text(
                "INSERT INTO ts.series_subject "
                "(series_id, subject_id, role, actor_id, actor_kind, request_id) "
                "VALUES (:sid, :subj, :role, :aid, :ak, :rid) "
                "ON CONFLICT (series_id, subject_id, role) DO NOTHING"
            ),
            [
                {
                    "sid": series_id,
                    "subj": s.subject_id,
                    "role": s.role,
                    "aid": actor.actor_id if actor else None,
                    "ak": actor.actor_kind if actor else None,
                    "rid": actor.request_id if actor else None,
                }
                for s in subjects
            ],
        )
