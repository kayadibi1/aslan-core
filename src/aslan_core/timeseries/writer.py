"""``ObservationWriter`` — fresh path (v0.4.0 Task 8).

Idempotent + field-change branches land in Tasks 9 + 10. Subjects
round-trip lands in Task 12. PII guards land in Task 11. Strict-mode
+ Prometheus + ``@traced`` land in Task 13.
"""

from __future__ import annotations

import json
import re
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
    SeriesCodeConflict,
)
from aslan_core.observability import metrics
from aslan_core.observability.tracing import traced
from aslan_core.schemas.timeseries import (
    Frequency,
    PiiClass,
    RestatementBasis,
    SeriesUpsertResult,
    SubjectRef,
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
