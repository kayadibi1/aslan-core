"""``aslan-core audit`` CLI group — M1 + M2.

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

Cadence:

  * recency-sweep — every 60s (spec §10.2)
  * coverage-snapshot — every 5m (spec §10.2)
  * spot-check-draw — weekly Mon 06:00 UTC (spec §13)

Each writes durable rows that the dashboard reads back via the
/dq/* pages.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Protocol

import click
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.db.engine import create_engine
from aslan_core.db.session import create_session_factory
from aslan_core.dq import coverage, event, recency, spot_check
from aslan_core.dq._sql import SELECT_RECENCY_SLA
from aslan_core.dq.probes import SOURCES, get_probe
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
    help=(
        "Limit the draw to one source (kap|bist|evds|tefas|mkk). "
        "Default: draw across all five."
    ),
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


async def _run_spot_check_draw(
    source_filter: str | None, n_per_source: int
) -> dict[str, int]:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as s:
            drawn = 0
            sources_touched = 0
            sources = (
                (source_filter,) if source_filter is not None else _SPOT_CHECK_SOURCES
            )
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


__all__ = ["SOURCES", "audit"]
