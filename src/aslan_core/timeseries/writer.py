"""``ObservationWriter`` — fresh path (v0.4.0 Task 8).

Idempotent + field-change branches land in Tasks 9 + 10. Subjects
round-trip lands in Task 12. PII guards land in Task 11. Strict-mode
+ Prometheus + ``@traced`` land in Task 13.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import (
    Actor,
    AuditRecord,
    current_actor,
)
from aslan_core.audit import record as audit_record
from aslan_core.observability.tracing import traced
from aslan_core.schemas.timeseries import (
    Frequency,
    PiiClass,
    RestatementBasis,
    SeriesUpsertResult,
    SubjectRef,
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
        actor = current_actor()
        meta: dict[str, Any] = metadata if metadata is not None else {}

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
            and existing.metadata == meta
        )
        if same:
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
        # Field-change path lands in Task 10.
        raise NotImplementedError("Task 10 — field-change path")

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
        """Single-round-trip INSERT with audit cols stamped IN the
        statement (codex F1: never a post-write UPDATE — that would let
        an idempotent retry rewrite the original creator's
        attribution).
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
                ") RETURNING series_id"
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
        sid = int(sid_raw)
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
