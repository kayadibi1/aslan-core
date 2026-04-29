"""``ObservationReader`` — read-only surface over ``ts.series_catalog``,
``ts.series_subject``, and ``ts.observation`` (v0.4.0 Tasks 20-22).

Public methods land per task:

* :meth:`ObservationReader.get_series` (Task 20) — catalog row + linked
  subjects hydrated into a :class:`Series` Pydantic model. Returns
  ``None`` when the row does not exist (callers branch on ``None`` for
  "look first, then write" patterns; :class:`SeriesNotFound` is
  reserved for callers that explicitly assert presence).
* :meth:`ObservationReader.latest` (Task 21) — latest observation by
  ``ts`` for a series, with point-in-time (PIT) collapse over ``as_of``.
* :meth:`ObservationReader.range` (Task 22) — half-open
  ``[ts_start, ts_end)`` range with the same PIT-collapse pattern.

Point-in-time semantics (codex F1, 2026-04-29). Both :meth:`latest`
and :meth:`range` use ``DISTINCT ON (series_id, ts)`` so each
``(series_id, ts)`` collapses to exactly one row: the row with the
maximum ``as_of <= pit``. Without this, a restated observation appears
as multiple rows in the result.

Reader is read-only — no audit events, no actor stamping, no
strict-mode checks. Strict-mode applies only to mutations.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.observability.tracing import traced
from aslan_core.schemas.timeseries import Observation, Series, SubjectRef


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

    @traced("ObservationReader.latest")
    async def latest(
        self,
        series_id: int,
        *,
        as_of: datetime | None = None,
    ) -> Observation | None:
        """Latest observation by ``ts`` for ``series_id`` with PIT
        collapse over ``as_of``.

        For each ``(series_id, ts)``, ``DISTINCT ON`` keeps only the
        row with the maximum ``as_of <= pit`` (or the maximum ``as_of``
        full-stop when ``pit`` is ``None``). The outer ``LIMIT 1`` then
        picks the most-recent ``ts``.

        ``ORDER BY series_id ASC, ts DESC, as_of DESC`` is critical
        (codex F17, 2026-04-29): an OLDER ``ts`` whose restatement
        carries the globally-largest ``as_of`` must NOT win. ``ts``
        priority comes BEFORE ``as_of`` priority.

        ``as_of=None`` semantics: returns the latest known fact
        regardless of recording time — equivalent to ``pit=+inf``. The
        WHERE clause becomes ``:pit IS NULL OR as_of <= :pit`` which is
        always true; DISTINCT ON still collapses on ``(series_id, ts)``
        keeping the highest ``as_of`` per ``ts``.
        """
        row = (
            await self._s.execute(
                text(
                    "SELECT * FROM ("
                    "  SELECT DISTINCT ON (series_id, ts) "
                    "    series_id, ts, as_of, value, value_text, "
                    "    quality_flag, ingestion_run_id, metadata "
                    "  FROM ts.observation "
                    "  WHERE series_id = :sid "
                    "    AND (CAST(:pit AS TIMESTAMPTZ) IS NULL "
                    "         OR as_of <= CAST(:pit AS TIMESTAMPTZ)) "
                    "  ORDER BY series_id, ts DESC, as_of DESC"
                    ") sub "
                    "ORDER BY ts DESC LIMIT 1"
                ),
                {"sid": series_id, "pit": as_of},
            )
        ).first()
        if row is None:
            return None
        return Observation(
            series_id=row.series_id,
            ts=row.ts,
            as_of=row.as_of,
            value=row.value,
            value_text=row.value_text,
            quality_flag=row.quality_flag,
            ingestion_run_id=row.ingestion_run_id,
            metadata=row.metadata if row.metadata is not None else {},
        )

    @traced("ObservationReader.range")
    async def range(
        self,
        series_id: int,
        ts_start: datetime,
        ts_end: datetime,
        *,
        as_of: datetime | None = None,
        limit: int | None = None,
    ) -> list[Observation]:
        """Half-open ``[ts_start, ts_end)`` range over ``series_id``
        with PIT collapse on ``as_of``.

        Returns at most one row per ``ts`` (DISTINCT ON collapses
        restated rows). ``ORDER BY series_id ASC, ts ASC, as_of DESC``
        means the row with the highest ``as_of <= pit`` wins per ``ts``;
        the outer SELECT then orders ascending by ``ts`` so the caller
        can iterate in time order.

        ``ts_start`` is inclusive, ``ts_end`` is exclusive — matches
        the half-open range convention used elsewhere in aslan-core.
        ``limit`` caps the post-collapse row count when supplied;
        ``None`` returns every row in the window.

        ``as_of`` filters BEFORE the DISTINCT ON: a row whose own
        winning ``as_of`` exceeds the PIT is excluded entirely (not
        merely deprioritised) so a future restatement does not "appear"
        in a historical PIT replay.
        """
        # Limit composition: built into the SQL only when provided so a
        # caller passing ``None`` does not bind an unused placeholder.
        # The composition uses an internal-only Python flag — no caller
        # input ever reaches the SQL string.
        sql = (
            "SELECT * FROM ("
            "  SELECT DISTINCT ON (series_id, ts) "
            "    series_id, ts, as_of, value, value_text, "
            "    quality_flag, ingestion_run_id, metadata "
            "  FROM ts.observation "
            "  WHERE series_id = :sid "
            "    AND ts >= :ts_start AND ts < :ts_end "
            "    AND (CAST(:pit AS TIMESTAMPTZ) IS NULL "
            "         OR as_of <= CAST(:pit AS TIMESTAMPTZ)) "
            "  ORDER BY series_id, ts ASC, as_of DESC"
            ") sub "
            "ORDER BY ts ASC"
        )
        params: dict[str, object] = {
            "sid": series_id,
            "ts_start": ts_start,
            "ts_end": ts_end,
            "pit": as_of,
        }
        if limit is not None:
            sql += " LIMIT :lim"
            params["lim"] = limit
        rows = (await self._s.execute(text(sql), params)).all()
        return [
            Observation(
                series_id=r.series_id,
                ts=r.ts,
                as_of=r.as_of,
                value=r.value,
                value_text=r.value_text,
                quality_flag=r.quality_flag,
                ingestion_run_id=r.ingestion_run_id,
                metadata=r.metadata if r.metadata is not None else {},
            )
            for r in rows
        ]
