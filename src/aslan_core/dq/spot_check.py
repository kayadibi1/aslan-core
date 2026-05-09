"""dq.spot_check — manual spot-check workflow per spec §5.5 + §7.2.

Three public surfaces:

  * ``draw_sample(...)`` — pick N random rows from a source's primary
    table, insert ``audit.spot_check_sample`` rows. KAP supports
    ``stratum='high_priority_event_type'`` which over-samples
    material_event / dividend / share_buyback / capital_action filings
    at 2× weight (spec §7.2 step 1).

  * ``pending_samples(...)`` — list rows where ``labelled = false``
    (the labeller's queue).

  * ``label_field(...)`` — write one ``audit.spot_check_result`` row
    + flip the sample's ``labelled`` flag on first label. Computes
    ``matches`` + numeric ``variance_pct``. Per spec §7.2 step 4: for
    KAP samples, also mirror the result into ``agg.filing_event_label``.
    The mirror is best-effort — if the agg table is absent (M3 lives
    on a different branch) we log an event and continue.

  * ``mark_sample_complete(...)`` — flip ``labelled = true`` even if
    no result rows landed (the labeller decided "no truth value to
    record"). Idempotent.

Source → primary-table mapping (used by ``draw_sample``):

  kap   → kap.disclosures        PK: disclosure_id
  bist  → bist.daily_ohlcv       PK: (security_id, trade_date)
  evds  → evds.observation       PK: (series_code, observation_date)
  tefas → tefas.fund_holding     PK: (fund_id, entity_id, snapshot_date)
  mkk   → mkk.capital_action     PK: (entity_id, event_at)

If a source's primary table is absent on this branch / environment
the function returns an empty list rather than raising — the cron
entry-point logs the gap via an event so the dashboard surfaces it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.dq import event as dq_event
from aslan_core.dq._sql import (
    INSERT_SPOT_CHECK_RESULT,
    INSERT_SPOT_CHECK_SAMPLE,
    SELECT_SPOT_CHECK_SAMPLE_BY_ID,
    UPDATE_SPOT_CHECK_SAMPLE_LABELLED,
)
from aslan_core.dq.types import Severity, SpotCheckSample

_log = structlog.get_logger(__name__)


# ── Source → primary-table catalogue ─────────────────────────────


@dataclass(frozen=True, slots=True)
class _SourceTable:
    """Metadata for one source's primary spot-check table."""

    source: str
    schema: str
    table: str
    pk_columns: tuple[str, ...]


_SOURCE_TABLES: dict[str, _SourceTable] = {
    "kap": _SourceTable("kap", "kap", "disclosures", ("disclosure_id",)),
    "bist": _SourceTable("bist", "bist", "daily_ohlcv", ("security_id", "trade_date")),
    "evds": _SourceTable("evds", "evds", "observation", ("series_code", "observation_date")),
    "tefas": _SourceTable(
        "tefas", "tefas", "fund_holding", ("fund_id", "entity_id", "snapshot_date")
    ),
    "mkk": _SourceTable("mkk", "mkk", "capital_action", ("entity_id", "event_at")),
}

# KAP "high-priority" event types — over-sampled at 2× when the
# stratum kwarg is set. Spec §7.2 step 1.
_HIGH_PRIORITY_KAP_TYPES: tuple[str, ...] = (
    "material_event",
    "dividend",
    "share_buyback",
    "capital_action",
)


# ── draw_sample ──────────────────────────────────────────────────


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


async def _column_present(
    session: AsyncSession, schema: str, table_name: str, column_name: str
) -> bool:
    row = (
        await session.execute(
            text(
                "SELECT EXISTS ("
                "  SELECT 1 FROM information_schema.columns "
                "  WHERE table_schema = :schema "
                "    AND table_name = :table "
                "    AND column_name = :column"
                ") AS present"
            ),
            {"schema": schema, "table": table_name, "column": column_name},
        )
    ).one()
    return bool(row.present)


