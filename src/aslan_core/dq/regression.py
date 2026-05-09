"""dq.regression — automated regression-flag client.

Spec §5.7 + §7.4. Three public surfaces:

  * ``flag(...)`` — INSERT one row into ``audit.regression_flag`` with
    ``status='open'``. Called by the nightly
    ``audit-regression-detect`` cron when a metric's period-over-period
    shift exceeds its threshold.

  * ``set_status(...)`` — UPDATE ``status`` / ``reviewer`` /
    ``reviewed_at`` / ``review_note`` on one flag. Status mutations
    also emit ``audit.event(event_type='regression_flag_reviewed')``
    so the audit trail records every transition (status is the only
    structurally-mutable column on the row by design — see spec §11).

  * ``pending_flags(...)`` — list rows where ``status='open'`` for
    the dashboard's review queue. Newest-detected first.

The v2 (NG5) auto-dismiss path in ``regression_detect`` calls
``set_status(...)`` with ``status='dismissed'`` and a
``review_note='auto-dismissed: justified by KAP filing <id>'``;
the same audit-event emit applies, with an extra
``regression_auto_dismissed`` event emitted by the caller for the
auto-dismiss audit trail.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.dq import event as dq_event
from aslan_core.dq._sql import (
    INSERT_REGRESSION_FLAG,
    SELECT_REGRESSION_FLAG_BY_ID,
    SELECT_REGRESSION_FLAGS_OPEN,
    UPDATE_REGRESSION_FLAG_STATUS,
)
from aslan_core.dq.types import RegressionFlag, Severity

_VALID_STATUSES: frozenset[str] = frozenset({"open", "reviewed", "dismissed", "confirmed_bug"})


def _row_to_flag(row: Any) -> RegressionFlag:
    record_pk = row.record_pk
    if isinstance(record_pk, str):
        record_pk_dict: dict[str, Any] = json.loads(record_pk)
    elif isinstance(record_pk, dict):
        record_pk_dict = record_pk
    else:
        record_pk_dict = dict(record_pk) if record_pk is not None else {}
    return RegressionFlag(
        flag_id=int(row.flag_id),
        source=row.source,
        record_table=row.record_table,
        record_pk=record_pk_dict,
        metric=row.metric,
        prior_value=row.prior_value,
        current_value=row.current_value,
        shift_pct=row.shift_pct,
        threshold_pct=row.threshold_pct,
        detected_at=row.detected_at,
        status=row.status,
        reviewer=row.reviewer,
        reviewed_at=row.reviewed_at,
        review_note=row.review_note,
        recorded_at=row.recorded_at,
    )


async def flag(
    *,
    session: AsyncSession,
    source: str,
    record_table: str,
    record_pk: dict[str, Any],
    metric: str,
    prior_value: Decimal | float | int | None,
    current_value: Decimal | float | int | None,
    shift_pct: Decimal | float | int,
    threshold_pct: Decimal | float | int,
    detected_at: datetime,
) -> int:
    """Insert one ``audit.regression_flag`` row with ``status='open'``.

    Returns the new ``flag_id``. ``shift_pct`` is signed (positive on
    growth, negative on contraction); the detector takes the absolute
    value before comparing against ``threshold_pct``. Both ``prior_value``
    and ``current_value`` are NUMERIC and may be ``None`` (e.g. when the
    prior period has no row in ``ts.canonical_financial`` — the
    detector flags this case as a "first-observed" event with
    ``shift_pct=0`` and skips threshold comparison instead).
    """
    if not source:
        raise ValueError("source must be a non-empty string")
    if not metric:
        raise ValueError("metric must be a non-empty string")
    if not record_table:
        raise ValueError("record_table must be a non-empty string")
    row = (
        await session.execute(
            INSERT_REGRESSION_FLAG,
            {
                "source": source,
                "record_table": record_table,
                "record_pk": json.dumps(record_pk, default=str),
                "metric": metric,
                "prior_value": prior_value,
                "current_value": current_value,
                "shift_pct": shift_pct,
                "threshold_pct": threshold_pct,
                "detected_at": detected_at,
            },
        )
    ).one()
    return int(row.flag_id)


async def set_status(
    *,
    session: AsyncSession,
    flag_id: int,
    status: str,
    reviewer: str,
    review_note: str | None = None,
) -> None:
    """Update one flag's status.

    Emits ``audit.event(event_type='regression_flag_reviewed')`` so the
    audit trail records every transition. Raises ``ValueError`` for an
    unknown status value, ``LookupError`` when the flag doesn't exist.
    """
    if status not in _VALID_STATUSES:
        raise ValueError(f"unknown status {status!r}; expected one of {sorted(_VALID_STATUSES)}")
    if not reviewer:
        raise ValueError("reviewer must be a non-empty string")
    existing = (
        await session.execute(SELECT_REGRESSION_FLAG_BY_ID, {"flag_id": flag_id})
    ).one_or_none()
    if existing is None:
        raise LookupError(f"audit.regression_flag {flag_id} not found")
    await session.execute(
        UPDATE_REGRESSION_FLAG_STATUS,
        {
            "flag_id": flag_id,
            "status": status,
            "reviewer": reviewer,
            "review_note": review_note,
        },
    )
    await dq_event.emit(
        session=session,
        event_type="regression_flag_reviewed",
        emitter="dq.regression.set_status",
        severity=Severity.INFO,
        payload={
            "flag_id": flag_id,
            "from_status": existing.status,
            "to_status": status,
            "reviewer": reviewer,
            "review_note": review_note,
            "metric": existing.metric,
            "source": existing.source,
        },
    )


async def pending_flags(
    *,
    session: AsyncSession,
    limit: int = 50,
) -> list[RegressionFlag]:
    """Return open ``audit.regression_flag`` rows, newest-detected first."""
    if limit <= 0:
        raise ValueError(f"limit must be positive; got {limit}")
    rows = (await session.execute(SELECT_REGRESSION_FLAGS_OPEN, {"limit": limit})).all()
    return [_row_to_flag(r) for r in rows]


async def get_flag(*, session: AsyncSession, flag_id: int) -> RegressionFlag | None:
    """Fetch one flag by id (for the dashboard's per-flag review form)."""
    row = (await session.execute(SELECT_REGRESSION_FLAG_BY_ID, {"flag_id": flag_id})).one_or_none()
    return _row_to_flag(row) if row is not None else None


# Re-export for convenient `from aslan_core.dq.regression import UUID`
# style use in callers that pass UUID-shaped record_pks.
__all__ = [
    "UUID",
    "RegressionFlag",
    "flag",
    "get_flag",
    "pending_flags",
    "set_status",
]
