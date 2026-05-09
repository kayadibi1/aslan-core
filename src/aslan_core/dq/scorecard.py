"""dq.scorecard — weekly DQ scorecard computation + email/HTML rendering.

Spec §5.10 + §13 (cron) + §16 (done definition). The Sunday 23:55 UTC
``audit-scorecard`` cron computes 10 per-week metrics aggregating from
``audit.*`` tables for the trailing 7 days, persists each as one row in
``audit.scorecard_snapshot``, then emits
``audit.event(event_type='scorecard_generated')``. The dispatcher's
``weekly_scorecard`` severity rule (seeded in 0059) picks up the event
and emails the rendered HTML body to the recipient list.

Three public surfaces:

  * ``compute(*, session, week_start)`` — for a given ISO-week-Monday
    ``week_start`` (UTC), computes the 10 metrics over the trailing
    7 days. Returns ``list[ScorecardRow]``.

  * ``write(*, session, week_start, rows)`` — UPSERT rows into
    ``audit.scorecard_snapshot`` via ``INSERT ... ON CONFLICT
    (week_start, metric_name) DO UPDATE``. Idempotent re-run
    overwrites ``actual``/``status``/``notes``.

  * ``render_email(*, week_start, rows)`` /
    ``render_html(*, week_start, rows)`` — produce the email body
    (subject + HTML body) and the standalone HTML export.

Idempotency: ``audit.scorecard_snapshot`` PK is
``(week_start, metric_name)``. ``write()`` uses
``ON CONFLICT DO UPDATE`` so re-running for the same week is a
no-op-equivalent (overwrites with current values; ``recorded_at``
stays at the original insert time).

Status thresholds are encoded inline in ``compute()`` rather than
parameterised into ``audit.scorecard_thresholds``; the per-spec
thresholds change rarely and a row-level threshold table would be
strictly more complex. Promote to a seed table only if/when the spec
introduces dynamic threshold tuning.

Tables an aggregator queries that may not be present on a given branch
(e.g. ``audit.bloomberg_comparison_cell`` only lands in M4 on this
codebase) are detected via ``information_schema.tables`` and the
metric is skipped with ``status='warn'`` and a notes string explaining
the gap. The cron continues; the email body surfaces the skipped
metrics so operators see when a metric is structurally unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html import escape as html_escape
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.dq._sql import (
    SELECT_SCORECARD_SNAPSHOT_BY_WEEK,
    UPSERT_SCORECARD_SNAPSHOT,
)

ScorecardStatus = Literal["pass", "warn", "fail"]


@dataclass(frozen=True, slots=True)
class ScorecardRow:
    """One metric row in a weekly scorecard.

    ``metric_name`` is the canonical identifier persisted to
    ``audit.scorecard_snapshot.metric_name``. ``target`` and ``actual``
    are TEXT-typed in the table so they can carry units (``"<= 300s"``,
    ``">= 95%"``) without forcing a parser; the dashboard renders both
    verbatim. ``status`` is the threshold verdict; ``notes`` is an
    optional one-line annotation (e.g. "skipped: bloomberg cells absent").
    """

    metric_name: str
    target: str
    actual: str
    status: ScorecardStatus
    notes: str | None = None


# ── ISO-week helpers ─────────────────────────────────────────────


def latest_week_start(now: datetime | None = None) -> date:
    """Return the ISO-week-Monday (UTC) of the most-recent FULLY-completed week.

    "Fully completed" means the Sunday of that week is in the past.
    The cron runs Sunday 23:55 UTC; ``latest_week_start()`` at that
    time returns the Monday seven days prior (i.e. the start of the
    week the cron is summarising).

    Per spec §13: weekly cron schedule ``55 23 * * 0`` (Sunday 23:55).
    """
    n = now if now is not None else datetime.now(UTC)
    today_utc = n.date()
    # ISO weekday: Mon=1 ... Sun=7. A Sunday call returns the Monday
    # six days earlier; any other day returns the Monday of the
    # PREVIOUS week (since "fully-completed" requires the Sunday is
    # past).
    iso_dow = today_utc.isoweekday()
    if iso_dow == 7:  # Sunday — current week ends today
        # Sunday 23:55 cron firing: the week that just ended is Mon-Sun
        # ending today; week_start is six days back.
        return today_utc - timedelta(days=6)
    # Mon..Sat: most-recent fully-completed week ends on the previous
    # Sunday; its Monday is today - iso_dow days back.
    return today_utc - timedelta(days=iso_dow + 6)


def _week_window(week_start: date) -> tuple[datetime, datetime]:
    """Return ``(window_start_dt, window_end_dt)`` UTC for ``week_start``.

    ``window_start`` is the Monday at 00:00 UTC. ``window_end`` is the
    Monday after at 00:00 UTC (i.e. the start of the *next* week — half
    open ``[start, end)`` interval covers exactly 7 days).
    """
    start_dt = datetime(week_start.year, week_start.month, week_start.day, tzinfo=UTC)
    end_dt = start_dt + timedelta(days=7)
    return start_dt, end_dt


# ── Threshold helpers ────────────────────────────────────────────


def _classify_lte(actual: float, pass_le: float, warn_le: float) -> ScorecardStatus:
    """Lower-is-better metric. ``pass`` if ``actual <= pass_le``; ``warn``
    up to ``warn_le``; ``fail`` above."""
    if actual <= pass_le:
        return "pass"
    if actual <= warn_le:
        return "warn"
    return "fail"


def _classify_gte(actual: float, pass_ge: float, warn_ge: float) -> ScorecardStatus:
    """Higher-is-better metric. ``pass`` if ``actual >= pass_ge``;
    ``warn`` down to ``warn_ge``; ``fail`` below."""
    if actual >= pass_ge:
        return "pass"
    if actual >= warn_ge:
        return "warn"
    return "fail"


# ── Table-presence guard ─────────────────────────────────────────


async def _table_present(session: AsyncSession, schema: str, name: str) -> bool:
    row = (
        await session.execute(
            text(
                "SELECT EXISTS ("
                "  SELECT 1 FROM information_schema.tables "
                "  WHERE table_schema = :schema AND table_name = :name"
                ") AS present"
            ),
            {"schema": schema, "name": name},
        )
    ).one()
    return bool(row.present)


# ── Per-metric computers ─────────────────────────────────────────


_SELECT_KAP_RECENCY_PCT = text(
    "SELECT "
    "  percentile_cont(0.95) WITHIN GROUP (ORDER BY lag_seconds)::float AS p95, "
    "  percentile_cont(0.99) WITHIN GROUP (ORDER BY lag_seconds)::float AS p99, "
    "  count(*)::int AS n "
    "FROM audit.recency_observation "
    "WHERE source = 'kap' "
    "  AND observed_at >= :start_at AND observed_at < :end_at"
)


async def _metric_kap_recency(
    *, session: AsyncSession, start_at: datetime, end_at: datetime
) -> tuple[ScorecardRow, ScorecardRow]:
    """``kap_recency_p95`` + ``kap_recency_p99`` from audit.recency_observation."""
    if not await _table_present(session, "audit", "recency_observation"):
        return (
            ScorecardRow(
                metric_name="kap_recency_p95",
                target="<= 300s",
                actual="—",
                status="warn",
                notes="audit.recency_observation not present",
            ),
            ScorecardRow(
                metric_name="kap_recency_p99",
                target="<= 1800s",
                actual="—",
                status="warn",
                notes="audit.recency_observation not present",
            ),
        )
    row = (
        await session.execute(
            _SELECT_KAP_RECENCY_PCT,
            {"start_at": start_at, "end_at": end_at},
        )
    ).one()
    if int(row.n) == 0 or row.p95 is None or row.p99 is None:
        return (
            ScorecardRow(
                metric_name="kap_recency_p95",
                target="<= 300s",
                actual="—",
                status="warn",
                notes="no recency observations in window",
            ),
            ScorecardRow(
                metric_name="kap_recency_p99",
                target="<= 1800s",
                actual="—",
                status="warn",
                notes="no recency observations in window",
            ),
        )
    p95 = float(row.p95)
    p99 = float(row.p99)
    return (
        ScorecardRow(
            metric_name="kap_recency_p95",
            target="<= 300s",
            actual=f"{p95:.0f}s",
            status=_classify_lte(p95, pass_le=300.0, warn_le=600.0),
            notes=f"n={int(row.n)}",
        ),
        ScorecardRow(
            metric_name="kap_recency_p99",
            target="<= 1800s",
            actual=f"{p99:.0f}s",
            status=_classify_lte(p99, pass_le=1800.0, warn_le=3000.0),
            notes=f"n={int(row.n)}",
        ),
    )


_SELECT_EVDS_FRESHNESS = text(
    "SELECT "
    "  count(*)::int AS expected, "
    "  count(o.series_code)::int AS observed "
    "FROM audit.evds_release_calendar c "
    "LEFT JOIN ts.observation o "
    "  ON o.series_code = c.series_code "
    "  AND o.observed_at >= c.expected_at "
    "  AND o.observed_at < c.expected_at + (c.grace_seconds || ' seconds')::interval "
    "WHERE c.expected_at >= :start_at AND c.expected_at < :end_at"
)


async def _metric_evds_freshness(
    *, session: AsyncSession, start_at: datetime, end_at: datetime
) -> ScorecardRow:
    """``evds_freshness_pct``: ratio of expected EVDS releases that landed
    in our DB within the grace window."""
    if not await _table_present(session, "audit", "evds_release_calendar"):
        return ScorecardRow(
            metric_name="evds_freshness_pct",
            target=">= 95%",
            actual="—",
            status="warn",
            notes="audit.evds_release_calendar not present",
        )
    if not await _table_present(session, "ts", "observation"):
        # No observation table on this branch — surface as a warn so
        # the email digest carries the gap.
        row = (
            await session.execute(
                text(
                    "SELECT count(*)::int AS expected "
                    "FROM audit.evds_release_calendar "
                    "WHERE expected_at >= :start_at AND expected_at < :end_at"
                ),
                {"start_at": start_at, "end_at": end_at},
            )
        ).one()
        expected = int(row.expected)
        return ScorecardRow(
            metric_name="evds_freshness_pct",
            target=">= 95%",
            actual="—",
            status="warn",
            notes=f"ts.observation not present; expected {expected} releases",
        )
    row = (
        await session.execute(
            _SELECT_EVDS_FRESHNESS,
            {"start_at": start_at, "end_at": end_at},
        )
    ).one()
    expected = int(row.expected)
    observed = int(row.observed)
    if expected == 0:
        return ScorecardRow(
            metric_name="evds_freshness_pct",
            target=">= 95%",
            actual="—",
            status="warn",
            notes="no expected releases in window",
        )
    pct = 100.0 * observed / expected
    return ScorecardRow(
        metric_name="evds_freshness_pct",
        target=">= 95%",
        actual=f"{pct:.1f}%",
        status=_classify_gte(pct, pass_ge=95.0, warn_ge=90.0),
        notes=f"{observed}/{expected} on time",
    )


_SELECT_LATEST_COVERAGE = text(
    "SELECT coverage_pct::float AS pct, observed_at "
    "FROM audit.coverage_snapshot "
    "WHERE source = :source AND dimension = :dimension "
    "  AND observed_at < :end_at "
    "ORDER BY observed_at DESC LIMIT 1"
)


async def _metric_latest_coverage(
    *,
    session: AsyncSession,
    end_at: datetime,
    source: str,
    dimension: str,
    metric_name: str,
    target: str,
    pass_ge: float,
    warn_ge: float,
) -> ScorecardRow:
    """Read the most-recent ``audit.coverage_snapshot`` for
    (source, dimension) before ``end_at`` and classify."""
    if not await _table_present(session, "audit", "coverage_snapshot"):
        return ScorecardRow(
            metric_name=metric_name,
            target=target,
            actual="—",
            status="warn",
            notes="audit.coverage_snapshot not present",
        )
    row = (
        await session.execute(
            _SELECT_LATEST_COVERAGE,
            {"source": source, "dimension": dimension, "end_at": end_at},
        )
    ).one_or_none()
    if row is None or row.pct is None:
        return ScorecardRow(
            metric_name=metric_name,
            target=target,
            actual="—",
            status="warn",
            notes=f"no coverage_snapshot for {source}/{dimension}",
        )
    pct = float(row.pct)
    return ScorecardRow(
        metric_name=metric_name,
        target=target,
        actual=f"{pct:.1f}%",
        status=_classify_gte(pct, pass_ge=pass_ge, warn_ge=warn_ge),
        notes=f"observed_at={row.observed_at.isoformat()}",
    )


_SELECT_VALIDATION_FAILURE_RATE_KAP = text(
    "SELECT count(*)::int AS failures "
    "FROM audit.validation_failure "
    "WHERE source = 'kap' "
    "  AND detected_at >= :start_at AND detected_at < :end_at"
)


_SELECT_KAP_RECORDS_VALIDATED = text(
    "SELECT count(*)::int AS n "
    "FROM audit.recency_observation "
    "WHERE source = 'kap' "
    "  AND observed_at >= :start_at AND observed_at < :end_at"
)


async def _metric_validation_pass_rate_kap(
    *, session: AsyncSession, start_at: datetime, end_at: datetime
) -> ScorecardRow:
    """``validation_pass_rate_kap``: 1 - (failures / records_validated_proxy).

    The exact denominator (records-validated count) lives across the
    KAP puller's ingestion logs and is not surfaced to audit.* on this
    branch. We use the count of recency observations as a proxy
    (one observation per sweep ≈ one validation pass). A more accurate
    proxy lands when ``audit.sync_log.records_ingested`` aggregates are
    surfaced through the dashboard's static-SQL helpers (M7+).
    """
    if not await _table_present(session, "audit", "validation_failure"):
        return ScorecardRow(
            metric_name="validation_pass_rate_kap",
            target=">= 99%",
            actual="—",
            status="warn",
            notes="audit.validation_failure not present",
        )
    failures_row = (
        await session.execute(
            _SELECT_VALIDATION_FAILURE_RATE_KAP,
            {"start_at": start_at, "end_at": end_at},
        )
    ).one()
    failures = int(failures_row.failures)
    if not await _table_present(session, "audit", "recency_observation"):
        return ScorecardRow(
            metric_name="validation_pass_rate_kap",
            target=">= 99%",
            actual="—",
            status="warn",
            notes="recency_observation absent — cannot compute denominator",
        )
    obs_row = (
        await session.execute(
            _SELECT_KAP_RECORDS_VALIDATED,
            {"start_at": start_at, "end_at": end_at},
        )
    ).one()
    n = int(obs_row.n)
    if n == 0:
        return ScorecardRow(
            metric_name="validation_pass_rate_kap",
            target=">= 99%",
            actual="—",
            status="warn",
            notes="no kap observations in window",
        )
    pass_rate = 100.0 * (1.0 - (failures / n))
    return ScorecardRow(
        metric_name="validation_pass_rate_kap",
        target=">= 99%",
        actual=f"{pass_rate:.2f}%",
        status=_classify_gte(pass_rate, pass_ge=99.0, warn_ge=97.0),
        notes=f"{failures} failure(s) / {n} record(s) (proxy)",
    )


_SELECT_SPOT_CHECK_RATE_4W = text(
    "SELECT "
    "  count(*) FILTER (WHERE labelled = true)::int AS labelled, "
    "  count(*)::int AS total "
    "FROM audit.spot_check_sample "
    "WHERE drawn_at >= :start_at AND drawn_at < :end_at"
)


async def _metric_spot_check_completion_rate(
    *, session: AsyncSession, end_at: datetime
) -> ScorecardRow:
    """``spot_check_completion_rate_4w``: ratio of labelled samples over
    the trailing 4 weeks (28 days)."""
    if not await _table_present(session, "audit", "spot_check_sample"):
        return ScorecardRow(
            metric_name="spot_check_completion_rate_4w",
            target=">= 90%",
            actual="—",
            status="warn",
            notes="audit.spot_check_sample not present",
        )
    start_at = end_at - timedelta(days=28)
    row = (
        await session.execute(
            _SELECT_SPOT_CHECK_RATE_4W,
            {"start_at": start_at, "end_at": end_at},
        )
    ).one()
    labelled = int(row.labelled)
    total = int(row.total)
    if total == 0:
        return ScorecardRow(
            metric_name="spot_check_completion_rate_4w",
            target=">= 90%",
            actual="—",
            status="warn",
            notes="no spot-check samples in trailing 4 weeks",
        )
    pct = 100.0 * labelled / total
    return ScorecardRow(
        metric_name="spot_check_completion_rate_4w",
        target=">= 90%",
        actual=f"{pct:.1f}%",
        status=_classify_gte(pct, pass_ge=90.0, warn_ge=80.0),
        notes=f"{labelled}/{total} labelled in trailing 28d",
    )


_SELECT_LATEST_CLOSED_RUN_WINS = text(
    "SELECT r.run_id, r.quarter, "
    "  count(*) FILTER (WHERE c.aslan_advantage = 'wins')::int AS wins, "
    "  count(*)::int AS total_cells "
    "FROM audit.bloomberg_comparison_run r "
    "JOIN audit.bloomberg_comparison_cell c ON c.run_id = r.run_id "
    "WHERE r.closed_at IS NOT NULL "
    "GROUP BY r.run_id, r.quarter, r.closed_at "
    "ORDER BY r.closed_at DESC LIMIT 1"
)


async def _metric_bloomberg_wins(
    *, session: AsyncSession
) -> ScorecardRow:
    """``bloomberg_wins_count``: count of cells with aslan_advantage='wins'
    in the latest closed run."""
    if not await _table_present(session, "audit", "bloomberg_comparison_cell"):
        return ScorecardRow(
            metric_name="bloomberg_wins_count",
            target=">= 30 (out of 60)",
            actual="—",
            status="warn",
            notes="audit.bloomberg_comparison_cell not present",
        )
    row = (await session.execute(_SELECT_LATEST_CLOSED_RUN_WINS)).one_or_none()
    if row is None:
        return ScorecardRow(
            metric_name="bloomberg_wins_count",
            target=">= 30 (out of 60)",
            actual="—",
            status="warn",
            notes="no closed Bloomberg-comparison run yet",
        )
    wins = int(row.wins)
    total = int(row.total_cells)
    return ScorecardRow(
        metric_name="bloomberg_wins_count",
        target=">= 30 (out of 60)",
        actual=f"{wins} / {total}",
        status=_classify_gte(float(wins), pass_ge=30.0, warn_ge=20.0),
        notes=f"latest closed run quarter={row.quarter}",
    )


_SELECT_REGRESSION_OPEN = text(
    "SELECT count(*)::int AS n FROM audit.regression_flag WHERE status = 'open'"
)


async def _metric_regression_open(
    *, session: AsyncSession
) -> ScorecardRow:
    """``regression_flag_open_count``: count of audit.regression_flag rows
    with status='open' (point-in-time, not week-windowed)."""
    if not await _table_present(session, "audit", "regression_flag"):
        return ScorecardRow(
            metric_name="regression_flag_open_count",
            target="<= 5",
            actual="—",
            status="warn",
            notes="audit.regression_flag not present",
        )
    row = (await session.execute(_SELECT_REGRESSION_OPEN)).one()
    n = int(row.n)
    return ScorecardRow(
        metric_name="regression_flag_open_count",
        target="<= 5",
        actual=str(n),
        status=_classify_lte(float(n), pass_le=5.0, warn_le=10.0),
        notes=f"{n} open flag(s)",
    )


# Six canonical cross-source rules per spec §7.3. Match the
# ``rule_name`` values written by ``aslan_core.dq.cross_source.run_all``.
_XS_RULE_NAMES: tuple[str, ...] = (
    "xs_kap_to_ref_entity",
    "xs_kap_to_mkk_capital_action",
    "xs_tefas_nav_reconciliation",
    "xs_evds_release_calendar",
    "xs_bist_to_ref_entity",
    "xs_kap_to_bist_corporate_action",
)


_SELECT_XS_RULE_FAILURES = text(
    "SELECT rule_name, count(*)::int AS failures "
    "FROM audit.validation_failure "
    "WHERE rule_name = ANY(:rule_names) "
    "  AND detected_at >= :start_at AND detected_at < :end_at "
    "GROUP BY rule_name"
)


async def _metric_xs_clean(
    *, session: AsyncSession, start_at: datetime, end_at: datetime
) -> ScorecardRow:
    """``cross_source_rule_clean_count``: count of cross-source rules that
    fired 0 failures over the week."""
    if not await _table_present(session, "audit", "validation_failure"):
        return ScorecardRow(
            metric_name="cross_source_rule_clean_count",
            target=">= 5 (out of 6)",
            actual="—",
            status="warn",
            notes="audit.validation_failure not present",
        )
    rows = (
        await session.execute(
            _SELECT_XS_RULE_FAILURES,
            {
                "rule_names": list(_XS_RULE_NAMES),
                "start_at": start_at,
                "end_at": end_at,
            },
        )
    ).all()
    rules_with_failures = {r.rule_name for r in rows}
    clean = len(_XS_RULE_NAMES) - len(rules_with_failures)
    return ScorecardRow(
        metric_name="cross_source_rule_clean_count",
        target=">= 5 (out of 6)",
        actual=f"{clean} / {len(_XS_RULE_NAMES)}",
        status=_classify_gte(float(clean), pass_ge=5.0, warn_ge=4.0),
        notes=(
            f"{len(rules_with_failures)} rule(s) failed: "
            f"{','.join(sorted(rules_with_failures)) or 'none'}"
        ),
    )


# ── compute / write ──────────────────────────────────────────────


async def compute(
    *,
    session: AsyncSession,
    week_start: date,
) -> list[ScorecardRow]:
    """Compute the 10 weekly scorecard metrics for the trailing 7 days
    starting at ``week_start`` (ISO-week-Monday, UTC).

    Returns rows in canonical metric-name order so the dashboard render
    is diff-stable across runs.
    """
    if week_start.isoweekday() != 1:
        raise ValueError(f"week_start must be a Monday; got {week_start.isoformat()}")
    start_at, end_at = _week_window(week_start)
    rows: list[ScorecardRow] = []

    # Recency p95/p99 (KAP).
    p95_row, p99_row = await _metric_kap_recency(
        session=session, start_at=start_at, end_at=end_at
    )
    rows.append(p95_row)
    rows.append(p99_row)

    # EVDS freshness.
    rows.append(
        await _metric_evds_freshness(session=session, start_at=start_at, end_at=end_at)
    )

    # Coverage snapshots.
    rows.append(
        await _metric_latest_coverage(
            session=session,
            end_at=end_at,
            source="kap",
            dimension="filings_today",
            metric_name="kap_filings_today_coverage",
            target=">= 99%",
            pass_ge=99.0,
            warn_ge=95.0,
        )
    )
    rows.append(
        await _metric_latest_coverage(
            session=session,
            end_at=end_at,
            source="bist",
            dimension="entity",
            metric_name="bist_entity_coverage",
            target="100%",
            pass_ge=100.0,
            warn_ge=99.0,
        )
    )

    # Validation pass rate (KAP).
    rows.append(
        await _metric_validation_pass_rate_kap(
            session=session, start_at=start_at, end_at=end_at
        )
    )

    # Spot-check completion (trailing 4 weeks).
    rows.append(await _metric_spot_check_completion_rate(session=session, end_at=end_at))

    # Bloomberg wins.
    rows.append(await _metric_bloomberg_wins(session=session))

    # Regression flag open count.
    rows.append(await _metric_regression_open(session=session))

    # Cross-source rule clean count.
    rows.append(await _metric_xs_clean(session=session, start_at=start_at, end_at=end_at))

    return rows


async def write(
    *,
    session: AsyncSession,
    week_start: date,
    rows: list[ScorecardRow],
) -> int:
    """UPSERT each row into ``audit.scorecard_snapshot``. Returns the
    number of rows written (always equals ``len(rows)`` on success).

    Idempotent — re-running for the same ``week_start`` overwrites
    ``actual`` / ``status`` / ``notes`` while keeping ``recorded_at``
    at the original insert time.
    """
    written = 0
    for r in rows:
        await session.execute(
            UPSERT_SCORECARD_SNAPSHOT,
            {
                "week_start": week_start,
                "metric_name": r.metric_name,
                "target": r.target,
                "actual": r.actual,
                "status": r.status,
                "notes": r.notes,
            },
        )
        written += 1
    return written


async def read_week(
    *,
    session: AsyncSession,
    week_start: date,
) -> list[ScorecardRow]:
    """Read back all rows for ``week_start`` from ``audit.scorecard_snapshot``."""
    rows = (
        await session.execute(
            SELECT_SCORECARD_SNAPSHOT_BY_WEEK,
            {"week_start": week_start},
        )
    ).all()
    return [
        ScorecardRow(
            metric_name=r.metric_name,
            target=r.target,
            actual=r.actual,
            status=r.status,
            notes=r.notes,
        )
        for r in rows
    ]


# ── Render — email + HTML export ─────────────────────────────────


_STATUS_COLOURS: dict[str, tuple[str, str]] = {
    # (background, text)
    "pass": ("#d4edda", "#155724"),
    "warn": ("#fff3cd", "#856404"),
    "fail": ("#f8d7da", "#721c24"),
}


def _summary_pass_pct(rows: list[ScorecardRow]) -> float:
    if not rows:
        return 0.0
    pass_n = sum(1 for r in rows if r.status == "pass")
    return 100.0 * pass_n / len(rows)


def _render_table_html(rows: list[ScorecardRow]) -> str:
    """Build the ``<table>`` block; called by both email + standalone HTML."""
    parts = [
        '<table border="1" cellpadding="6" cellspacing="0" '
        'style="border-collapse:collapse;font-family:monospace;'
        'font-size:13px;border:1px solid #999;">',
        "<thead><tr>"
        '<th align="left">Metric</th>'
        '<th align="left">Target</th>'
        '<th align="left">Actual</th>'
        '<th align="left">Status</th>'
        '<th align="left">Notes</th>'
        "</tr></thead>",
        "<tbody>",
    ]
    for r in rows:
        bg, fg = _STATUS_COLOURS.get(r.status, ("#ffffff", "#000000"))
        parts.append(
            f'<tr style="background:{bg};color:{fg};">'
            f"<td><code>{html_escape(r.metric_name)}</code></td>"
            f"<td>{html_escape(r.target)}</td>"
            f"<td><strong>{html_escape(r.actual)}</strong></td>"
            f'<td><strong>{html_escape(r.status.upper())}</strong></td>'
            f"<td>{html_escape(r.notes or '')}</td>"
            f"</tr>"
        )
    parts.append("</tbody></table>")
    return "".join(parts)


def render_email(*, week_start: date, rows: list[ScorecardRow]) -> tuple[str, str]:
    """Return ``(subject, body_html)`` for the weekly digest email.

    Subject format per task spec: ``[ASLAN AUDIT] Weekly scorecard — week of YYYY-MM-DD``.
    """
    subject = f"[ASLAN AUDIT] Weekly scorecard — week of {week_start.isoformat()}"
    pass_pct = _summary_pass_pct(rows)
    pass_n = sum(1 for r in rows if r.status == "pass")
    warn_n = sum(1 for r in rows if r.status == "warn")
    fail_n = sum(1 for r in rows if r.status == "fail")
    body = (
        "<html><body>"
        f"<h2>Aslan weekly scorecard — week of {week_start.isoformat()}</h2>"
        f"<p><strong>{pass_pct:.0f}% pass</strong> "
        f"({pass_n} pass / {warn_n} warn / {fail_n} fail "
        f"across {len(rows)} metrics).</p>"
        f"{_render_table_html(rows)}"
        "<p style='font-size:11px;color:#666;'>"
        "Generated by <code>aslan-core audit scorecard</code>. "
        "Stored verbatim in <code>audit.scorecard_snapshot</code>."
        "</p>"
        "</body></html>"
    )
    return subject, body


def render_html(*, week_start: date, rows: list[ScorecardRow]) -> str:
    """Standalone HTML export — same body as ``render_email`` but with
    a doctype + ``<title>`` so it stands alone as a downloadable file.

    PDF export is deferred to M6.1 (needs a PDF library install). The
    dashboard's "Export PDF" button serves this HTML; downstream the
    user can ``Print → Save as PDF`` from the browser. Ticket the real
    PDF render when WeasyPrint or similar is added to ``[obs]`` extras.
    """
    pass_pct = _summary_pass_pct(rows)
    pass_n = sum(1 for r in rows if r.status == "pass")
    warn_n = sum(1 for r in rows if r.status == "warn")
    fail_n = sum(1 for r in rows if r.status == "fail")
    return (
        "<!doctype html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        f"<title>Aslan weekly scorecard — {week_start.isoformat()}</title>"
        "</head><body>"
        f"<h1>Aslan weekly scorecard — week of {week_start.isoformat()}</h1>"
        f"<p><strong>{pass_pct:.0f}% pass</strong> "
        f"({pass_n} pass / {warn_n} warn / {fail_n} fail "
        f"across {len(rows)} metrics).</p>"
        f"{_render_table_html(rows)}"
        "<p style='font-size:11px;color:#666;'>"
        "Generated by <code>aslan-core audit scorecard</code>. "
        "TODO(M6.1): real PDF export via WeasyPrint or reportlab. "
        "v1 serves this HTML and relies on browser print-to-PDF."
        "</p>"
        "</body></html>"
    )


def render_text_fallback(*, week_start: date, rows: list[ScorecardRow]) -> str:
    """Plain-text fallback for the email payload's ``text/plain`` part.

    Email clients that don't render HTML get a readable monospace
    dump instead of a JSON blob. Keeps the digest useful in a terminal
    mail client.
    """
    pass_pct = _summary_pass_pct(rows)
    lines = [
        f"Aslan weekly scorecard — week of {week_start.isoformat()}",
        f"{pass_pct:.0f}% pass across {len(rows)} metrics.",
        "",
        f"{'metric':40s} {'target':20s} {'actual':20s} {'status':6s} notes",
        "-" * 110,
    ]
    for r in rows:
        lines.append(
            f"{r.metric_name:40s} {r.target:20s} {r.actual:20s} "
            f"{r.status:6s} {r.notes or ''}"
        )
    return "\n".join(lines) + "\n"


__all__ = [
    "ScorecardRow",
    "ScorecardStatus",
    "compute",
    "latest_week_start",
    "read_week",
    "render_email",
    "render_html",
    "render_text_fallback",
    "write",
]
