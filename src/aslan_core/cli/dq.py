"""``aslan-core audit`` CLI group — M1 + M2 + M3.

Subcommands:

  * ``recency-sweep`` — for each (source, dimension) row in
    ``audit.recency_sla``, probe upstream + db, write one row to
    ``audit.recency_observation``, and emit
    ``audit.event(event_type='recency_sla_breach')`` if breached.
    See spec §6.1.

  * ``coverage-snapshot`` — for each source x dimension defined in
    spec §8, write one row to ``audit.coverage_snapshot``. See
    spec §8.

  * ``spot-check-draw`` — weekly random sample (Mon 06:00 UTC per
    spec §13). For each requested source draws N rows into
    ``audit.spot_check_sample`` for /dq/spot-check labelling.

  * ``alert-dispatch`` — evaluate ``audit.severity_rule`` predicates
    against recent audit.* rows; enqueue + drain pending
    ``audit.alert_dispatch`` rows through the GlitchTip / email /
    Slack sinks. See spec §10.3 + §10.4.

  * ``test-alert`` — emit a synthetic alert through the configured
    sinks for live-deployment smoke testing.

Cadence:

  * recency-sweep — every 60s (spec §10.2)
  * coverage-snapshot — every 5m (spec §10.2)
  * spot-check-draw — weekly Mon 06:00 UTC (spec §13)
  * alert-dispatch — every 60s (spec §10.4)

Each writes durable rows that the dashboard reads back via the
/dq/* pages.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from typing import Any, Protocol

import click
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.config import Settings
from aslan_core.db.engine import create_engine
from aslan_core.db.session import create_session_factory
from aslan_core.dq import (
    alert_dispatch,
    bloomberg,
    coverage,
    cross_source,
    event,
    recency,
    regression_detect,
    scorecard,
    spot_check,
)
from aslan_core.dq._sql import SELECT_RECENCY_SLA
from aslan_core.dq.probes import SOURCES, get_probe
from aslan_core.dq.sinks import Sink, SinkNotConfigured, build_default_sinks
from aslan_core.dq.types import Severity


@click.group("audit")
def audit() -> None:
    """Operational data-quality / audit cron commands.

    NOTE: distinct from ``aslan_core.audit`` (the actor-attribution
    mutation recorder). This group operates on the ``audit.*``
    operational tables seeded by migrations 0053-0056.
    """


# ── recency-sweep ──────────────────────────────────────────────────


@audit.command("recency-sweep")
@click.option(
    "--source",
    "source_filter",
    default=None,
    help="Limit the sweep to one source (kap|evds|bist|tefas|mkk). Default: all.",
)
def recency_sweep_cmd(source_filter: str | None) -> None:
    """Probe upstream + db freshness per source/dimension; write
    audit.recency_observation rows; emit recency_sla_breach events."""
    summary = asyncio.run(_run_recency_sweep(source_filter))
    click.echo(
        f"recency-sweep complete: {summary['observed']} observations, "
        f"{summary['breached']} breached, {summary['skipped']} skipped"
    )


async def _run_recency_sweep(source_filter: str | None) -> dict[str, int]:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as s:
            rows = (await s.execute(SELECT_RECENCY_SLA)).all()
            observed = 0
            breached = 0
            skipped = 0
            sweep_at = datetime.now(UTC)
            for row in rows:
                source = row.source
                if source_filter is not None and source != source_filter:
                    continue
                dimension = row.dimension
                sla_seconds = int(row.sla_seconds)
                try:
                    probe = get_probe(source)
                except ValueError:
                    # Source seeded in audit.recency_sla but no probe
                    # registered — log and skip rather than crash the
                    # whole sweep.
                    skipped += 1
                    await event.emit(
                        session=s,
                        event_type="recency_probe_skipped",
                        emitter=f"recency-sweep:{source}",
                        severity=Severity.WARN,
                        payload={
                            "source": source,
                            "dimension": dimension,
                            "reason": "no probe registered for source",
                        },
                    )
                    continue
                upstream_ts, upstream_detail = await probe.upstream_latest(s, dimension)
                db_ts, db_detail = await probe.db_latest(s, dimension)
                probe_detail = {
                    "dimension": dimension,
                    "upstream": upstream_detail,
                    "db": db_detail,
                }
                if upstream_ts is None or db_ts is None:
                    # Either calendar empty or table absent. Skip the
                    # observation insert (the table requires both
                    # NOT NULL via the generated lag column) but emit
                    # a `recency_probe_skipped` event so the dashboard
                    # records the gap.
                    skipped += 1
                    await event.emit(
                        session=s,
                        event_type="recency_probe_skipped",
                        emitter=f"recency-sweep:{source}",
                        severity=Severity.INFO,
                        payload={
                            "source": source,
                            "dimension": dimension,
                            "upstream_latest_at": (
                                upstream_ts.isoformat() if upstream_ts is not None else None
                            ),
                            "db_latest_at": (db_ts.isoformat() if db_ts is not None else None),
                            "probe_detail": probe_detail,
                        },
                    )
                    continue
                obs = await recency.observe(
                    session=s,
                    source=source,
                    observed_at=sweep_at,
                    upstream_latest_at=upstream_ts,
                    db_latest_at=db_ts,
                    sla_target_seconds=sla_seconds,
                    probe_detail=probe_detail,
                )
                observed += 1
                if obs.sla_breached:
                    breached += 1
                    await event.emit(
                        session=s,
                        event_type="recency_sla_breach",
                        emitter=f"recency-sweep:{source}",
                        severity=Severity.ERROR,
                        payload={
                            "source": source,
                            "dimension": dimension,
                            "observation_id": obs.observation_id,
                            "lag_seconds": obs.lag_seconds,
                            "sla_target_seconds": sla_seconds,
                        },
                    )
            await s.commit()
            return {"observed": observed, "breached": breached, "skipped": skipped}
    finally:
        await engine.dispose()


# ── coverage-snapshot ──────────────────────────────────────────────


# A coverage probe is a tiny async function that, given a source-naming
# context (now() and the AsyncSession), returns the (expected, actual,
# missing_ids) tuple for one (source, dimension) pair. Each is wired
# into the cron via the COVERAGE_PROBES table below.
class _CoverageProbe(Protocol):
    async def __call__(
        self, session: AsyncSession, now: datetime
    ) -> tuple[int | None, int | None, dict[str, Any] | None]: ...


async def _coverage_bist_entity(
    session: AsyncSession, now: datetime
) -> tuple[int | None, int | None, dict[str, Any] | None]:
    """BIST roster coverage: distinct entities holding a `bist_ticker`
    identifier in `ref.identifier`.

    Per the workspace convention (see `aslan_core.query.entity`), an
    entity is considered BIST-listed when it has a current
    `ref.identifier(namespace='bist_ticker')` row. The valid_to clause
    keeps the count bitemporally point-in-time correct.

    Expected count is hard-coded at 502 — the BIST main board hovers
    around 500 issuers. # TODO(M1.1): replace with the BIST puller's
    roster watermark (a row in ``audit.coverage_watermark`` keyed by
    source='bist').
    """
    _ = now
    if not await _table_present(session, "ref", "identifier"):
        return None, None, {"reason": "ref.identifier not present"}
    actual_row = (
        await session.execute(
            text(
                "SELECT count(DISTINCT entity_id)::int AS n "
                "FROM ref.identifier "
                "WHERE namespace = 'bist_ticker' "
                "  AND valid_from <= current_date AND valid_to > current_date"
            )
        )
    ).one()
    actual = int(actual_row.n)
    return 502, actual, None  # TODO(M1.1): real expected from puller watermark


async def _coverage_kap_filings_today(
    session: AsyncSession, now: datetime
) -> tuple[int | None, int | None, dict[str, Any] | None]:
    """KAP filings today: kap.disclosures WHERE published_at::date = today.

    Expected = actual placeholder (no upstream poll for v1).
    # TODO(M1.1): HTTP-query the KAP listing endpoint for today's
    publication count and use that as `expected`.
    """
    _ = now
    if not await _table_present(session, "kap", "disclosures"):
        return None, None, {"reason": "kap.disclosures not present"}
    row = (
        await session.execute(
            text(
                "SELECT count(*)::int AS n FROM kap.disclosures "
                "WHERE published_at::date = current_date"
            )
        )
    ).one()
    actual = int(row.n)
    return actual, actual, None  # TODO(M1.1): real expected from KAP API


async def _coverage_kap_historical_depth(
    session: AsyncSession, now: datetime
) -> tuple[int | None, int | None, dict[str, Any] | None]:
    """5y historical-depth coverage on ts.canonical_financial.

    Per spec §8: an entity is "missing" if it has fewer than 20
    canonical_financial rows in the trailing 5 years (5y x 4Q). For
    v1 this is a corpus-wide count; per-entity gaps are tabulated
    only when ts.canonical_financial is present.
    """
    _ = now
    if not await _table_present(session, "ts", "canonical_financial"):
        return None, None, {"reason": "ts.canonical_financial not present"}
    if not await _table_present(session, "ref", "identifier"):
        return None, None, {"reason": "ref.identifier not present"}
    row = (
        await session.execute(
            text(
                "SELECT "
                "  (SELECT count(DISTINCT entity_id)::int FROM ts.canonical_financial "
                "     WHERE period_end >= current_date - INTERVAL '5 years') AS depth_ok, "
                "  (SELECT count(DISTINCT entity_id)::int FROM ref.identifier "
                "     WHERE namespace = 'bist_ticker' "
                "       AND valid_from <= current_date AND valid_to > current_date) "
                "    AS expected"
            )
        )
    ).one()
    return int(row.expected), int(row.depth_ok), None


async def _coverage_evds_series(
    session: AsyncSession, now: datetime
) -> tuple[int | None, int | None, dict[str, Any] | None]:
    """EVDS series coverage: count enrolled series in series_catalog
    if present.

    For v1, we count all `ts.series_catalog` rows whose source is EVDS
    (no per-source filter is currently kept on series_catalog so we
    use a placeholder of "every series"). # TODO(M1.1): wire to the
    EVDS puller's enrolled-series watermark.
    """
    _ = now
    if not await _table_present(session, "ts", "series_catalog"):
        return None, None, {"reason": "ts.series_catalog not present"}
    row = (
        await session.execute(
            text("SELECT count(*)::int AS n FROM ts.series_catalog WHERE source_id = 'evds'")
        )
    ).one()
    n = int(row.n)
    return n, n, None  # TODO(M1.1): expected from EVDS puller manifest


async def _coverage_tefas_funds(
    session: AsyncSession, now: datetime
) -> tuple[int | None, int | None, dict[str, Any] | None]:
    """TEFAS funds coverage: fresh fund holdings vs registered funds.

    # TODO(M1.1): expected = upstream count of registered funds; for
    v1 we use actual=fresh_funds and expected=fresh_funds so coverage
    is reported as 100% with the placeholder gap noted.
    """
    _ = now
    if not await _table_present(session, "tefas", "fund_holding"):
        return None, None, {"reason": "tefas.fund_holding not present"}
    row = (
        await session.execute(
            text(
                "SELECT count(DISTINCT fund_id)::int AS n FROM tefas.fund_holding "
                "WHERE snapshot_date >= current_date - INTERVAL '7 days'"
            )
        )
    ).one()
    n = int(row.n)
    return n, n, None  # TODO(M1.1): real expected from TEFAS roster


# (source, dimension) → probe. Iterated by the cron in this order.
_COVERAGE_PROBES: tuple[tuple[str, str, _CoverageProbe], ...] = (
    ("bist", "entity", _coverage_bist_entity),
    ("kap", "filings_today", _coverage_kap_filings_today),
    ("kap", "historical_depth_5y", _coverage_kap_historical_depth),
    ("evds", "series", _coverage_evds_series),
    ("tefas", "funds", _coverage_tefas_funds),
)


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


@audit.command("coverage-snapshot")
def coverage_snapshot_cmd() -> None:
    """Compute coverage-snapshot rows for each known source x dimension."""
    summary = asyncio.run(_run_coverage_snapshot())
    click.echo(
        f"coverage-snapshot complete: {summary['written']} written, {summary['skipped']} skipped"
    )


async def _run_coverage_snapshot() -> dict[str, int]:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as s:
            written = 0
            skipped = 0
            now = datetime.now(UTC)
            now_iso = now.isoformat()
            for source, dimension, probe in _COVERAGE_PROBES:
                expected, actual, missing = await probe(s, now)
                if expected is None and actual is None:
                    # Probe couldn't run (table absent) — emit an event
                    # so operators see the gap, but skip the snapshot
                    # row (coverage_pct would be NULL).
                    skipped += 1
                    await event.emit(
                        session=s,
                        event_type="coverage_probe_skipped",
                        emitter=f"coverage-snapshot:{source}",
                        severity=Severity.INFO,
                        payload={
                            "source": source,
                            "dimension": dimension,
                            "missing": missing,
                        },
                    )
                    continue
                snap_id = await coverage.snapshot(
                    session=s,
                    source=source,
                    dimension=dimension,
                    observed_at=now_iso,
                    expected_count=expected,
                    actual_count=actual,
                    missing_ids=missing,
                )
                if snap_id is not None:
                    written += 1
            await s.commit()
            return {"written": written, "skipped": skipped}
    finally:
        await engine.dispose()


# ── spot-check-draw ────────────────────────────────────────────────


# All 5 sources draw their primary table (kap.disclosures,
# bist.daily_ohlcv, …) per dq.spot_check internal catalogue. KAP
# alone gets a stratified high-priority over-sample (spec §7.2).
_SPOT_CHECK_SOURCES: tuple[str, ...] = ("kap", "bist", "evds", "tefas", "mkk")


@audit.command("spot-check-draw")
@click.option(
    "--source",
    "source_filter",
    default=None,
    help=("Limit the draw to one source (kap|bist|evds|tefas|mkk). Default: draw across all five."),
)
@click.option(
    "--n",
    "n_per_source",
    type=int,
    default=10,
    show_default=True,
    help="Rows to draw per source.",
)
def spot_check_draw_cmd(source_filter: str | None, n_per_source: int) -> None:
    """Draw N random rows per source into audit.spot_check_sample.

    For KAP, the draw is stratified over high-priority event types
    (material_event, dividend, share_buyback, capital_action) at 2x
    weight — see spec §7.2. Cron schedule: Mon 06:00 UTC.
    """
    summary = asyncio.run(_run_spot_check_draw(source_filter, n_per_source))
    click.echo(
        f"spot-check-draw complete: {summary['drawn']} samples drawn across "
        f"{summary['sources']} source(s)"
    )


async def _run_spot_check_draw(source_filter: str | None, n_per_source: int) -> dict[str, int]:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as s:
            drawn = 0
            sources_touched = 0
            sources = (source_filter,) if source_filter is not None else _SPOT_CHECK_SOURCES
            for source in sources:
                stratum = "high_priority_event_type" if source == "kap" else None
                try:
                    sample_ids = await spot_check.draw_sample(
                        session=s,
                        source=source,
                        n=n_per_source,
                        stratum=stratum,
                    )
                except ValueError as exc:
                    # Bad source filter (e.g. typo on the CLI) — log
                    # rather than crash the whole sweep.
                    await event.emit(
                        session=s,
                        event_type="spot_check_draw_invalid",
                        emitter=f"spot-check-draw:{source}",
                        severity=Severity.WARN,
                        payload={"source": source, "error": str(exc)},
                    )
                    continue
                drawn += len(sample_ids)
                if sample_ids:
                    sources_touched += 1
            await s.commit()
            return {"drawn": drawn, "sources": sources_touched}
    finally:
        await engine.dispose()


# ── alert-dispatch ─────────────────────────────────────────────────


@audit.command("alert-dispatch")
@click.option(
    "--evaluate-only",
    "evaluate_only",
    is_flag=True,
    default=False,
    help=(
        "Just evaluate severity rules + enqueue audit.alert_dispatch rows; "
        "do not deliver pending rows through the sinks."
    ),
)
@click.option(
    "--dispatch-only",
    "dispatch_only",
    is_flag=True,
    default=False,
    help=("Just deliver pending audit.alert_dispatch rows; do not run the predicate evaluator."),
)
def alert_dispatch_cmd(evaluate_only: bool, dispatch_only: bool) -> None:
    """Evaluate severity rules + drain pending alert dispatches.

    Default behaviour (no flags) is the canonical cron loop: run the
    predicate evaluator first to land freshly-fired conditions in
    ``audit.alert_dispatch``, then drain pending rows through the
    sinks.

    --evaluate-only and --dispatch-only are mutually exclusive ways
    to run only half the loop (useful for testing and for splitting
    enqueue and dispatch onto separate cron schedules in the future).
    """
    if evaluate_only and dispatch_only:
        raise click.UsageError("--evaluate-only and --dispatch-only are mutually exclusive")
    summary = asyncio.run(
        _run_alert_dispatch(evaluate_only=evaluate_only, dispatch_only=dispatch_only)
    )
    click.echo(
        f"alert-dispatch complete: {summary['enqueued']} enqueued, {summary['delivered']} delivered"
    )


async def _run_alert_dispatch(
    *,
    evaluate_only: bool,
    dispatch_only: bool,
    settings: Settings | None = None,
    sinks: dict[str, Sink] | None = None,
) -> dict[str, int]:
    """Inner async loop. The ``settings`` / ``sinks`` kwargs are test-only
    overrides — production callers go through the click command which
    constructs a real ``Settings()`` and the canonical default sinks.
    """
    if settings is None:
        settings = Settings()
    enqueued = 0
    delivered = 0
    if not dispatch_only:
        engine = create_engine()
        factory = create_session_factory(engine)
        try:
            async with factory() as s:
                enqueued = await alert_dispatch.evaluate_and_enqueue(session=s, settings=settings)
                await s.commit()
        finally:
            await engine.dispose()
    if not evaluate_only:
        delivered = await alert_dispatch.dispatch_pending(settings=settings, sinks=sinks)
    return {"enqueued": enqueued, "delivered": delivered}


# ── test-alert ─────────────────────────────────────────────────────


@audit.command("test-alert")
@click.option(
    "--severity",
    type=click.Choice(["info", "warn", "error", "critical"]),
    default="info",
    show_default=True,
    help="Severity stamp on the synthetic event + dispatch row.",
)
@click.option(
    "--sink",
    "sink_filter",
    type=click.Choice(["glitchtip", "email", "slack", "all"]),
    default="all",
    show_default=True,
    help="Restrict the synthetic dispatch to one sink. Default fans out to all three.",
)
def test_alert_cmd(severity: str, sink_filter: str) -> None:
    """Emit one synthetic alert through the configured sinks.

    Useful for live-deployment smoke testing — no real upstream
    condition required. The synthetic dispatch lands a row in
    ``audit.alert_dispatch`` with ``rule_name='test_alert'`` and a
    payload that identifies it as synthetic.

    NOTE: ``test_alert`` is NOT a row in ``audit.severity_rule``; the
    dispatch row uses a fabricated ``rule_name`` that the dispatcher
    will skip on subsequent runs (no severity_rule entry → no
    re-enqueue). The synthetic dispatch is drained immediately by this
    command; subsequent ``alert-dispatch`` cron runs see no leftover.
    """
    summary = asyncio.run(_run_test_alert(severity=severity, sink_filter=sink_filter))
    click.echo(
        f"test-alert complete: {summary['delivered']} delivered, "
        f"{summary['suppressed']} suppressed, {summary['failed']} failed"
    )


async def _run_test_alert(
    *,
    severity: str,
    sink_filter: str,
    settings: Settings | None = None,
    sinks: dict[str, Sink] | None = None,
) -> dict[str, int]:
    """Inner async test-alert. ``settings`` / ``sinks`` kwargs are test-only
    overrides for the integration test."""
    if settings is None:
        settings = Settings()
    if sinks is None:
        sinks = build_default_sinks(settings)
    target_sinks = list(sinks.keys()) if sink_filter == "all" else [sink_filter]
    synthetic_payload: dict[str, Any] = {
        "synthetic": True,
        "test_alert_id": str(uuid.uuid4()),
        "emitted_at": datetime.now(UTC).isoformat(),
        "note": "synthetic test-alert; safe to ignore",
    }
    delivered = 0
    suppressed = 0
    failed = 0
    # Emit the audit.event marker so the dashboard's event stream
    # carries a record of the smoke test, then drive each sink in turn
    # with the synthetic payload. We bypass the audit.alert_dispatch
    # table here (the synthetic rule_name doesn't exist in
    # audit.severity_rule, so an INSERT would FK-fail) and call the
    # sinks directly.
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as s:
            await event.emit(
                session=s,
                event_type="test_alert_emitted",
                emitter="cli:audit-test-alert",
                severity=Severity(severity),
                payload=synthetic_payload,
            )
            await s.commit()
    finally:
        await engine.dispose()

    for sink_name in target_sinks:
        sink_impl = sinks.get(sink_name)
        if sink_impl is None:
            failed += 1
            click.echo(f"  {sink_name}: unknown sink", err=True)
            continue
        try:
            ok = await sink_impl.deliver(
                payload=synthetic_payload,
                severity=severity,
                rule_name="test_alert",
            )
        except Exception as exc:
            from aslan_core.dq.sinks import SinkNotConfigured

            if isinstance(exc, SinkNotConfigured):
                suppressed += 1
                click.echo(f"  {sink_name}: suppressed ({exc})", err=True)
            else:
                failed += 1
                click.echo(f"  {sink_name}: failed ({exc})", err=True)
            continue
        if ok:
            delivered += 1
            click.echo(f"  {sink_name}: delivered")
        else:
            failed += 1
            click.echo(f"  {sink_name}: returned False", err=True)
    return {"delivered": delivered, "suppressed": suppressed, "failed": failed}


# ── bloomberg-* commands ───────────────────────────────────────────


@audit.command("bloomberg-open-quarter")
@click.option(
    "--quarter",
    "quarter",
    default=None,
    help="YYYYQn label (e.g. 2026Q2). Defaults to the calendar quarter containing now().",
)
def bloomberg_open_quarter_cmd(quarter: str | None) -> None:
    """Open a Bloomberg-comparison run for ``quarter``.

    Idempotent: a second invocation for the same quarter prints the
    existing run_id and exits cleanly. Creates 60 cells (5 entities x
    12 fields) on first run; subsequent calls touch nothing.
    """
    target = quarter or bloomberg.current_quarter()
    summary = asyncio.run(_run_bloomberg_open_quarter(target))
    click.echo(
        f"bloomberg-open-quarter: quarter={summary['quarter']} "
        f"run_id={summary['run_id']} cells_per_run={bloomberg.CELLS_PER_RUN}"
    )


async def _run_bloomberg_open_quarter(quarter: str) -> dict[str, str]:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as s:
            run_id = await bloomberg.open_quarter(session=s, quarter=quarter)
            await s.commit()
        return {"quarter": quarter, "run_id": str(run_id)}
    finally:
        await engine.dispose()


@audit.command("bloomberg-close-quarter")
@click.option("--quarter", "quarter", required=True, help="YYYYQn label, e.g. 2026Q2.")
@click.option(
    "--closed-by",
    "closed_by",
    default=None,
    help="Identity stamped on closed_by; defaults to the CLI actor.",
)
def bloomberg_close_quarter_cmd(quarter: str, closed_by: str | None) -> None:
    """Close ``quarter``. Refuses if any cell has bloomberg_value=NULL."""
    actor = closed_by or f"cli:{getpass_user()}"
    try:
        asyncio.run(_run_bloomberg_close_quarter(quarter=quarter, closed_by=actor))
    except bloomberg.QuarterNotReadyError as exc:
        raise click.ClickException(str(exc)) from exc
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"bloomberg-close-quarter: quarter={quarter} closed_by={actor}")


def getpass_user() -> str:
    """Return the current CLI user (delegated import to keep the CLI's
    top-level import block stable)."""
    import getpass

    return getpass.getuser()


async def _run_bloomberg_close_quarter(*, quarter: str, closed_by: str) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as s:
            await bloomberg.close_quarter(session=s, quarter=quarter, closed_by=closed_by)
            await s.commit()
    finally:
        await engine.dispose()


@audit.command("bloomberg-sample")
def bloomberg_sample_cmd() -> None:
    """Drain NULL aslan_value cells across all open runs.

    Iterates every cell in any non-closed run where ``aslan_value IS
    NULL`` and runs the per-field auto-sampler. Cron schedule: nightly
    (e.g. ``5 1 * * *``).
    """
    summary = asyncio.run(_run_bloomberg_sample())
    click.echo(
        f"bloomberg-sample: {summary['sampled']} cells sampled, "
        f"{summary['placeholders']} placeholders"
    )


async def _run_bloomberg_sample() -> dict[str, int]:
    engine = create_engine()
    factory = create_session_factory(engine)
    sampled = 0
    placeholders = 0
    try:
        async with factory() as s:
            cell_ids = await bloomberg.open_null_aslan_cells(session=s)
        for cid in cell_ids:
            async with factory() as s:
                # Snapshot pre-event count so we can detect placeholder
                # firings without reaching into the bloomberg module's
                # internals.
                pre = (
                    await s.execute(
                        text(
                            "SELECT count(*)::int AS n FROM audit.event "
                            "WHERE event_type = 'bloomberg_sampler_placeholder'"
                        )
                    )
                ).scalar_one()
                await bloomberg.record_aslan_value(session=s, cell_id=cid)
                post = (
                    await s.execute(
                        text(
                            "SELECT count(*)::int AS n FROM audit.event "
                            "WHERE event_type = 'bloomberg_sampler_placeholder'"
                        )
                    )
                ).scalar_one()
                if int(post) > int(pre):
                    placeholders += 1
                sampled += 1
                await s.commit()
        return {"sampled": sampled, "placeholders": placeholders}
    finally:
        await engine.dispose()


@audit.command("bloomberg-claim-check")
@click.option(
    "--field",
    "field",
    required=True,
    type=click.Choice(list(bloomberg.COMPARISON_FIELDS)),
    help="Field to summarise (e.g. revenue_q-1).",
)
def bloomberg_claim_check_cmd(field: str) -> None:
    """Print a PR-ready summary of the latest closed run for ``field``.

    Per workspace ``CLAUDE.md`` §2 — paste this into the PR description
    when claiming Aslan beats Bloomberg on ``field``. The output is a
    markdown block, safe to include verbatim.
    """
    text_block = asyncio.run(_run_bloomberg_claim_check(field))
    click.echo(text_block)


async def _run_bloomberg_claim_check(field: str) -> str:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as s:
            cc = await bloomberg.claim_check(session=s, field=field)
        return cc.to_pr_text()
    finally:
        await engine.dispose()


# Default render output path. Resolved relative to the workspace root
# (``aslan-event-extractor`` is a sibling repo of ``aslan-core-dq-m0``).
_DEFAULT_RENDER_PATH = "../aslan-event-extractor/docs/comparisons/bloomberg.md"


@audit.command("bloomberg-render")
@click.option(
    "--output",
    "output_path",
    default=_DEFAULT_RENDER_PATH,
    show_default=True,
    help="Output markdown file. Parent directory is created if missing.",
)
def bloomberg_render_cmd(output_path: str) -> None:
    """Render the latest closed run to ``aslan-event-extractor/docs/comparisons/bloomberg.md``.

    The file is overwritten on each invocation. Do NOT edit by hand —
    the next render will clobber any manual changes. The handoff doc
    carries this contract under ``## M4 Bloomberg-comparison rotation``.
    """
    summary = asyncio.run(_run_bloomberg_render(output_path))
    if summary["wrote"]:
        click.echo(
            f"bloomberg-render: wrote {summary['path']} "
            f"({summary['cells']} cells, quarter={summary['quarter']})"
        )
    else:
        click.echo(f"bloomberg-render: no closed run yet — nothing to render at {summary['path']}")


def _write_render_output(output_path: str, body: str) -> str:
    """Sync filesystem write for the bloomberg-render markdown.

    Kept sync so the async render driver can call it without tripping
    the ASYNC240 lint (the dashboard render is a one-shot human-driven
    CLI write, not a hot-path I/O surface — synchronous filesystem
    I/O is appropriate here).
    """
    from pathlib import Path

    path = Path(output_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return str(path)


async def _run_bloomberg_render(output_path: str) -> dict[str, Any]:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as s:
            run = await bloomberg.latest_closed_run(session=s)
        if run is None:
            # Write a stub so the file always exists for downstream
            # tooling, but log clearly that it carries no data yet.
            stub = (
                "# Bloomberg vs Aslan\n\n"
                "_No closed Bloomberg-comparison run yet. Open one via "
                "`aslan-core audit bloomberg-open-quarter` and close it "
                "via `aslan-core audit bloomberg-close-quarter` once the "
                "Bloomberg-side values are entered._\n"
            )
            written = _write_render_output(output_path, stub)
            return {"wrote": False, "path": written, "cells": 0, "quarter": None}
        markdown = bloomberg.render_markdown(run)
        written = _write_render_output(output_path, markdown)
        return {
            "wrote": True,
            "path": written,
            "cells": len(run.cells),
            "quarter": run.quarter,
        }
    finally:
        await engine.dispose()


@audit.command("bloomberg-reminder")
def bloomberg_reminder_cmd() -> None:
    """Slack-ping sidar for any open run with NULL bloomberg cells > 1 week old.

    Cron schedule: weekly Mon 09:00 UTC. Sends one message per stale
    run via the configured Slack sink. If no Slack sink is configured
    the reminder is logged via ``audit.event`` but not delivered (same
    suppression contract as the alert dispatcher).
    """
    summary = asyncio.run(_run_bloomberg_reminder())
    click.echo(
        f"bloomberg-reminder: {summary['stale_runs']} stale run(s), "
        f"{summary['delivered']} reminder(s) delivered, "
        f"{summary['suppressed']} suppressed"
    )


async def _run_bloomberg_reminder(
    *,
    settings: Settings | None = None,
    sinks: dict[str, Sink] | None = None,
) -> dict[str, int]:
    if settings is None:
        settings = Settings()
    if sinks is None:
        sinks = build_default_sinks(settings)
    slack_sink = sinks.get("slack")
    delivered = 0
    suppressed = 0
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as s:
            stale = await bloomberg.stale_open_runs(session=s)
            for run_info in stale:
                payload: dict[str, Any] = {
                    "run_id": str(run_info.run_id),
                    "quarter": run_info.quarter,
                    "opened_at": run_info.opened_at.isoformat(),
                    "null_cells": run_info.null_cells,
                    "reminder": (
                        f"Bloomberg-comparison run {run_info.quarter} has "
                        f"{run_info.null_cells} NULL bloomberg_value cell(s) "
                        f"> 1 week after open. Enter values via /dq/bloomberg."
                    ),
                }
                # Always emit an audit.event so the gap is auditable
                # even when Slack isn't configured.
                await event.emit(
                    session=s,
                    event_type="bloomberg_reminder",
                    emitter="cli:audit-bloomberg-reminder",
                    severity=Severity.WARN,
                    payload=payload,
                )
                if slack_sink is None:
                    suppressed += 1
                    continue
                try:
                    ok = await slack_sink.deliver(
                        payload=payload,
                        severity="warn",
                        rule_name="bloomberg_reminder",
                    )
                except SinkNotConfigured:
                    suppressed += 1
                    continue
                except Exception as exc:  # log + continue past per-run failures
                    click.echo(
                        f"  bloomberg-reminder slack failed for {run_info.quarter}: {exc}",
                        err=True,
                    )
                    continue
                if ok:
                    delivered += 1
            await s.commit()
        return {
            "stale_runs": len(stale),
            "delivered": delivered,
            "suppressed": suppressed,
        }
    finally:
        await engine.dispose()


# ── cross-source-consistency ──────────────────────────────────────


@audit.command("cross-source-consistency")
def cross_source_consistency_cmd() -> None:
    """Run all 6 cross-source consistency rules nightly.

    Spec §7.3. Each rule joins across two source schemas to detect
    inconsistencies single-source validation can't catch (dangling
    entity references, KAP/MKK capital-action correlation, NAV
    reconciliation, EVDS calendar misses, etc.). Per-rule failures
    persist to ``audit.validation_failure`` so they show up in the
    /dq/validation feed alongside in-line validation failures.

    Rules whose required source tables aren't present on this branch
    skip gracefully and emit a single ``xs_rule_skipped`` event; the
    cron loop continues.

    Cron schedule: ``0 2 * * * audit cross-source-consistency``.
    """
    summary = asyncio.run(_run_cross_source_consistency())
    rules_section = ", ".join(f"{k}={v}" for k, v in summary.items())
    click.echo(f"cross-source-consistency complete: {rules_section}")


async def _run_cross_source_consistency() -> dict[str, int]:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as s:
            summary = await cross_source.run_all(session=s)
            await s.commit()
            return summary
    finally:
        await engine.dispose()


# ── regression-detect (v1 + v2) ────────────────────────────────────


@audit.command("regression-detect")
def regression_detect_cmd() -> None:
    """Detect period-over-period regressions + auto-dismiss justified flags.

    v1: for each (entity, metric) in the curated list, compute the
    period-over-period shift on ``ts.canonical_financial`` and emit an
    ``audit.regression_flag`` with ``status='open'`` whenever
    ``|shift_pct| > threshold_pct``.

    v2 (NG5, in-scope per autonomy directive): for each newly-flagged
    row, query ``kap.disclosures`` for material filings on the entity
    in the window ``[detected_at - 7d, detected_at + 1d]``. If a
    filing of category in ``{material_event, capital_action, dividend}``
    exists, auto-dismiss the flag with
    ``review_note='auto-dismissed: justified by KAP filing <id>'`` and
    emit ``regression_auto_dismissed`` for the audit trail.

    Cron schedule: ``0 3 * * * audit regression-detect``.
    """
    summary = asyncio.run(_run_regression_detect())
    click.echo(
        f"regression-detect complete: "
        f"{summary['v1_flagged']} flagged, "
        f"{summary['v2_dismissed']} auto-dismissed (v2), "
        f"{summary['v2_kept_open']} kept open"
    )


async def _run_regression_detect() -> dict[str, int]:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as s:
            result = await regression_detect.detect_and_correlate(session=s)
            await s.commit()
        return {
            "v1_flagged": result.v1_flagged,
            "v2_dismissed": result.v2_dismissed,
            "v2_kept_open": result.v2_kept_open,
        }
    finally:
        await engine.dispose()


# ── scorecard (M6) ─────────────────────────────────────────────────


@audit.command("scorecard")
@click.option(
    "--week-start",
    "week_start",
    default=None,
    help=(
        "ISO-week-Monday (YYYY-MM-DD, UTC) to compute the scorecard for. "
        "Defaults to the most-recent FULLY-completed week."
    ),
)
def scorecard_cmd(week_start: str | None) -> None:
    """Compute the weekly DQ scorecard + emit scorecard_generated event.

    Spec §5.10 + §13. Cron schedule: ``55 23 * * 0 audit scorecard``
    (Sunday 23:55 UTC). Computes ten metrics (recency p95/p99, EVDS
    freshness, coverage snapshots, validation pass rate, spot-check
    completion, Bloomberg wins, regression-open count, cross-source
    clean count), UPSERTs each into ``audit.scorecard_snapshot``, then
    emits ``audit.event(event_type='scorecard_generated')`` with the
    rendered HTML body in the payload — the dispatcher's
    ``weekly_scorecard`` severity rule picks that up and emails sidar.

    Re-runnable for any past week via ``--week-start YYYY-MM-DD`` —
    the UPSERT overwrites prior rows in place.
    """
    if week_start is None:
        target_week = scorecard.latest_week_start()
    else:
        try:
            target_week = date.fromisoformat(week_start)
        except ValueError as exc:
            raise click.UsageError(
                f"--week-start must be YYYY-MM-DD; got {week_start!r}: {exc}"
            ) from exc
        if target_week.isoweekday() != 1:
            raise click.UsageError(
                f"--week-start must be a Monday; {target_week.isoformat()} is "
                f"a {target_week.strftime('%A')}"
            )
    summary = asyncio.run(_run_scorecard(target_week))
    click.echo(
        f"audit scorecard: week_start={summary['week_start']} "
        f"rows_written={summary['rows_written']} "
        f"pass={summary['pass_count']} warn={summary['warn_count']} "
        f"fail={summary['fail_count']}"
    )


async def _run_scorecard(week_start_value: date) -> dict[str, Any]:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as s:
            rows = await scorecard.compute(session=s, week_start=week_start_value)
            written = await scorecard.write(session=s, week_start=week_start_value, rows=rows)
            subject, body_html = scorecard.render_email(week_start=week_start_value, rows=rows)
            body_text = scorecard.render_text_fallback(week_start=week_start_value, rows=rows)
            await event.emit(
                session=s,
                event_type="scorecard_generated",
                emitter="cli:audit-scorecard",
                severity=Severity.INFO,
                payload={
                    "week_start": week_start_value.isoformat(),
                    "rows_written": written,
                    "pass_count": sum(1 for r in rows if r.status == "pass"),
                    "warn_count": sum(1 for r in rows if r.status == "warn"),
                    "fail_count": sum(1 for r in rows if r.status == "fail"),
                    "subject": subject,
                    "body_html": body_html,
                    "body_text": body_text,
                },
            )
            await s.commit()
        return {
            "week_start": week_start_value.isoformat(),
            "rows_written": written,
            "pass_count": sum(1 for r in rows if r.status == "pass"),
            "warn_count": sum(1 for r in rows if r.status == "warn"),
            "fail_count": sum(1 for r in rows if r.status == "fail"),
        }
    finally:
        await engine.dispose()


__all__ = ["SOURCES", "audit"]
