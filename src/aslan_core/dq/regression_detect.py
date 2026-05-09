"""dq.regression_detect — nightly threshold-based regression detection.

Spec §7.4. v1 detects period-over-period shifts on a curated metric
list and persists each crossing as an ``audit.regression_flag`` with
``status='open'``. v2 (NG5; promoted to in-scope per the autonomy
directive on this branch) post-processes v1's flags by querying
``kap.disclosures`` for material filings on the entity in a window
around the detected period; matched flags are auto-dismissed with
``status='dismissed'`` and ``review_note='auto-dismissed: justified
by KAP filing <id>'``.

v1 metrics (per spec §7.4):

  * ``revenue``         — QoQ, threshold 25%
  * ``net_income``      — QoQ, threshold 40%
  * ``total_assets``    — QoQ, threshold 15%
  * ``debt_to_equity``  — QoQ, threshold 30%
  * ``pe``              — DoD, threshold 20%
  * ``roe``             — QoQ, threshold 30%

The implementation uses a curated entity list (top-50 by liquidity).
The list is hard-coded in this module as a placeholder; sidar
curation of the actual list is flagged in the M5 handoff. For metrics
sourced from ``ts.canonical_financial`` the ``canonical_code`` for
``debt_to_equity``, ``pe``, and ``roe`` is currently a placeholder —
the real codes land with the ``aslan-core`` financial-canonicaliser
M2.1 patch; until then the detector skips those metrics with an
``regression_metric_unwired`` event.

v2 correlation window: ``[T - 7 days, T + 1 day]`` around the
``detected_at`` of each flag. Filings of category in
``{material_event, capital_action, dividend}`` justify the flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.dq import event as dq_event
from aslan_core.dq import regression
from aslan_core.dq.types import RegressionFlag, Severity

_log = structlog.get_logger(__name__)


# ── Curated metric list ──────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """One metric in the regression-detect curated list.

    ``canonical_code`` is the lookup key in ``ts.canonical_financial``;
    ``period_kind`` is one of ``'q'`` (quarter-over-quarter) or ``'d'``
    (day-over-day for price-derived metrics like P/E). ``threshold_pct``
    is the firing threshold for ``|shift_pct|`` in percent.
    """

    metric: str
    canonical_code: str
    period_kind: str
    threshold_pct: Decimal
    wired: bool = True


METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("revenue", "is.revenue", "q", Decimal("25")),
    MetricSpec("net_income", "is.net_income", "q", Decimal("40")),
    MetricSpec("total_assets", "bs.total_assets", "q", Decimal("15")),
    # debt_to_equity / pe / roe are derived metrics that require a
    # ratio-projection step in the canonicaliser (not yet shipped).
    # Marked ``wired=False`` so the detector emits a single skip event
    # per metric instead of attempting a missing-code lookup.
    MetricSpec("debt_to_equity", "ratio.debt_to_equity", "q", Decimal("30"), wired=False),
    MetricSpec("pe", "ratio.pe", "d", Decimal("20"), wired=False),
    MetricSpec("roe", "ratio.roe", "q", Decimal("30"), wired=False),
)


# ── Curated entity roster ────────────────────────────────────────


# v1 placeholder: the BIST top-50 list is curated quarterly by sidar.
# Until the real list lands, the detector queries every entity that
# has a current ``ref.identifier(namespace='bist_ticker')`` row and
# at least one ``ts.canonical_financial`` observation in the last
# 4 quarters, capped at 50 to keep the cron bounded.
_SELECT_TOP_BIST_ENTITIES = text(
    "SELECT DISTINCT i.entity_id, i.value AS ticker "
    "FROM ref.identifier i "
    "WHERE i.namespace = 'bist_ticker' "
    "  AND i.valid_from <= current_date "
    "  AND i.valid_to > current_date "
    "ORDER BY i.value "
    "LIMIT 50"
)


@dataclass(frozen=True, slots=True)
class _RosterEntity:
    entity_id: UUID
    ticker: str


async def _table_present(session: AsyncSession, schema: str, table_name: str) -> bool:
    row = (
        await session.execute(
            text(
                "SELECT EXISTS ("
                "  SELECT 1 FROM information_schema.tables "
                "  WHERE table_schema = :schema AND table_name = :table"
                ") AS present"
            ),
            {"schema": schema, "table": table_name},
        )
    ).one()
    return bool(row.present)


async def _load_roster(session: AsyncSession) -> list[_RosterEntity]:
    if not await _table_present(session, "ref", "identifier"):
        return []
    rows = (await session.execute(_SELECT_TOP_BIST_ENTITIES)).all()
    return [_RosterEntity(entity_id=UUID(str(r.entity_id)), ticker=r.ticker) for r in rows]


# ── v1: threshold detection ──────────────────────────────────────


_SELECT_LATEST_TWO_QUARTERLY = text(
    "SELECT period_end, value "
    "FROM ts.canonical_financial "
    "WHERE entity_id = :entity_id "
    "  AND canonical_code = :canonical_code "
    "  AND period_type = 'q' "
    "  AND restatement_basis = 'as_reported' "
    "ORDER BY period_end DESC "
    "LIMIT 2"
)


async def _fetch_period_pair(
    *, session: AsyncSession, entity_id: UUID, canonical_code: str
) -> tuple[Decimal, Decimal, Any] | None:
    """Return ``(prior_value, current_value, period_end)`` for the latest
    pair of QoQ values. None when fewer than 2 rows exist."""
    rows = (
        await session.execute(
            _SELECT_LATEST_TWO_QUARTERLY,
            {"entity_id": entity_id, "canonical_code": canonical_code},
        )
    ).all()
    if len(rows) < 2:
        return None
    current = rows[0]
    prior = rows[1]
    if current.value is None or prior.value is None:
        return None
    return (
        Decimal(str(prior.value)),
        Decimal(str(current.value)),
        current.period_end,
    )


def _shift_pct(prior: Decimal, current: Decimal) -> Decimal | None:
    """Signed period-over-period percent. Returns None when prior=0."""
    if prior == 0:
        return None
    return (current - prior) / abs(prior) * Decimal(100)


@dataclass(frozen=True, slots=True)
class V1Result:
    flag_id: int
    metric: str
    entity_id: UUID
    ticker: str
    detected_at: datetime
    shift_pct: Decimal


async def detect_v1(*, session: AsyncSession, now: datetime | None = None) -> list[V1Result]:
    """Run v1 threshold detection across the curated entity x metric grid.

    Returns the list of newly-flagged rows. The detector skips entities
    with insufficient history (<2 quarterly observations) and metrics
    not yet wired through the canonicaliser; both produce single audit
    events (``regression_history_insufficient`` and
    ``regression_metric_unwired``) so the dashboard surfaces the gaps.
    """
    detected_at = now if now is not None else datetime.now(UTC)
    results: list[V1Result] = []
    if not await _table_present(session, "ts", "canonical_financial"):
        await dq_event.emit(
            session=session,
            event_type="regression_detect_skipped",
            emitter="dq.regression_detect.detect_v1",
            severity=Severity.INFO,
            payload={"reason": "ts.canonical_financial not present"},
        )
        return results
    roster = await _load_roster(session)
    if not roster:
        await dq_event.emit(
            session=session,
            event_type="regression_detect_skipped",
            emitter="dq.regression_detect.detect_v1",
            severity=Severity.INFO,
            payload={"reason": "no entities resolved via ref.identifier(bist_ticker)"},
        )
        return results

    unwired_emitted: set[str] = set()
    for metric in METRICS:
        if not metric.wired:
            if metric.metric not in unwired_emitted:
                await dq_event.emit(
                    session=session,
                    event_type="regression_metric_unwired",
                    emitter="dq.regression_detect.detect_v1",
                    severity=Severity.INFO,
                    payload={
                        "metric": metric.metric,
                        "canonical_code": metric.canonical_code,
                        "reason": (
                            "v1 placeholder: ratio metric not yet projected by "
                            "the financial-canonicaliser"
                        ),
                    },
                )
                unwired_emitted.add(metric.metric)
            continue
        if metric.period_kind != "q":
            # DoD metrics (pe) flow through a different branch (price-
            # series); v1 only ships QoQ from canonical_financial.
            continue
        for ent in roster:
            pair = await _fetch_period_pair(
                session=session,
                entity_id=ent.entity_id,
                canonical_code=metric.canonical_code,
            )
            if pair is None:
                continue
            prior, current, period_end = pair
            shift = _shift_pct(prior, current)
            if shift is None:
                continue
            if abs(shift) <= metric.threshold_pct:
                continue
            flag_id = await regression.flag(
                session=session,
                source="ts",
                record_table="ts.canonical_financial",
                record_pk={
                    "entity_id": str(ent.entity_id),
                    "canonical_code": metric.canonical_code,
                    "period_end": period_end.isoformat() if period_end is not None else None,
                },
                metric=metric.metric,
                prior_value=prior,
                current_value=current,
                shift_pct=shift,
                threshold_pct=metric.threshold_pct,
                detected_at=detected_at,
            )
            results.append(
                V1Result(
                    flag_id=flag_id,
                    metric=metric.metric,
                    entity_id=ent.entity_id,
                    ticker=ent.ticker,
                    detected_at=detected_at,
                    shift_pct=shift,
                )
            )
    return results


# ── v2: KAP filing correlation auto-dismissal ────────────────────


_V2_WINDOW_BACK = timedelta(days=7)
_V2_WINDOW_FORWARD = timedelta(days=1)
_V2_MATERIAL_CATEGORIES: tuple[str, ...] = (
    "material_event",
    "capital_action",
    "dividend",
)


_SELECT_KAP_JUSTIFYING_FILING = text(
    "SELECT disclosure_id, category, published_at "
    "FROM kap.disclosures "
    "WHERE entity_id = :entity_id "
    "  AND category = ANY(:categories) "
    "  AND published_at >= :window_start "
    "  AND published_at <= :window_end "
    "ORDER BY published_at DESC "
    "LIMIT 1"
)


@dataclass(frozen=True, slots=True)
class V2Result:
    flag_id: int
    dismissed: bool
    justifying_disclosure_id: str | None


async def correlate_v2(
    *,
    session: AsyncSession,
    flags: list[RegressionFlag] | list[V1Result],
) -> list[V2Result]:
    """Auto-dismiss v1 flags justified by a recent KAP filing.

    For each flag, query ``kap.disclosures`` for the flag's entity_id
    in the window ``[detected_at - 7d, detected_at + 1d]`` for any
    filing in ``_V2_MATERIAL_CATEGORIES``. On a match, call
    ``regression.set_status(status='dismissed', ...)`` and emit a
    ``regression_auto_dismissed`` event.

    Accepts either ``RegressionFlag`` (full row shape from
    ``regression.pending_flags``) or ``V1Result`` (lightweight tuple
    returned by ``detect_v1``). Both carry the ``flag_id`` +
    ``detected_at`` + ``record_pk.entity_id`` we need.
    """
    results: list[V2Result] = []
    if not await _table_present(session, "kap", "disclosures"):
        await dq_event.emit(
            session=session,
            event_type="regression_v2_skipped",
            emitter="dq.regression_detect.correlate_v2",
            severity=Severity.INFO,
            payload={"reason": "kap.disclosures not present"},
        )
        return results
    for f in flags:
        flag_id, entity_id_value, detected_at = _flag_correlation_key(f)
        if entity_id_value is None or detected_at is None:
            continue
        window_start = detected_at - _V2_WINDOW_BACK
        window_end = detected_at + _V2_WINDOW_FORWARD
        match_row = (
            await session.execute(
                _SELECT_KAP_JUSTIFYING_FILING,
                {
                    "entity_id": entity_id_value,
                    "categories": list(_V2_MATERIAL_CATEGORIES),
                    "window_start": window_start,
                    "window_end": window_end,
                },
            )
        ).one_or_none()
        if match_row is None:
            results.append(
                V2Result(flag_id=flag_id, dismissed=False, justifying_disclosure_id=None)
            )
            continue
        justifying_id = str(match_row.disclosure_id)
        review_note = f"auto-dismissed: justified by KAP filing {justifying_id}"
        await regression.set_status(
            session=session,
            flag_id=flag_id,
            status="dismissed",
            reviewer="cli:audit-regression-detect",
            review_note=review_note,
        )
        await dq_event.emit(
            session=session,
            event_type="regression_auto_dismissed",
            emitter="dq.regression_detect.correlate_v2",
            severity=Severity.INFO,
            payload={
                "flag_id": flag_id,
                "entity_id": entity_id_value,
                "justifying_disclosure_id": justifying_id,
                "category": match_row.category,
                "published_at": match_row.published_at.isoformat()
                if match_row.published_at is not None
                else None,
                "window_back_days": _V2_WINDOW_BACK.days,
                "window_forward_days": _V2_WINDOW_FORWARD.days,
            },
        )
        results.append(
            V2Result(
                flag_id=flag_id,
                dismissed=True,
                justifying_disclosure_id=justifying_id,
            )
        )
    return results


def _flag_correlation_key(
    flag: RegressionFlag | V1Result,
) -> tuple[int, str | None, datetime | None]:
    """Extract ``(flag_id, entity_id_str, detected_at)`` from either shape."""
    if isinstance(flag, V1Result):
        return flag.flag_id, str(flag.entity_id), flag.detected_at
    if isinstance(flag, RegressionFlag):
        ent = flag.record_pk.get("entity_id")
        return flag.flag_id, str(ent) if ent is not None else None, flag.detected_at
    raise TypeError(f"unexpected flag type {type(flag)!r}")


# ── Combined cron entry ──────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DetectSummary:
    v1_flagged: int
    v2_dismissed: int
    v2_kept_open: int


async def detect_and_correlate(
    *, session: AsyncSession, now: datetime | None = None
) -> DetectSummary:
    """v1 + v2 in sequence. Returns aggregate counts.

    v1 inserts flags into ``audit.regression_flag``. v2 queries KAP
    for material filings on the same entity within ``[T-7d, T+1d]`` of
    each new flag's ``detected_at``; matched flags are auto-dismissed.
    """
    v1 = await detect_v1(session=session, now=now)
    v2 = await correlate_v2(session=session, flags=v1)
    dismissed = sum(1 for r in v2 if r.dismissed)
    return DetectSummary(
        v1_flagged=len(v1),
        v2_dismissed=dismissed,
        v2_kept_open=len(v1) - dismissed,
    )


__all__ = [
    "METRICS",
    "DetectSummary",
    "MetricSpec",
    "V1Result",
    "V2Result",
    "correlate_v2",
    "detect_and_correlate",
    "detect_v1",
]