# Per-source query SQL. Each is a small literal — no f-string formatting,
# no caller-supplied identifier interpolation, so the static-SQL discipline
# carries through. Each query selects N rows uniformly at random; the
# KAP variant doubles the per-row hit probability for high-priority types.
_RANDOM_SQL: dict[str, Any] = {
    "kap": text(
        "SELECT disclosure_id FROM kap.disclosures "
        "ORDER BY random() LIMIT :n"
    ),
    "bist": text(
        "SELECT security_id, trade_date FROM bist.daily_ohlcv "
        "ORDER BY random() LIMIT :n"
    ),
    "evds": text(
        "SELECT series_code, observation_date FROM evds.observation "
        "ORDER BY random() LIMIT :n"
    ),
    "tefas": text(
        "SELECT fund_id, entity_id, snapshot_date FROM tefas.fund_holding "
        "ORDER BY random() LIMIT :n"
    ),
    "mkk": text(
        "SELECT entity_id, event_at FROM mkk.capital_action "
        "ORDER BY random() LIMIT :n"
    ),
}

# KAP stratified variant: rows where event_type IN high-priority list
# get a 2× weight via UNION ALL — the 2nd copy effectively doubles the
# random-pick probability per row.
_RANDOM_KAP_HIGH_PRIORITY_SQL = text(
    "SELECT disclosure_id FROM ("
    "  SELECT disclosure_id, random() AS r FROM kap.disclosures "
    "  UNION ALL "
    "  SELECT disclosure_id, random() AS r FROM kap.disclosures "
    "    WHERE event_type IN ('material_event','dividend','share_buyback','capital_action') "
    ") s "
    "ORDER BY r LIMIT :n"
)


async def draw_sample(
    *,
    session: AsyncSession,
    source: str,
    n: int = 10,
    stratum: str | None = None,
) -> list[UUID]:
    """Draw N random rows from ``source``'s primary table, inserting one
    ``audit.spot_check_sample`` row per pick.

    Returns the list of inserted ``sample_id`` values.

    If the primary table is absent on this DB the function returns
    ``[]`` and emits a ``spot_check_draw_skipped`` event so operators
    see the gap in /dq/spot-check rather than silently zero rows.

    `stratum`:
      * ``None`` — uniform random.
      * ``"high_priority_event_type"`` — KAP-only over-sample of
        material_event/dividend/share_buyback/capital_action at 2×.
        Falls back to uniform random when ``event_type`` is absent
        from ``kap.disclosures`` on this branch.

    Raises ``ValueError`` for unknown source or unsupported stratum.
    """
    if source not in _SOURCE_TABLES:
        raise ValueError(
            f"unknown source {source!r}; expected one of {sorted(_SOURCE_TABLES)}"
        )
    if n <= 0:
        raise ValueError(f"n must be positive; got {n}")
    if stratum is not None and stratum != "high_priority_event_type":
        raise ValueError(
            f"unsupported stratum {stratum!r}; only "
            f"'high_priority_event_type' is recognised"
        )
    if stratum == "high_priority_event_type" and source != "kap":
        raise ValueError(
            f"stratum='high_priority_event_type' is KAP-only; got source={source!r}"
        )

    src_meta = _SOURCE_TABLES[source]
    if not await _table_present(session, src_meta.schema, src_meta.table):
        await dq_event.emit(
            session=session,
            event_type="spot_check_draw_skipped",
            emitter=f"spot-check-draw:{source}",
            severity=Severity.INFO,
            payload={
                "source": source,
                "reason": f"{src_meta.schema}.{src_meta.table} not present",
                "n_requested": n,
            },
        )
        return []

    drawn_at = datetime.now(UTC)

    # Pick the right query. KAP stratified path requires the event_type
    # column to exist; otherwise we fall back to uniform.
    if stratum == "high_priority_event_type" and source == "kap":
        if await _column_present(session, "kap", "disclosures", "event_type"):
            sql = _RANDOM_KAP_HIGH_PRIORITY_SQL
            stratum_label: str | None = "high_priority_event_type"
        else:
            sql = _RANDOM_SQL["kap"]
            stratum_label = None
            await dq_event.emit(
                session=session,
                event_type="spot_check_stratum_fallback",
                emitter="spot-check-draw:kap",
                severity=Severity.INFO,
                payload={
                    "reason": "kap.disclosures has no event_type column on this branch",
                    "fallback": "uniform_random",
                },
            )
    else:
        sql = _RANDOM_SQL[source]
        stratum_label = None

    rows = (await session.execute(sql, {"n": n})).all()

    sample_ids: list[UUID] = []
    seen_pks: set[tuple[Any, ...]] = set()
    for r in rows:
        pk: dict[str, Any] = {}
        pk_tuple: list[Any] = []
        for col in src_meta.pk_columns:
            value = getattr(r, col)
            pk[col] = value
            pk_tuple.append(value)
        # The KAP high-priority UNION ALL path can return the same
        # disclosure_id twice (the high-priority subset is the second
        # arm of the UNION). De-dup before insert.
        key = tuple(pk_tuple)
        if key in seen_pks:
            continue
        seen_pks.add(key)
        result = await session.execute(
            INSERT_SPOT_CHECK_SAMPLE,
            {
                "source": source,
                "drawn_at": drawn_at,
                "record_table": f"{src_meta.schema}.{src_meta.table}",
                "record_pk": json.dumps(pk, default=str),
                "stratum": stratum_label,
            },
        )
        sample_id = result.scalar_one()
        sample_ids.append(UUID(str(sample_id)))
    return sample_ids


