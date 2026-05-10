"""dq.cross_source — nightly cross-source consistency rules.

Spec §7.3. Six rules that join across two or more source schemas to
detect inconsistencies that single-source validation can't catch:

  1. ``xs_tefas_holding_dangling_entity`` — ``tefas.fund_holding``
     references an ``entity_id`` not present in ``ref.entity``.

  2. ``xs_bist_ticker_kap_issuer`` — a BIST ticker on
     ``bist.security`` does not resolve to any KAP issuer via
     ``ref.identifier(namespace='bist_ticker')``.

  3. ``xs_mkk_kap_capital_action_corr`` — ``mkk.capital_action`` row
     without a corresponding ``kap.disclosures`` filing of category
     ``'capital_action'`` for that entity in ±3 trading days.

  4. ``xs_tefas_nav_holdings_recon`` — for each ``tefas.fund_nav`` row,
     the reconstructed NAV from
     ``Sigma tefas.fund_holding x bist.daily_ohlcv.close`` differs from
     the published NAV by more than 2%.

  5. ``xs_evds_observation_calendar`` — an ``audit.evds_release_calendar``
     row whose ``expected_at + grace_seconds`` has elapsed and no
     corresponding ``evds.observation`` row exists.

  6. ``xs_kap_filing_count_recon`` — per-day ``kap.disclosures`` count
     diverges from the KAP API's per-day count. v1 placeholder: HTTP
     query to KAP is M5.1; v1 self-compares with a fudge factor for
     the test fixture path. Real-world deployment lights up the
     ``kap_api_count_unimplemented`` event so operators see the gap.

Each rule is a coroutine returning ``list[ValidationFailure]`` (the
shape used by ``dq.validation.check`` so callers compose). Each rule
also persists every failure to ``audit.validation_failure`` for
durable audit. When the source tables required by a rule are absent
from this branch / environment, the rule emits a single
``xs_rule_skipped`` ``audit.event`` and returns ``[]`` rather than
raising — the cron loop continues with the next rule.

The cron entry point ``cli.dq.cross_source_consistency_cmd`` runs
all six in sequence and aggregates the per-rule counts for the
operational summary line.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.config import Settings
from aslan_core.dq import event as dq_event
from aslan_core.dq._sql import INSERT_VALIDATION_FAILURE
from aslan_core.dq.probes._http import proxy_aware_client
from aslan_core.dq.types import Severity, ValidationFailure

_log = structlog.get_logger(__name__)


# ── Helpers ──────────────────────────────────────────────────────


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


async def _all_tables_present(
    session: AsyncSession, required: tuple[tuple[str, str], ...]
) -> tuple[bool, list[str]]:
    """Return (all_present, missing_list). ``missing_list`` is a list of
    ``"schema.table"`` strings for the missing dependencies."""
    missing: list[str] = []
    for schema, table in required:
        if not await _table_present(session, schema, table):
            missing.append(f"{schema}.{table}")
    return (not missing), missing


async def _emit_skip(
    *,
    session: AsyncSession,
    rule_name: str,
    reason: str,
    missing: list[str],
) -> None:
    await dq_event.emit(
        session=session,
        event_type="xs_rule_skipped",
        emitter=f"dq.cross_source.{rule_name}",
        severity=Severity.INFO,
        payload={
            "rule_name": rule_name,
            "reason": reason,
            "missing": missing,
        },
    )


async def _persist_failure(
    *,
    session: AsyncSession,
    source: str,
    rule_name: str,
    severity: Severity,
    record_table: str,
    record_pk: dict[str, Any],
    detail: dict[str, Any],
    detected_at: datetime,
) -> ValidationFailure:
    """INSERT one ``audit.validation_failure`` row + return the dataclass."""
    insert_row = (
        await session.execute(
            INSERT_VALIDATION_FAILURE,
            {
                "source": source,
                "rule_name": rule_name,
                "severity": severity.value,
                "record_table": record_table,
                "record_pk": json.dumps(record_pk, default=str),
                "detected_at": detected_at,
                "detail": json.dumps(detail, default=str),
            },
        )
    ).one()
    return ValidationFailure(
        source=source,
        rule_name=rule_name,
        severity=severity,
        record_table=record_table,
        record_pk=record_pk,
        detail=detail,
        detected_at=detected_at.isoformat(),
        failure_id=int(insert_row.failure_id),
    )


# ── Rule 1: xs_tefas_holding_dangling_entity ─────────────────────


_RULE_1 = "xs_tefas_holding_dangling_entity"

_SQL_DANGLING_HOLDING_ENTITIES = text(
    "SELECT DISTINCT h.entity_id, h.fund_id, h.snapshot_date "
    "FROM tefas.fund_holding h "
    "LEFT JOIN ref.entity e ON e.entity_id = h.entity_id "
    "WHERE e.entity_id IS NULL "
    "ORDER BY h.snapshot_date DESC, h.entity_id "
    "LIMIT :max_failures"
)


async def xs_tefas_holding_dangling_entity(
    *, session: AsyncSession, max_failures: int = 500
) -> list[ValidationFailure]:
    """``tefas.fund_holding`` rows whose ``entity_id`` is missing from
    ``ref.entity``. Fires once per (fund_id, entity_id, snapshot_date)
    triple."""
    ok, missing = await _all_tables_present(session, (("tefas", "fund_holding"), ("ref", "entity")))
    if not ok:
        await _emit_skip(
            session=session,
            rule_name=_RULE_1,
            reason="required tables not present",
            missing=missing,
        )
        return []
    rows = (
        await session.execute(_SQL_DANGLING_HOLDING_ENTITIES, {"max_failures": max_failures})
    ).all()
    detected_at = datetime.now(UTC)
    out: list[ValidationFailure] = []
    for r in rows:
        pk: dict[str, Any] = {
            "fund_id": str(r.fund_id),
            "entity_id": str(r.entity_id),
            "snapshot_date": r.snapshot_date.isoformat() if r.snapshot_date is not None else None,
        }
        out.append(
            await _persist_failure(
                session=session,
                source="tefas",
                rule_name=_RULE_1,
                severity=Severity.WARN,
                record_table="tefas.fund_holding",
                record_pk=pk,
                detail={"reason": "holding entity_id not in ref.entity"},
                detected_at=detected_at,
            )
        )
    return out


# ── Rule 2: xs_bist_ticker_kap_issuer ────────────────────────────


_RULE_2 = "xs_bist_ticker_kap_issuer"

# A bist.security row is "tied" to a KAP issuer when there's a
# `ref.identifier(namespace='bist_ticker', value=ticker)` valid today.
# Spec §7.3: flag any bist.security ticker not present.
_SQL_BIST_TICKERS_WITHOUT_IDENTIFIER = text(
    "SELECT s.security_id, s.ticker "
    "FROM bist.security s "
    "LEFT JOIN ref.identifier i "
    "  ON i.namespace = 'bist_ticker' "
    " AND i.value = s.ticker "
    " AND i.valid_from <= current_date "
    " AND i.valid_to > current_date "
    "WHERE i.identifier_id IS NULL "
    "ORDER BY s.ticker "
    "LIMIT :max_failures"
)


async def xs_bist_ticker_kap_issuer(
    *, session: AsyncSession, max_failures: int = 500
) -> list[ValidationFailure]:
    """``bist.security`` rows whose ticker is not registered in any
    ``ref.identifier(namespace='bist_ticker')`` for today. The
    identifier row is the canonical resolver from ticker → entity_id;
    a missing identifier means the ticker doesn't resolve to a KAP
    issuer, which breaks every cross-source join keyed on entity."""
    ok, missing = await _all_tables_present(session, (("bist", "security"), ("ref", "identifier")))
    if not ok:
        await _emit_skip(
            session=session,
            rule_name=_RULE_2,
            reason="required tables not present",
            missing=missing,
        )
        return []
    rows = (
        await session.execute(_SQL_BIST_TICKERS_WITHOUT_IDENTIFIER, {"max_failures": max_failures})
    ).all()
    detected_at = datetime.now(UTC)
    out: list[ValidationFailure] = []
    for r in rows:
        pk: dict[str, Any] = {"security_id": str(r.security_id), "ticker": r.ticker}
        out.append(
            await _persist_failure(
                session=session,
                source="bist",
                rule_name=_RULE_2,
                severity=Severity.WARN,
                record_table="bist.security",
                record_pk=pk,
                detail={"reason": "ticker not present in ref.identifier(bist_ticker)"},
                detected_at=detected_at,
            )
        )
    return out


# ── Rule 3: xs_mkk_kap_capital_action_corr ───────────────────────


_RULE_3 = "xs_mkk_kap_capital_action_corr"

# MKK capital_action rows are the authoritative event of record. A KAP
# filing of category 'capital_action' should land within ±3 trading
# days of the MKK ``event_at``. v1 uses calendar days for the window
# (trading-day calendar lookup is M5.1) — a 5-calendar-day window
# (Mon-Fri ±3 trading days, allowing for one weekend) approximates
# the trading-day rule and is documented in the spec.
_SQL_MKK_WITHOUT_KAP_FILING = text(
    "SELECT m.entity_id, m.event_at "
    "FROM mkk.capital_action m "
    "WHERE NOT EXISTS ( "
    "  SELECT 1 FROM kap.disclosures d "
    "  WHERE d.entity_id = m.entity_id "
    "    AND d.category = 'capital_action' "
    "    AND d.published_at >= m.event_at - INTERVAL '5 days' "
    "    AND d.published_at <= m.event_at + INTERVAL '5 days' "
    ") "
    "ORDER BY m.event_at DESC "
    "LIMIT :max_failures"
)


async def xs_mkk_kap_capital_action_corr(
    *, session: AsyncSession, max_failures: int = 500
) -> list[ValidationFailure]:
    """``mkk.capital_action`` rows without a corresponding ``kap.disclosures``
    filing of category ``'capital_action'`` for the same entity within
    ±3 trading days (approximated as ±5 calendar days for v1)."""
    ok, missing = await _all_tables_present(
        session, (("mkk", "capital_action"), ("kap", "disclosures"))
    )
    if not ok:
        await _emit_skip(
            session=session,
            rule_name=_RULE_3,
            reason="required tables not present",
            missing=missing,
        )
        return []
    rows = (
        await session.execute(_SQL_MKK_WITHOUT_KAP_FILING, {"max_failures": max_failures})
    ).all()
    detected_at = datetime.now(UTC)
    out: list[ValidationFailure] = []
    for r in rows:
        pk: dict[str, Any] = {
            "entity_id": str(r.entity_id),
            "event_at": r.event_at.isoformat() if r.event_at is not None else None,
        }
        out.append(
            await _persist_failure(
                session=session,
                source="mkk",
                rule_name=_RULE_3,
                severity=Severity.WARN,
                record_table="mkk.capital_action",
                record_pk=pk,
                detail={
                    "reason": (
                        "no kap.disclosures(category='capital_action') "
                        "in ±5 calendar days for this entity"
                    ),
                    "window_days": 5,
                },
                detected_at=detected_at,
            )
        )
    return out


# ── Rule 4: xs_tefas_nav_holdings_recon ──────────────────────────


_RULE_4 = "xs_tefas_nav_holdings_recon"
_NAV_DIFF_THRESHOLD_PCT: Decimal = Decimal("2.0")

# For each tefas.fund_nav row, reconstruct NAV from holdings. The
# reconstructed NAV is the sum across all holdings on the same
# snapshot_date of (units * close). If the published NAV differs by
# more than 2%, flag.
_SQL_TEFAS_NAV_RECON = text(
    "WITH recon AS ( "
    "  SELECT n.fund_id, n.snapshot_date, n.nav AS published_nav, "
    "    SUM(h.units * o.close) AS reconstructed_nav "
    "  FROM tefas.fund_nav n "
    "  JOIN tefas.fund_holding h "
    "    ON h.fund_id = n.fund_id AND h.snapshot_date = n.snapshot_date "
    "  JOIN bist.daily_ohlcv o "
    "    ON o.security_id = h.security_id AND o.trade_date = n.snapshot_date "
    "  GROUP BY n.fund_id, n.snapshot_date, n.nav "
    ") "
    "SELECT fund_id, snapshot_date, published_nav, reconstructed_nav, "
    "  CASE WHEN published_nav = 0 THEN NULL "
    "       ELSE 100.0 * abs(reconstructed_nav - published_nav) / abs(published_nav) "
    "  END AS diff_pct "
    "FROM recon "
    "WHERE published_nav <> 0 "
    "  AND 100.0 * abs(reconstructed_nav - published_nav) / abs(published_nav) > :threshold "
    "ORDER BY snapshot_date DESC "
    "LIMIT :max_failures"
)


async def xs_tefas_nav_holdings_recon(
    *,
    session: AsyncSession,
    threshold_pct: Decimal = _NAV_DIFF_THRESHOLD_PCT,
    max_failures: int = 500,
) -> list[ValidationFailure]:
    """For each ``tefas.fund_nav`` row, reconstruct NAV from holdings x
    daily-OHLCV close and flag rows where the difference exceeds
    ``threshold_pct`` (default 2%)."""
    ok, missing = await _all_tables_present(
        session,
        (
            ("tefas", "fund_nav"),
            ("tefas", "fund_holding"),
            ("bist", "daily_ohlcv"),
        ),
    )
    if not ok:
        await _emit_skip(
            session=session,
            rule_name=_RULE_4,
            reason="required tables not present",
            missing=missing,
        )
        return []
    rows = (
        await session.execute(
            _SQL_TEFAS_NAV_RECON,
            {"threshold": threshold_pct, "max_failures": max_failures},
        )
    ).all()
    detected_at = datetime.now(UTC)
    out: list[ValidationFailure] = []
    for r in rows:
        pk: dict[str, Any] = {
            "fund_id": str(r.fund_id),
            "snapshot_date": r.snapshot_date.isoformat() if r.snapshot_date is not None else None,
        }
        detail: dict[str, Any] = {
            "published_nav": str(r.published_nav),
            "reconstructed_nav": str(r.reconstructed_nav),
            "diff_pct": str(r.diff_pct),
            "threshold_pct": str(threshold_pct),
            "reason": "NAV reconstruction diverges from published NAV",
        }
        out.append(
            await _persist_failure(
                session=session,
                source="tefas",
                rule_name=_RULE_4,
                severity=Severity.ERROR,
                record_table="tefas.fund_nav",
                record_pk=pk,
                detail=detail,
                detected_at=detected_at,
            )
        )
    return out


# ── Rule 5: xs_evds_observation_calendar ─────────────────────────


_RULE_5 = "xs_evds_observation_calendar"

_SQL_EVDS_CALENDAR_MISS = text(
    "SELECT c.series_code, c.expected_at, c.grace_seconds "
    "FROM audit.evds_release_calendar c "
    "WHERE c.expected_at + (c.grace_seconds * INTERVAL '1 second') < now() "
    "  AND NOT EXISTS ( "
    "    SELECT 1 FROM evds.observation o "
    "    WHERE o.series_code = c.series_code "
    "      AND o.observation_date >= c.expected_at::date - 2 "
    "      AND o.observation_date <= c.expected_at::date + 2 "
    "  ) "
    "ORDER BY c.expected_at DESC "
    "LIMIT :max_failures"
)


async def xs_evds_observation_calendar(
    *, session: AsyncSession, max_failures: int = 500
) -> list[ValidationFailure]:
    """For each ``audit.evds_release_calendar`` row whose
    ``expected_at + grace_seconds`` is past and no corresponding
    ``evds.observation`` exists, emit a failure. Window for the
    observation lookup is ±2 days around the expected release date."""
    if not await _table_present(session, "audit", "evds_release_calendar"):
        await _emit_skip(
            session=session,
            rule_name=_RULE_5,
            reason="audit.evds_release_calendar not present",
            missing=["audit.evds_release_calendar"],
        )
        return []
    if not await _table_present(session, "evds", "observation"):
        await _emit_skip(
            session=session,
            rule_name=_RULE_5,
            reason="evds.observation not present",
            missing=["evds.observation"],
        )
        return []
    rows = (await session.execute(_SQL_EVDS_CALENDAR_MISS, {"max_failures": max_failures})).all()
    detected_at = datetime.now(UTC)
    out: list[ValidationFailure] = []
    for r in rows:
        pk: dict[str, Any] = {
            "series_code": r.series_code,
            "expected_at": r.expected_at.isoformat() if r.expected_at is not None else None,
        }
        detail: dict[str, Any] = {
            "reason": "expected EVDS release is past grace window without an observation",
            "grace_seconds": int(r.grace_seconds),
        }
        out.append(
            await _persist_failure(
                session=session,
                source="evds",
                rule_name=_RULE_5,
                severity=Severity.ERROR,
                record_table="audit.evds_release_calendar",
                record_pk=pk,
                detail=detail,
                detected_at=detected_at,
            )
        )
    return out


# ── Rule 6: xs_kap_filing_count_recon ────────────────────────────


_RULE_6 = "xs_kap_filing_count_recon"
_KAP_RECON_FUDGE_PCT: Decimal = Decimal("0.0")

# Two implementations:
#
#   * Real-upstream mode (Settings.dq_kap_listing_url is set): GET
#     the KAP listing endpoint via the proxy-aware httpx helper
#     (KAP_PROXY_URL rotating pool when present), bucket by
#     publishDate.date(), compare each day's upstream count to the
#     in-DB count from kap.disclosures. Any non-zero diff fires a
#     failure (severity=info — count drift is typically real, not a
#     bug, and we want to surface it for human triage rather than
#     page).
#
#   * Self-compare fallback (Settings.dq_kap_listing_url is None):
#     v1 trailing-7-day mean self-compare. Emits the
#     ``kap_api_count_unimplemented`` marker event so the dashboard
#     surfaces the gap; this is the original M5.1 placeholder, kept
#     working until production wires the listing URL.
_SQL_KAP_FILING_PER_DAY_TRAILING_7D = text(
    "SELECT date_trunc('day', published_at)::date AS pub_day, "
    "  count(*)::int AS daily_count "
    "FROM kap.disclosures "
    "WHERE published_at >= current_date - INTERVAL '7 days' "
    "  AND published_at < current_date "
    "GROUP BY pub_day "
    "ORDER BY pub_day"
)


def _bucket_kap_listing_by_day(payload: Any) -> dict[date, int]:
    """Group a KAP listing JSON payload by publishDate.date() -> count.

    Accepts the same shapes as the recency probe parser
    (``aslan_core.dq.probes.kap._parse_kap_listing``): top-level list
    or ``{"data": [...]}`` envelope. Rows without a parseable
    publishDate are silently skipped — a real upstream that returns
    a row without a date is a structural change worth surfacing in
    the per-rule failure detail rather than crashing the recon.
    """
    from aslan_core.dq.probes.kap import _parse_kap_ts

    rows: Any
    if isinstance(payload, dict):
        rows = payload.get("data") if "data" in payload else payload
    else:
        rows = payload
    if not isinstance(rows, list):
        return {}
    counts: dict[date, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = row.get("publishDate") or row.get("published_at")
        if not isinstance(raw, str):
            continue
        ts = _parse_kap_ts(raw)
        if ts is None:
            continue
        d = ts.date()
        counts[d] = counts.get(d, 0) + 1
    return counts


async def _xs_kap_recon_self_compare(
    *,
    session: AsyncSession,
    fudge_pct: Decimal,
    max_failures: int,
    detected_at: datetime,
) -> list[ValidationFailure]:
    """Trailing-7d-mean self-compare fallback (when no listing URL set).

    Emits the ``kap_api_count_unimplemented`` marker event so the
    dashboard surfaces the gap to operators.
    """
    await dq_event.emit(
        session=session,
        event_type="kap_api_count_unimplemented",
        emitter=f"dq.cross_source.{_RULE_6}",
        severity=Severity.INFO,
        payload={
            "reason": "no DQ_KAP_LISTING_URL configured; self-compare fallback",
            "fudge_pct": str(fudge_pct),
        },
    )
    rows = (await session.execute(_SQL_KAP_FILING_PER_DAY_TRAILING_7D)).all()
    if len(rows) < 2:
        return []
    counts = [int(r.daily_count) for r in rows]
    mean = Decimal(sum(counts)) / Decimal(len(counts))
    out: list[ValidationFailure] = []
    for r in rows:
        if mean == 0:
            continue
        diff_pct = abs(Decimal(int(r.daily_count)) - mean) / mean * Decimal(100)
        if diff_pct <= fudge_pct:
            continue
        if len(out) >= max_failures:
            break
        pk: dict[str, Any] = {"pub_day": r.pub_day.isoformat()}
        detail: dict[str, Any] = {
            "mode": "self_compare",
            "daily_count": int(r.daily_count),
            "trailing_7d_mean": str(mean),
            "diff_pct": str(diff_pct),
            "fudge_pct": str(fudge_pct),
            "reason": (
                "kap.disclosures daily count diverges from trailing-7d "
                "mean (self-compare fallback; set DQ_KAP_LISTING_URL "
                "for upstream recon)"
            ),
        }
        out.append(
            await _persist_failure(
                session=session,
                source="kap",
                rule_name=_RULE_6,
                severity=Severity.INFO,
                record_table="kap.disclosures",
                record_pk=pk,
                detail=detail,
                detected_at=detected_at,
            )
        )
    return out


async def _xs_kap_recon_http(
    *,
    session: AsyncSession,
    listing_url: str,
    max_failures: int,
    detected_at: datetime,
) -> list[ValidationFailure]:
    """Real-upstream mode: GET listing URL, compare per-day to DB.

    On HTTP / parse error: emit ``kap_listing_recon_error`` event and
    fall back to self-compare so the cron makes progress.
    """
    try:
        async with proxy_aware_client() as (client, proxy_label):
            resp = await client.get(listing_url)
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
        await dq_event.emit(
            session=session,
            event_type="kap_listing_recon_error",
            emitter=f"dq.cross_source.{_RULE_6}",
            severity=Severity.WARN,
            payload={
                "listing_url": listing_url,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "fallback": "self_compare",
            },
        )
        return await _xs_kap_recon_self_compare(
            session=session,
            fudge_pct=_KAP_RECON_FUDGE_PCT,
            max_failures=max_failures,
            detected_at=detected_at,
        )

    upstream_by_day = _bucket_kap_listing_by_day(payload)
    db_rows = (await session.execute(_SQL_KAP_FILING_PER_DAY_TRAILING_7D)).all()
    db_by_day: dict[date, int] = {r.pub_day: int(r.daily_count) for r in db_rows}

    out: list[ValidationFailure] = []
    # Union of days seen on either side. A day present upstream but
    # not in DB is the "we missed a filing" case; a day in DB but not
    # upstream is "upstream pruned but we still have it" (also worth
    # surfacing).
    all_days = sorted(set(upstream_by_day.keys()) | set(db_by_day.keys()))
    for d in all_days:
        upstream_n = upstream_by_day.get(d, 0)
        db_n = db_by_day.get(d, 0)
        if upstream_n == db_n:
            continue
        if len(out) >= max_failures:
            break
        pk: dict[str, Any] = {"pub_day": d.isoformat()}
        detail: dict[str, Any] = {
            "mode": "http_recon",
            "listing_url": listing_url,
            "proxy": proxy_label,
            "upstream_count": upstream_n,
            "db_count": db_n,
            "diff": upstream_n - db_n,
            "reason": (
                "kap.disclosures daily count differs from KAP listing "
                f"endpoint (upstream={upstream_n}, db={db_n})"
            ),
        }
        out.append(
            await _persist_failure(
                session=session,
                source="kap",
                rule_name=_RULE_6,
                severity=Severity.INFO,
                record_table="kap.disclosures",
                record_pk=pk,
                detail=detail,
                detected_at=detected_at,
            )
        )
    return out


async def xs_kap_filing_count_recon(
    *,
    session: AsyncSession,
    fudge_pct: Decimal = _KAP_RECON_FUDGE_PCT,
    max_failures: int = 500,
    settings: Settings | None = None,
) -> list[ValidationFailure]:
    """KAP per-day count vs upstream KAP API per-day count.

    Real-upstream mode (``Settings.dq_kap_listing_url`` set): HTTP
    GET via proxy-aware httpx, compare per-day counts. Any non-zero
    diff fires a row.

    Self-compare fallback (``Settings.dq_kap_listing_url`` unset):
    trailing-7-day mean self-compare; emits
    ``kap_api_count_unimplemented`` marker so operators see the gap.
    """
    ok, missing = await _all_tables_present(session, (("kap", "disclosures"),))
    if not ok:
        await _emit_skip(
            session=session,
            rule_name=_RULE_6,
            reason="required tables not present",
            missing=missing,
        )
        return []
    s = settings if settings is not None else Settings()
    detected_at = datetime.now(UTC)
    if s.dq_kap_listing_url is None:
        return await _xs_kap_recon_self_compare(
            session=session,
            fudge_pct=fudge_pct,
            max_failures=max_failures,
            detected_at=detected_at,
        )
    return await _xs_kap_recon_http(
        session=session,
        listing_url=s.dq_kap_listing_url,
        max_failures=max_failures,
        detected_at=detected_at,
    )


# ── All-rules driver ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CrossSourceRule:
    name: str
    runner: Callable[..., Awaitable[list[ValidationFailure]]]


ALL_RULES: tuple[CrossSourceRule, ...] = (
    CrossSourceRule(_RULE_1, xs_tefas_holding_dangling_entity),
    CrossSourceRule(_RULE_2, xs_bist_ticker_kap_issuer),
    CrossSourceRule(_RULE_3, xs_mkk_kap_capital_action_corr),
    CrossSourceRule(_RULE_4, xs_tefas_nav_holdings_recon),
    CrossSourceRule(_RULE_5, xs_evds_observation_calendar),
    CrossSourceRule(_RULE_6, xs_kap_filing_count_recon),
)


async def run_all(*, session: AsyncSession) -> dict[str, int]:
    """Run every rule sequentially, returning a {rule_name: failure_count}
    dict. Per-rule transactional commit is the caller's responsibility
    (the cron driver commits once per rule)."""
    summary: dict[str, int] = {}
    for rule in ALL_RULES:
        results = await rule.runner(session=session)
        summary[rule.name] = len(results)
    return summary


__all__ = [
    "ALL_RULES",
    "CrossSourceRule",
    "run_all",
    "xs_bist_ticker_kap_issuer",
    "xs_evds_observation_calendar",
    "xs_kap_filing_count_recon",
    "xs_mkk_kap_capital_action_corr",
    "xs_tefas_holding_dangling_entity",
    "xs_tefas_nav_holdings_recon",
]
