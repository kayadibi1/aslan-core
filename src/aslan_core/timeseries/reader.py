"""``ObservationReader`` — read-only surface over ``ts.series_catalog``,
``ts.series_subject``, and ``ts.observation`` (v0.4.0 Tasks 20-22).

Public methods land per task:

* :meth:`ObservationReader.get_series` (Task 20) — catalog row + linked
  subjects hydrated into a :class:`Series` Pydantic model. Returns
  ``None`` when the row does not exist (callers branch on ``None`` for
  "look first, then write" patterns; :class:`SeriesNotFound` is
  reserved for callers that explicitly assert presence).
* ``latest`` (Task 21) and ``range`` (Task 22) land in subsequent
  commits with ``DISTINCT ON (series_id, ts)`` PIT collapse.

Reader is read-only — no audit events, no actor stamping, no
strict-mode checks. Strict-mode applies only to mutations.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.observability.tracing import traced
from aslan_core.schemas.timeseries import Series, SubjectRef


class ObservationReader:
    """Read-only API over the timeseries surface. Single-session
    scope mirroring :class:`ObservationWriter`'s session contract: the
    caller owns the transaction lifecycle.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    @traced("ObservationReader.get_series")
    async def get_series(self, series_code: str) -> Series | None:
        """Return the catalog row (with linked subjects) for
        ``series_code``, or ``None`` if no such code exists.

        Subjects are returned in ``(subject_id, role)`` ASC order so
        the result is deterministic regardless of insertion order. The
        catalog row is fetched in one round-trip and the subject rows
        in a second round-trip; the two SELECTs do NOT need a single
        snapshot because subjects are only ever appended (no deletes
        in v0.4 — Art. 17 lives in aslan-service) and the caller's
        transaction isolation already pins the snapshot.
        """
        row = (
            await self._s.execute(
                text(
                    "SELECT series_id, series_code, source_id, entity_id, "
                    "       metric, frequency, unit, currency_code, "
                    "       restatement_basis, accounting_standard, "
                    "       consolidation, period_type, description, "
                    "       pii_class, metadata, created_at, updated_at "
                    "FROM ts.series_catalog WHERE series_code = :code"
                ),
                {"code": series_code},
            )
        ).first()
        if row is None:
            return None
        subj_rows = (
            await self._s.execute(
                text(
                    "SELECT subject_id, role FROM ts.series_subject "
                    "WHERE series_id = :sid ORDER BY subject_id, role"
                ),
                {"sid": row.series_id},
            )
        ).all()
        return Series(
            series_id=row.series_id,
            series_code=row.series_code,
            source_id=row.source_id,
            entity_id=row.entity_id,
            metric=row.metric,
            frequency=row.frequency,
            unit=row.unit,
            currency_code=row.currency_code,
            restatement_basis=row.restatement_basis,
            accounting_standard=row.accounting_standard,
            consolidation=row.consolidation,
            period_type=row.period_type,
            description=row.description,
            pii_class=row.pii_class,
            metadata=row.metadata if row.metadata is not None else {},
            subjects=tuple(SubjectRef(subject_id=r.subject_id, role=r.role) for r in subj_rows),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