# ── pending_samples ──────────────────────────────────────────────


_SELECT_PENDING_SAMPLES = text(
    "SELECT sample_id, source, drawn_at, record_table, record_pk, "
    "  stratum, labelled, labelled_at, labeller, recorded_at "
    "FROM audit.spot_check_sample "
    "WHERE labelled = false "
    "ORDER BY drawn_at DESC "
    "LIMIT :limit"
)
_SELECT_PENDING_SAMPLES_BY_SOURCE = text(
    "SELECT sample_id, source, drawn_at, record_table, record_pk, "
    "  stratum, labelled, labelled_at, labeller, recorded_at "
    "FROM audit.spot_check_sample "
    "WHERE labelled = false AND source = :source "
    "ORDER BY drawn_at DESC "
    "LIMIT :limit"
)


def _row_to_sample(row: Any) -> SpotCheckSample:
    record_pk = row.record_pk
    if isinstance(record_pk, str):
        record_pk_dict: dict[str, Any] = json.loads(record_pk)
    elif isinstance(record_pk, dict):
        record_pk_dict = record_pk
    else:
        record_pk_dict = dict(record_pk) if record_pk is not None else {}
    return SpotCheckSample(
        sample_id=UUID(str(row.sample_id)),
        source=row.source,
        drawn_at=row.drawn_at,
        record_table=row.record_table,
        record_pk=record_pk_dict,
        stratum=row.stratum,
        labelled=bool(row.labelled),
        labelled_at=row.labelled_at,
        labeller=row.labeller,
        recorded_at=row.recorded_at,
    )


async def pending_samples(
    *,
    session: AsyncSession,
    source: str | None = None,
    limit: int = 50,
) -> list[SpotCheckSample]:
    """Return the pending labelling queue (newest-drawn first)."""
    if limit <= 0:
        raise ValueError(f"limit must be positive; got {limit}")
    if source is not None:
        result = await session.execute(
            _SELECT_PENDING_SAMPLES_BY_SOURCE,
            {"source": source, "limit": limit},
        )
    else:
        result = await session.execute(_SELECT_PENDING_SAMPLES, {"limit": limit})
    return [_row_to_sample(r) for r in result.all()]


async def get_sample(
    *, session: AsyncSession, sample_id: UUID
) -> SpotCheckSample | None:
    """Fetch one sample by id (for the dashboard's per-sample form)."""
    result = await session.execute(
        SELECT_SPOT_CHECK_SAMPLE_BY_ID,
        {"sample_id": sample_id},
    )
    row = result.one_or_none()
    return _row_to_sample(row) if row is not None else None


# ── label_field ──────────────────────────────────────────────────


def _try_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value.strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None


