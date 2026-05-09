"""dq.bloomberg — Bloomberg-vs-Aslan quarterly comparison framework.

Spec §5.6 + §9 + §17 R2. Operates a quarterly rotation:

  1. ``open_quarter(quarter='2026Q2')`` — creates one
     ``audit.bloomberg_comparison_run`` row + 60 cells (5 entities x 12
     fields), all NULL on bloomberg_value / aslan_value. Idempotent: a
     second call for the same quarter returns the existing run_id.

  2. ``record_bloomberg_value(cell_id, value, entered_by)`` — manual
     entry for one cell. Production flow: sidar reads Bloomberg
     terminal, types the value into ``/dq/bloomberg``; the POST handler
     calls this function under the ``audit_admin`` role.

  3. ``record_aslan_value(cell_id)`` — auto-sampler. Reads
     ``cell.entity_ticker`` + ``cell.field``, queries the relevant
     Aslan-side source table, persists the result into the cell. Also
     computes ``variance_pct`` + ``aslan_advantage`` per spec §9.1.
     Run nightly via ``aslan-core audit bloomberg-sample``.

  4. ``close_quarter(quarter)`` — refuses if any cell has
     ``bloomberg_value IS NULL`` (spec §17 R2: cannot close a run with
     missing manual entries). On success stamps ``closed_at`` on the
     run.

  5. ``claim_check(field)`` — returns the most-recent closed-run
     aggregate for one field across all 5 entities. Used by the
     ``bloomberg-claim-check`` CLI which prints text suitable for
     pasting into a PR description per workspace ``CLAUDE.md`` §2 (the
     Bloomberg-bar defence requirement).

The 12 per-entity fields are spec §9.1's canonical comparison set. The
five anchor entities (AKBNK, ASELS, GARAN, KCHOL, TUPRS) are large
BIST issuers chosen for ground-truth availability — they all have
quarterly disclosures, dividend histories, and capital actions in the
KAP corpus, so each cell has a meaningful Bloomberg counterpart.

Per spec §9.1 step 3 the ``aslan_advantage`` heuristic is:

  * ``ties``  — both values null OR exact-equal (string-equality after
                strip, or Decimal equality for numerics)
  * ``wins``  — Aslan has a value and Bloomberg does not, OR Aslan's
                value matches the source filing exactly while
                Bloomberg's doesn't (variance_pct > 1%)
  * ``loses`` — Bloomberg has a value and Aslan does not, OR Aslan's
                value disagrees with the source filing while
                Bloomberg's matches

For the v1 sampler the Aslan-side numeric fields (revenue, net_income)
are computed from ``ts.canonical_financial`` directly, so any disagreement
is a real Aslan-side gap; the heuristic collapses to:

  * Aslan has value, Bloomberg null → wins
  * Aslan null, Bloomberg has value → loses
  * Both null → ties (rare; usually means ground-truth quarter is
    pre-history for the entity)
  * Both populated, exact match → ties
  * Both populated, diff > 1% → loses (treat as Aslan-side error vs
    Bloomberg ground truth; manual investigation required)
  * Both populated, diff ≤ 1% → ties (within rounding tolerance)

Placeholder samplers (dividend, capital action, material_event field
count) emit a ``bloomberg_sampler_placeholder`` audit.event when used,
since the v1 ground truth is not yet wired through the
event-extractor pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.dq import event as dq_event
from aslan_core.dq._sql import (
    INSERT_BLOOMBERG_CELL,
    INSERT_BLOOMBERG_RUN,
    SELECT_BLOOMBERG_CELL_BY_ID,
    SELECT_BLOOMBERG_NULL_CELL_COUNT,
    SELECT_BLOOMBERG_RUN_BY_QUARTER,
    UPDATE_BLOOMBERG_CELL_ASLAN_VALUE,
    UPDATE_BLOOMBERG_CELL_BLOOMBERG_VALUE,
    UPDATE_BLOOMBERG_RUN_CLOSE,
)
from aslan_core.dq.types import Severity

_log = structlog.get_logger(__name__)

# ── Public catalogue ─────────────────────────────────────────────


# 5 anchor entities (BIST tickers). Spec §9.1: large issuers with
# quarterly disclosures, dividends, and capital actions in the KAP
# corpus. Stable across quarters — a future widening (10/20 entities)
# is a spec amendment, not a config knob.
ANCHOR_ENTITIES: tuple[str, ...] = (
    "AKBNK",
    "ASELS",
    "GARAN",
    "KCHOL",
    "TUPRS",
)

# 12 per-entity fields. Spec §9.1: 4 quarterly revenues + 4 quarterly
# net incomes + 1 latest dividend + 1 latest capital action + 1
# audit-derived (filing lag p95 over 30d) + 1 Aslan-only (avg
# structured_field count per material_event filing).
COMPARISON_FIELDS: tuple[str, ...] = (
    "revenue_q-1",
    "revenue_q-2",
    "revenue_q-3",
    "revenue_q-4",
    "net_income_q-1",
    "net_income_q-2",
    "net_income_q-3",
    "net_income_q-4",
    "latest_dividend_amount",
    "latest_capital_action",
    "filing_lag_p95_30d",
    "material_event_field_count",
)


CELLS_PER_RUN: int = len(ANCHOR_ENTITIES) * len(COMPARISON_FIELDS)


# ── Quarter helpers ──────────────────────────────────────────────


_QUARTER_RE = __import__("re").compile(r"^([0-9]{4})Q([1-4])$")


def parse_quarter(quarter: str) -> tuple[int, int]:
    """Parse ``"2026Q2"`` → ``(2026, 2)``. Raises ``ValueError`` on bad input."""
    m = _QUARTER_RE.match(quarter)
    if m is None:
        raise ValueError(f"bad quarter format {quarter!r}; expected YYYYQn (e.g. 2026Q2)")
    return int(m.group(1)), int(m.group(2))


def current_quarter(now: datetime | None = None) -> str:
    """Return the YYYYQn label for the calendar quarter containing ``now``."""
    n = now if now is not None else datetime.now(UTC)
    q = ((n.month - 1) // 3) + 1
    return f"{n.year}Q{q}"


def quarter_start_date(quarter: str) -> datetime:
    """First day of ``quarter`` as a UTC midnight datetime."""
    year, q = parse_quarter(quarter)
    month = (q - 1) * 3 + 1
    return datetime(year, month, 1, tzinfo=UTC)


def n_quarters_back(quarter: str, n: int) -> str:
    """Return the YYYYQn label N quarters before ``quarter`` (N>=0)."""
    if n < 0:
        raise ValueError(f"n must be non-negative; got {n}")
    year, q = parse_quarter(quarter)
    total = year * 4 + (q - 1) - n
    new_year, rem = divmod(total, 4)
    return f"{new_year}Q{rem + 1}"


# ── open_quarter ──────────────────────────────────────────────────


async def open_quarter(*, session: AsyncSession, quarter: str) -> UUID:
    """Open a Bloomberg-comparison quarter. Returns the run_id.

    Idempotent: if a run already exists for ``quarter`` the existing
    run_id is returned and no new cells are inserted.
    """
    parse_quarter(quarter)  # validate format early
    existing = (
        await session.execute(SELECT_BLOOMBERG_RUN_BY_QUARTER, {"quarter": quarter})
    ).one_or_none()
    if existing is not None:
        return UUID(str(existing.run_id))

    run_row = (await session.execute(INSERT_BLOOMBERG_RUN, {"quarter": quarter})).one()
    run_id = UUID(str(run_row.run_id))
    for entity in ANCHOR_ENTITIES:
        for field in COMPARISON_FIELDS:
            await session.execute(
                INSERT_BLOOMBERG_CELL,
                {"run_id": run_id, "entity_ticker": entity, "field": field},
            )
    await dq_event.emit(
        session=session,
        event_type="bloomberg_quarter_opened",
        emitter="dq.bloomberg.open_quarter",
        severity=Severity.INFO,
        payload={
            "run_id": str(run_id),
            "quarter": quarter,
            "cells_created": CELLS_PER_RUN,
        },
    )
    return run_id


# ── record_bloomberg_value (manual entry) ────────────────────────


async def record_bloomberg_value(
    *,
    session: AsyncSession,
    cell_id: UUID,
    bloomberg_value: str | None,
    entered_by: str,
) -> None:
    """Manual entry for one cell's bloomberg_value. ``entered_by`` is
    the labeller identity (sidar via /dq/bloomberg POST in production)."""
    if not entered_by:
        raise ValueError("entered_by must be a non-empty string")
    cell = (await session.execute(SELECT_BLOOMBERG_CELL_BY_ID, {"cell_id": cell_id})).one_or_none()
    if cell is None:
        raise LookupError(f"audit.bloomberg_comparison_cell {cell_id} not found")
    await session.execute(
        UPDATE_BLOOMBERG_CELL_BLOOMBERG_VALUE,
        {
            "cell_id": cell_id,
            "bloomberg_value": bloomberg_value,
            "entered_by": entered_by,
        },
    )


# ── record_aslan_value (auto-sampler) ────────────────────────────


@dataclass(frozen=True, slots=True)
class _AslanSample:
    """Result of one Aslan-side field sampler.

    ``value`` is the textual representation persisted to the cell
    (TEXT-typed column). ``placeholder`` indicates the sampler is a
    v1 stub that has not yet been wired to a real source — the caller
    emits a ``bloomberg_sampler_placeholder`` audit event in this case.
    """

    value: str | None
    placeholder: bool = False


def _try_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value.strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None


def _compute_advantage(
    bloomberg_value: str | None, aslan_value: str | None
) -> tuple[Decimal | None, str]:
    """Return ``(variance_pct, aslan_advantage)`` per spec §9.1 step 3.

    Both null → ``ties``. Aslan-only → ``wins``. Bloomberg-only → ``loses``.
    Both populated:
      * Numeric: variance_pct = 100 * |a-b|/|b|. ≤1% → ``ties``;
        otherwise → ``loses`` (Aslan diverges from Bloomberg ground truth).
      * Non-numeric: exact-string-after-strip → ``ties``; mismatch → ``loses``.
    """
    if bloomberg_value is None and aslan_value is None:
        return None, "ties"
    if aslan_value is not None and bloomberg_value is None:
        return None, "wins"
    if aslan_value is None and bloomberg_value is not None:
        return None, "loses"
    # Both populated.
    assert aslan_value is not None and bloomberg_value is not None
    a_num = _try_decimal(aslan_value)
    b_num = _try_decimal(bloomberg_value)
    if a_num is not None and b_num is not None:
        if b_num == 0:
            # Variance undefined; fall back to exact equality.
            return None, "ties" if a_num == 0 else "loses"
        diff = abs(a_num - b_num)
        variance = (diff / abs(b_num)) * Decimal(100)
        if variance <= Decimal(1):
            return variance, "ties"
        return variance, "loses"
    # Non-numeric.
    if aslan_value.strip() == bloomberg_value.strip():
        return None, "ties"
    return None, "loses"


# ── Per-field samplers ───────────────────────────────────────────


_REVENUE_CANONICAL_CODE = "is.revenue"
_NET_INCOME_CANONICAL_CODE = "is.net_income"


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


_SELECT_ENTITY_BY_BIST_TICKER = text(
    "SELECT entity_id FROM ref.identifier "
    "WHERE namespace = 'bist_ticker' "
    "  AND value = :ticker "
    "  AND valid_from <= current_date AND valid_to > current_date "
    "LIMIT 1"
)


async def _resolve_entity_id(session: AsyncSession, ticker: str) -> UUID | None:
    if not await _table_present(session, "ref", "identifier"):
        return None
    row = (await session.execute(_SELECT_ENTITY_BY_BIST_TICKER, {"ticker": ticker})).one_or_none()
    if row is None:
        return None
    return UUID(str(row.entity_id))


_SELECT_NTH_QUARTER_FINANCIAL = text(
    "SELECT value, period_end FROM ts.canonical_financial "
    "WHERE entity_id = :entity_id "
    "  AND canonical_code = :canonical_code "
    "  AND period_type = 'q' "
    "  AND restatement_basis = 'as_reported' "
    "ORDER BY period_end DESC "
    "OFFSET :offset_n LIMIT 1"
)


async def _sample_quarterly_financial(
    *, session: AsyncSession, ticker: str, canonical_code: str, n_back: int
) -> _AslanSample:
    """Read the N-th most-recent quarterly value of ``canonical_code``
    for the entity resolved from ``ticker``. ``n_back=0`` is the latest
    quarter, ``n_back=1`` the one before, etc.
    """
    if not await _table_present(session, "ts", "canonical_financial"):
        return _AslanSample(value=None, placeholder=True)
    entity_id = await _resolve_entity_id(session, ticker)
    if entity_id is None:
        return _AslanSample(value=None, placeholder=True)
    row = (
        await session.execute(
            _SELECT_NTH_QUARTER_FINANCIAL,
            {
                "entity_id": entity_id,
                "canonical_code": canonical_code,
                "offset_n": n_back,
            },
        )
    ).one_or_none()
    if row is None:
        return _AslanSample(value=None)
    if row.value is None:
        return _AslanSample(value=None)
    return _AslanSample(value=str(row.value))


async def _sample_filing_lag_p95_30d(*, session: AsyncSession) -> _AslanSample:
    """p95 of audit.recency_observation.lag_seconds for source='kap' in
    the trailing 30 days. None if there are no observations."""
    row = (
        await session.execute(
            text(
                "SELECT percentile_cont(0.95) WITHIN GROUP "
                "  (ORDER BY lag_seconds)::float AS p95 "
                "FROM audit.recency_observation "
                "WHERE source = 'kap' "
                "  AND observed_at >= now() - INTERVAL '30 days'"
            )
        )
    ).one()
    if row.p95 is None:
        return _AslanSample(value=None)
    return _AslanSample(value=f"{float(row.p95):.2f}")


async def _sample_material_event_field_count(*, session: AsyncSession) -> _AslanSample:
    """Aslan-only signal — average structured-field count per material_event
    filing. v1 placeholder: returns None until the event-extractor's M3
    structured-payload writer is wired."""
    if not await _table_present(session, "kap", "disclosures"):
        return _AslanSample(value=None, placeholder=True)
    # TODO(M4.1): real Aslan sampler. Real version queries
    # `agg.filing_event` joined to kap.disclosures and computes
    # avg(jsonb_object_keys_count(structured_payload)) for the
    # material_event slice. For v1 we emit a placeholder event since
    # the structured-payload writer isn't on this branch.
    return _AslanSample(value=None, placeholder=True)


async def _sample_latest_dividend_amount(*, ticker: str, session: AsyncSession) -> _AslanSample:
    """Latest dividend amount for ``ticker``. v1 placeholder; real wiring
    requires the dividend extraction in aslan-event-extractor M3."""
    _ = ticker
    _ = session
    # TODO(M4.1): real Aslan sampler. Real version joins kap.disclosures
    # to agg.filing_event(event_type='dividend') for the resolved
    # entity_id and returns the latest dividend's amount field.
    return _AslanSample(value=None, placeholder=True)


async def _sample_latest_capital_action(*, ticker: str, session: AsyncSession) -> _AslanSample:
    """Latest capital action for ``ticker``. v1 placeholder; real wiring
    requires the capital-action extraction in aslan-event-extractor M3."""
    _ = ticker
    _ = session
    # TODO(M4.1): real Aslan sampler. Real version joins kap.disclosures
    # to agg.filing_event(event_type='capital_action') for the resolved
    # entity_id and returns the latest capital action's classification +
    # date. Output format: "<action_type>@<date>".
    return _AslanSample(value=None, placeholder=True)


async def _dispatch_sampler(*, session: AsyncSession, ticker: str, field: str) -> _AslanSample:
    """Route a (ticker, field) pair to the right per-field sampler."""
    if field.startswith("revenue_q-"):
        n_back = int(field.removeprefix("revenue_q-")) - 1
        return await _sample_quarterly_financial(
            session=session,
            ticker=ticker,
            canonical_code=_REVENUE_CANONICAL_CODE,
            n_back=n_back,
        )
    if field.startswith("net_income_q-"):
        n_back = int(field.removeprefix("net_income_q-")) - 1
        return await _sample_quarterly_financial(
            session=session,
            ticker=ticker,
            canonical_code=_NET_INCOME_CANONICAL_CODE,
            n_back=n_back,
        )
    if field == "latest_dividend_amount":
        return await _sample_latest_dividend_amount(ticker=ticker, session=session)
    if field == "latest_capital_action":
        return await _sample_latest_capital_action(ticker=ticker, session=session)
    if field == "filing_lag_p95_30d":
        return await _sample_filing_lag_p95_30d(session=session)
    if field == "material_event_field_count":
        return await _sample_material_event_field_count(session=session)
    raise ValueError(f"unknown field {field!r}; expected one of {COMPARISON_FIELDS}")


async def record_aslan_value(*, session: AsyncSession, cell_id: UUID) -> None:
    """Auto-sampler entry point. Reads ``cell.entity_ticker`` + ``cell.field``,
    computes the Aslan-side value, persists it + variance + advantage.

    On placeholder samplers, emits a ``bloomberg_sampler_placeholder``
    audit.event so the dashboard surfaces the gap.
    """
    cell = (await session.execute(SELECT_BLOOMBERG_CELL_BY_ID, {"cell_id": cell_id})).one_or_none()
    if cell is None:
        raise LookupError(f"audit.bloomberg_comparison_cell {cell_id} not found")

    sample = await _dispatch_sampler(session=session, ticker=cell.entity_ticker, field=cell.field)
    if sample.placeholder:
        await dq_event.emit(
            session=session,
            event_type="bloomberg_sampler_placeholder",
            emitter="dq.bloomberg.record_aslan_value",
            severity=Severity.INFO,
            payload={
                "cell_id": str(cell_id),
                "entity_ticker": cell.entity_ticker,
                "field": cell.field,
                "reason": "v1 sampler placeholder; real wiring deferred to M4.1",
            },
        )

    variance_pct, advantage = _compute_advantage(cell.bloomberg_value, sample.value)
    await session.execute(
        UPDATE_BLOOMBERG_CELL_ASLAN_VALUE,
        {
            "cell_id": cell_id,
            "aslan_value": sample.value,
            "variance_pct": variance_pct,
            "aslan_advantage": advantage,
        },
    )


# ── close_quarter ─────────────────────────────────────────────────


class QuarterNotReadyError(RuntimeError):
    """``close_quarter`` refused — at least one cell has bloomberg_value=NULL.

    Per spec §17 R2: a Bloomberg-comparison run cannot close until
    every cell carries a manually-entered Bloomberg side. This is the
    integrity contract that keeps the public claim ("wins on N of 12")
    grounded in real comparison data.
    """

    def __init__(self, *, run_id: UUID, null_cell_count: int) -> None:
        self.run_id = run_id
        self.null_cell_count = null_cell_count
        super().__init__(
            f"close_quarter refused — run {run_id} still has "
            f"{null_cell_count} cell(s) with NULL bloomberg_value"
        )


async def close_quarter(*, session: AsyncSession, quarter: str, closed_by: str = "system") -> None:
    """Close ``quarter``. Refuses if any cell has NULL bloomberg_value.

    Raises ``LookupError`` when no run exists, ``QuarterNotReadyError``
    when at least one cell is unfilled.
    """
    run = (
        await session.execute(SELECT_BLOOMBERG_RUN_BY_QUARTER, {"quarter": quarter})
    ).one_or_none()
    if run is None:
        raise LookupError(f"no audit.bloomberg_comparison_run for quarter {quarter!r}")
    run_id = UUID(str(run.run_id))
    nulls = (
        await session.execute(SELECT_BLOOMBERG_NULL_CELL_COUNT, {"run_id": run_id})
    ).scalar_one()
    if int(nulls) > 0:
        raise QuarterNotReadyError(run_id=run_id, null_cell_count=int(nulls))
    await session.execute(
        UPDATE_BLOOMBERG_RUN_CLOSE,
        {"run_id": run_id, "closed_by": closed_by},
    )
    await dq_event.emit(
        session=session,
        event_type="bloomberg_quarter_closed",
        emitter="dq.bloomberg.close_quarter",
        severity=Severity.INFO,
        payload={"run_id": str(run_id), "quarter": quarter, "closed_by": closed_by},
    )


# ── claim_check ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ClaimCheckCell:
    entity_ticker: str
    aslan_value: str | None
    bloomberg_value: str | None
    aslan_advantage: str | None


@dataclass(frozen=True, slots=True)
class ClaimCheck:
    """Aggregate result for one field across all 5 entities of the
    most-recent CLOSED run. Used to defend a per-PR Bloomberg-bar claim.
    """

    field: str
    quarter: str | None
    wins: int
    ties: int
    loses: int
    cells: tuple[ClaimCheckCell, ...]

    def to_pr_text(self) -> str:
        """Render as a multi-line block suitable for a PR description."""
        if self.quarter is None:
            return (
                f"### Bloomberg comparison — {self.field}\n\n"
                f"No closed Bloomberg-comparison run yet — claim cannot be defended.\n"
                f"Open one via `aslan-core audit bloomberg-open-quarter` and have "
                f"sidar enter the {len(ANCHOR_ENTITIES)} Bloomberg values for "
                f"`{self.field}`.\n"
            )
        lines = [
            f"### Bloomberg comparison — {self.field} (quarter {self.quarter})",
            "",
            f"Aslan: **{self.wins} wins / {self.ties} ties / {self.loses} loses** "
            f"across {len(self.cells)} BIST anchor entities.",
            "",
            "| Entity | Aslan | Bloomberg | Verdict |",
            "| --- | --- | --- | --- |",
        ]
        for c in self.cells:
            lines.append(
                f"| {c.entity_ticker} | "
                f"{c.aslan_value or '—'} | "
                f"{c.bloomberg_value or '—'} | "
                f"{c.aslan_advantage or '—'} |"
            )
        return "\n".join(lines) + "\n"


_SELECT_LATEST_CLOSED_RUN = text(
    "SELECT run_id, quarter FROM audit.bloomberg_comparison_run "
    "WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 1"
)

_SELECT_FIELD_CELLS_FOR_RUN = text(
    "SELECT entity_ticker, aslan_value, bloomberg_value, aslan_advantage "
    "FROM audit.bloomberg_comparison_cell "
    "WHERE run_id = :run_id AND field = :field "
    "ORDER BY entity_ticker"
)


async def claim_check(*, session: AsyncSession, field: str) -> ClaimCheck:
    """Most-recent CLOSED run aggregate for one field."""
    if field not in COMPARISON_FIELDS:
        raise ValueError(f"unknown field {field!r}; expected one of {COMPARISON_FIELDS}")
    run_row = (await session.execute(_SELECT_LATEST_CLOSED_RUN)).one_or_none()
    if run_row is None:
        return ClaimCheck(field=field, quarter=None, wins=0, ties=0, loses=0, cells=())
    rows = (
        await session.execute(
            _SELECT_FIELD_CELLS_FOR_RUN,
            {"run_id": run_row.run_id, "field": field},
        )
    ).all()
    wins = ties = loses = 0
    cells: list[ClaimCheckCell] = []
    for r in rows:
        cells.append(
            ClaimCheckCell(
                entity_ticker=r.entity_ticker,
                aslan_value=r.aslan_value,
                bloomberg_value=r.bloomberg_value,
                aslan_advantage=r.aslan_advantage,
            )
        )
        if r.aslan_advantage == "wins":
            wins += 1
        elif r.aslan_advantage == "ties":
            ties += 1
        elif r.aslan_advantage == "loses":
            loses += 1
    return ClaimCheck(
        field=field,
        quarter=str(run_row.quarter),
        wins=wins,
        ties=ties,
        loses=loses,
        cells=tuple(cells),
    )


# ── Render markdown ──────────────────────────────────────────────


_SELECT_RUN_BY_ID = text(
    "SELECT run_id, quarter, opened_at, closed_at "
    "FROM audit.bloomberg_comparison_run WHERE run_id = :run_id"
)

_SELECT_CELLS_FOR_RUN = text(
    "SELECT entity_ticker, field, bloomberg_value, aslan_value, "
    "  variance_pct, aslan_advantage "
    "FROM audit.bloomberg_comparison_cell "
    "WHERE run_id = :run_id "
    "ORDER BY entity_ticker, field"
)


@dataclass(frozen=True, slots=True)
class RenderCell:
    entity_ticker: str
    field: str
    bloomberg_value: str | None
    aslan_value: str | None
    variance_pct: Decimal | None
    aslan_advantage: str | None


@dataclass(frozen=True, slots=True)
class RenderRun:
    """Snapshot of a closed run, ready for markdown rendering.

    Carries everything the ``bloomberg-render`` CLI needs to produce
    ``aslan-event-extractor/docs/comparisons/bloomberg.md`` without
    re-querying the database.
    """

    run_id: UUID
    quarter: str
    opened_at: datetime
    closed_at: datetime | None
    cells: tuple[RenderCell, ...]


async def latest_closed_run(*, session: AsyncSession) -> RenderRun | None:
    """Fetch the most-recent CLOSED run + all 60 cells for rendering."""
    run_row = (await session.execute(_SELECT_LATEST_CLOSED_RUN)).one_or_none()
    if run_row is None:
        return None
    full_run = (await session.execute(_SELECT_RUN_BY_ID, {"run_id": run_row.run_id})).one()
    cell_rows = (await session.execute(_SELECT_CELLS_FOR_RUN, {"run_id": run_row.run_id})).all()
    cells = tuple(
        RenderCell(
            entity_ticker=r.entity_ticker,
            field=r.field,
            bloomberg_value=r.bloomberg_value,
            aslan_value=r.aslan_value,
            variance_pct=Decimal(r.variance_pct) if r.variance_pct is not None else None,
            aslan_advantage=r.aslan_advantage,
        )
        for r in cell_rows
    )
    return RenderRun(
        run_id=UUID(str(full_run.run_id)),
        quarter=str(full_run.quarter),
        opened_at=full_run.opened_at,
        closed_at=full_run.closed_at,
        cells=cells,
    )


def render_markdown(run: RenderRun) -> str:
    """Render a closed run to GitHub-flavoured markdown.

    Layout:

      # Bloomberg vs Aslan — <quarter>
      <summary line: total wins/ties/loses>
      <per-field commentary subsection>
      ## <ENTITY>
      <12-row table per entity with Aslan / Bloomberg / verdict>
    """
    wins = sum(1 for c in run.cells if c.aslan_advantage == "wins")
    ties = sum(1 for c in run.cells if c.aslan_advantage == "ties")
    loses = sum(1 for c in run.cells if c.aslan_advantage == "loses")
    closed_iso = run.closed_at.isoformat() if run.closed_at is not None else "(open)"
    lines: list[str] = [
        f"# Bloomberg vs Aslan — {run.quarter}",
        "",
        (
            f"**Status:** closed {closed_iso}. "
            f"**Aslan: {wins} wins / {ties} ties / {loses} loses** across "
            f"{len(run.cells)} cells "
            f"({len(ANCHOR_ENTITIES)} entities x {len(COMPARISON_FIELDS)} fields)."
        ),
        "",
        ("_Generated by `aslan-core audit bloomberg-render`. Do not edit by hand._"),
        "",
        "## Per-field summary",
        "",
        "| Field | Wins | Ties | Loses |",
        "| --- | --- | --- | --- |",
    ]
    for field in COMPARISON_FIELDS:
        f_wins = sum(1 for c in run.cells if c.field == field and c.aslan_advantage == "wins")
        f_ties = sum(1 for c in run.cells if c.field == field and c.aslan_advantage == "ties")
        f_loses = sum(1 for c in run.cells if c.field == field and c.aslan_advantage == "loses")
        lines.append(f"| `{field}` | {f_wins} | {f_ties} | {f_loses} |")

    lines.extend(["", "## Per-entity breakdown", ""])
    cells_by_entity: dict[str, list[RenderCell]] = {e: [] for e in ANCHOR_ENTITIES}
    for c in run.cells:
        cells_by_entity.setdefault(c.entity_ticker, []).append(c)

    for entity in ANCHOR_ENTITIES:
        lines.extend(
            [
                f"### {entity}",
                "",
                "| Field | Aslan | Bloomberg | Variance % | Verdict |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        # Render in the canonical field order, not whatever the DB
        # returned — keeps adjacent runs diffable.
        per_field: dict[str, RenderCell] = {c.field: c for c in cells_by_entity.get(entity, [])}
        for field in COMPARISON_FIELDS:
            cell = per_field.get(field)
            if cell is None:
                lines.append(f"| `{field}` | — | — | — | — |")
                continue
            variance = "—" if cell.variance_pct is None else f"{cell.variance_pct:.4f}"
            lines.append(
                f"| `{field}` | "
                f"{cell.aslan_value or '—'} | "
                f"{cell.bloomberg_value or '—'} | "
                f"{variance} | "
                f"{cell.aslan_advantage or '—'} |"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


# ── Sampler-loop helpers (used by CLI) ───────────────────────────


_SELECT_OPEN_NULL_ASLAN_CELLS = text(
    "SELECT c.cell_id "
    "FROM audit.bloomberg_comparison_cell c "
    "JOIN audit.bloomberg_comparison_run r ON r.run_id = c.run_id "
    "WHERE r.closed_at IS NULL AND c.aslan_value IS NULL"
)


async def open_null_aslan_cells(*, session: AsyncSession) -> list[UUID]:
    """Return cell_ids in open runs that still have aslan_value=NULL.

    The nightly sampler iterates these and calls ``record_aslan_value``
    for each.
    """
    rows = (await session.execute(_SELECT_OPEN_NULL_ASLAN_CELLS)).all()
    return [UUID(str(r.cell_id)) for r in rows]


_SELECT_OPEN_RUNS_WITH_NULLS_OLDER_THAN = text(
    "SELECT r.run_id, r.quarter, r.opened_at, "
    "  count(*) FILTER (WHERE c.bloomberg_value IS NULL)::int AS null_cells "
    "FROM audit.bloomberg_comparison_run r "
    "JOIN audit.bloomberg_comparison_cell c ON c.run_id = r.run_id "
    "WHERE r.closed_at IS NULL "
    "  AND r.opened_at < now() - INTERVAL '7 days' "
    "GROUP BY r.run_id, r.quarter, r.opened_at "
    "HAVING count(*) FILTER (WHERE c.bloomberg_value IS NULL) > 0 "
    "ORDER BY r.opened_at"
)


@dataclass(frozen=True, slots=True)
class StaleRun:
    run_id: UUID
    quarter: str
    opened_at: datetime
    null_cells: int


async def stale_open_runs(*, session: AsyncSession) -> list[StaleRun]:
    """Open runs older than 1 week with at least one NULL bloomberg cell.

    Used by ``bloomberg-reminder`` to ping sidar via Slack when the
    quarterly entry has fallen behind.
    """
    rows = (await session.execute(_SELECT_OPEN_RUNS_WITH_NULLS_OLDER_THAN)).all()
    return [
        StaleRun(
            run_id=UUID(str(r.run_id)),
            quarter=str(r.quarter),
            opened_at=r.opened_at,
            null_cells=int(r.null_cells),
        )
        for r in rows
    ]


__all__ = [
    "ANCHOR_ENTITIES",
    "CELLS_PER_RUN",
    "COMPARISON_FIELDS",
    "ClaimCheck",
    "ClaimCheckCell",
    "QuarterNotReadyError",
    "RenderCell",
    "RenderRun",
    "StaleRun",
    "claim_check",
    "close_quarter",
    "current_quarter",
    "latest_closed_run",
    "n_quarters_back",
    "open_null_aslan_cells",
    "open_quarter",
    "parse_quarter",
    "quarter_start_date",
    "record_aslan_value",
    "record_bloomberg_value",
    "render_markdown",
    "stale_open_runs",
]
