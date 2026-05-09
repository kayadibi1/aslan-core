"""SQL fragments used by per-source probes.

Each probe consults `information_schema.tables` to verify its primary
table is present before attempting `MAX(...)`. Centralized here so a
future schema rename (e.g. `kap.disclosures` → `kap.disclosure`)
lights up exactly one place.
"""

from __future__ import annotations

from sqlalchemy import text

# ── Table-presence checks ──────────────────────────────────────────


TABLE_EXISTS = text(
    "SELECT EXISTS ("
    "  SELECT 1 FROM information_schema.tables "
    "  WHERE table_schema = :schema AND table_name = :table"
    ") AS present"
)


# ── Per-source MAX queries ─────────────────────────────────────────


MAX_KAP_PUBLISHED_AT = text("SELECT MAX(published_at) AS db_latest_at FROM kap.disclosures")


MAX_EVDS_OBSERVATION_DATE = text(
    "SELECT MAX(observation_date)::timestamptz AS db_latest_at FROM evds.observation"
)


MAX_BIST_TRADE_DATE = text(
    "SELECT MAX(trade_date)::timestamptz AS db_latest_at FROM bist.daily_ohlcv"
)


MAX_TEFAS_SNAPSHOT_DATE = text(
    "SELECT MAX(snapshot_date)::timestamptz AS db_latest_at FROM tefas.fund_holding"
)


MAX_MKK_EVENT_AT = text("SELECT MAX(event_at) AS db_latest_at FROM mkk.capital_action")


# ── EVDS calendar lookup ───────────────────────────────────────────


# Most recent expected_at whose grace window has elapsed (i.e.
# "what should already be in the DB"). Returns NULL if the calendar
# is empty.
LATEST_EVDS_DUE = text(
    "SELECT MAX(expected_at) AS expected_at "
    "FROM audit.evds_release_calendar "
    "WHERE expected_at + (grace_seconds * INTERVAL '1 second') < now()"
)