def _compute_match_and_variance(
    db_value: str | None, truth_value: str | None
) -> tuple[bool, Decimal | None]:
    """Return ``(matches, variance_pct)`` per spec §7.2.

    Both numeric → ``matches = abs(diff) ≤ 1e-9 * truth`` (or exact when
    truth=0); ``variance_pct = 100 * abs(db - truth) / |truth|`` (NULL
    when truth=0 to avoid divide-by-zero).

    Otherwise → ``matches`` is exact string equality after strip;
    ``variance_pct`` is NULL.
    """
    if db_value is None and truth_value is None:
        return True, None
    if db_value is None or truth_value is None:
        return False, None
    db_num = _try_decimal(db_value)
    truth_num = _try_decimal(truth_value)
    if db_num is not None and truth_num is not None:
        diff = abs(db_num - truth_num)
        if truth_num == 0:
            # Variance undefined; fall back to exact match.
            return diff == 0, None
        # variance_pct as percentage of |truth|.
        variance = (diff / abs(truth_num)) * Decimal(100)
        # "Matches" when relative diff is below 1e-9 — guards against
        # representational drift between the DB read and the labeller's
        # typed string. Exact equality is the conservative read.
        matches = diff == 0
        return matches, variance
    return db_value.strip() == truth_value.strip(), None


_AGG_LABEL_TABLE_PRESENT_QUERY = text(
    "SELECT EXISTS ("
    "  SELECT 1 FROM information_schema.tables "
    "  WHERE table_schema = 'agg' AND table_name = 'filing_event_label'"
    ") AS present"
)

_INSERT_AGG_LABEL_MIRROR = text(
    "INSERT INTO agg.filing_event_label("
    "  filing_id, event_type, event_seq, expected_payload, labeller, "
    "  confidence, is_holdout, notes"
    ") VALUES ("
    "  :filing_id, "
    "  CAST(:event_type AS agg.filing_event_type), "
    "  1, "
    "  CAST(:expected_payload AS JSONB), "
    "  :labeller, "
    "  NULL, "
    "  false, "
    "  :notes"
    ") ON CONFLICT DO NOTHING "
    "RETURNING label_id"
)


async def _maybe_mirror_to_agg_label(
    *,
    session: AsyncSession,
    sample: SpotCheckSample,
    field: str,
    truth_value: str | None,
    label_note: str | None,
    labeller: str,
) -> int | None:
    """Best-effort mirror of a KAP spot-check label into
    ``agg.filing_event_label``. Returns the new ``label_id`` or
    ``None`` when the mirror was skipped (table absent, sample is
    not a KAP sample, missing filing_id PK shape, INSERT raised on a
    constraint we can't satisfy from a spot-check context).

    Per spec §7.2 step 4: spot-check labels feed the labelled corpus
    — we mirror with ON CONFLICT DO NOTHING so the agg table's own
    invariants drive deduplication.
    """
    if sample.source != "kap":
        return None
    present = (await session.execute(_AGG_LABEL_TABLE_PRESENT_QUERY)).one()
    if not bool(present.present):
        await dq_event.emit(
            session=session,
            event_type="spot_check_mirror_skipped",
            emitter="spot-check-label",
            severity=Severity.INFO,
            payload={
                "sample_id": str(sample.sample_id),
                "reason": "agg.filing_event_label not present on this branch",
            },
        )
        return None
    # Map the KAP record_pk to a doc.filing_id. The KAP M3 plan keys
    # spot-check labels by filing_id; on this branch the sample's
    # record_pk only carries `disclosure_id`. We project to the
    # corresponding doc.filing row via doc.filing.source_filing_ref =
    # CAST(disclosure_id AS TEXT). If no such filing exists we skip
    # (the mirror is best-effort; the spot_check_result remains the
    # primary record).
    disclosure_id = sample.record_pk.get("disclosure_id")
    if disclosure_id is None:
        return None
    filing_row = (
        await session.execute(
            text(
                "SELECT filing_id FROM doc.filing "
                "WHERE source_id = 'kap' "
                "  AND source_filing_ref = :ref "
                "ORDER BY revision_no DESC LIMIT 1"
            ),
            {"ref": str(disclosure_id)},
        )
    ).one_or_none()
    if filing_row is None:
        return None
    expected_payload = {
        "field": field,
        "truth_value": truth_value,
        "spot_check_sample_id": str(sample.sample_id),
    }
    try:
        label_row = (
            await session.execute(
                _INSERT_AGG_LABEL_MIRROR,
                {
                    "filing_id": filing_row.filing_id,
                    # KAP spot-check labels feed back as a generic
                    # "material_event" type when we don't know the
                    # specific event type. Real M3 wiring will replace
                    # this with the canonical event_type extracted by
                    # the event-extractor.
                    "event_type": "material_event",
                    "expected_payload": json.dumps(expected_payload, default=str),
                    "labeller": labeller,
                    "notes": label_note,
                },
            )
        ).one_or_none()
    except Exception as exc:  # noqa: BLE001 — best-effort mirror, log and continue
        _log.warning(
            "spot_check.mirror_failed",
            sample_id=str(sample.sample_id),
            error=str(exc),
        )
        await dq_event.emit(
            session=session,
            event_type="spot_check_mirror_failed",
            emitter="spot-check-label",
            severity=Severity.WARN,
            payload={
                "sample_id": str(sample.sample_id),
                "error": str(exc),
            },
        )
        return None
    return int(label_row.label_id) if label_row is not None else None


async def label_field(
    *,
    session: AsyncSession,
    sample_id: UUID,
    field: str,
    db_value: str | None,
    truth_value: str | None,
    labeller: str,
    label_note: str | None = None,
) -> int:
    """Persist one labelled field result. Returns the new ``result_id``.

    Computes ``matches`` (numeric → exact-equality on Decimal; otherwise
    exact-string after strip) and ``variance_pct`` (percentage relative
    to ``|truth|`` for numeric; NULL otherwise).

    On first label for a sample, flips ``labelled = true`` + stamps
    ``labelled_at`` and ``labeller`` on ``audit.spot_check_sample``.
    Subsequent labels for the same sample leave the flag alone (the
    UPDATE has a ``WHERE labelled = false`` guard so it's idempotent).

    Per spec §7.2 step 4: for KAP samples, also mirror into
    ``agg.filing_event_label`` (best-effort; logs an event if the
    agg table isn't present).
    """
    if not field:
        raise ValueError("field must be a non-empty string")
    if not labeller:
        raise ValueError("labeller must be a non-empty string")

    sample = await get_sample(session=session, sample_id=sample_id)
    if sample is None:
        raise LookupError(
            f"audit.spot_check_sample {sample_id} not found — "
            f"draw_sample() must run before label_field()"
        )

    matches, variance_pct = _compute_match_and_variance(db_value, truth_value)

    label_event_id = await _maybe_mirror_to_agg_label(
        session=session,
        sample=sample,
        field=field,
        truth_value=truth_value,
        label_note=label_note,
        labeller=labeller,
    )

    insert_row = (
        await session.execute(
            INSERT_SPOT_CHECK_RESULT,
            {
                "sample_id": sample_id,
                "field": field,
                "db_value": db_value,
                "truth_value": truth_value,
                "variance_pct": variance_pct,
                "matches": matches,
                "label_note": label_note,
                "labeller": labeller,
                "label_event_id": label_event_id,
            },
        )
    ).one()
    result_id = int(insert_row.result_id)

    # Flip the sample's labelled flag — idempotent via the WHERE clause.
    await session.execute(
        UPDATE_SPOT_CHECK_SAMPLE_LABELLED,
        {
            "sample_id": sample_id,
            "labelled_at": datetime.now(UTC),
            "labeller": labeller,
        },
    )
    return result_id


# ── mark_sample_complete ─────────────────────────────────────────


async def mark_sample_complete(
    *, session: AsyncSession, sample_id: UUID, labeller: str = "system"
) -> None:
    """Flip ``labelled = true`` on a sample even if no result rows landed.

    Use case (spec §7.2): the labeller decides "no truth value to
    record" for a sample (e.g. the source row is a known-bad seed).
    Idempotent — a second call is a no-op via the
    ``WHERE labelled = false`` guard.
    """
    await session.execute(
        UPDATE_SPOT_CHECK_SAMPLE_LABELLED,
        {
            "sample_id": sample_id,
            "labelled_at": datetime.now(UTC),
            "labeller": labeller,
        },
    )


__all__ = [
    "draw_sample",
    "get_sample",
    "label_field",
    "mark_sample_complete",
    "pending_samples",
]
