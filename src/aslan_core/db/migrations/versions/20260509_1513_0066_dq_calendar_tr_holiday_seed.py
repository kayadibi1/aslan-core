"""dq Batch 2 follow-up: ref.calendar_tr table + TR holiday seed (2026-2027).

Revision ID: 0066
Revises: 0065
Create Date: 2026-05-09 15:13:00

The BIST recency probe (``aslan_core.dq.probes.bist``) uses
``ref.calendar_tr`` to compute the most-recent TR trading day,
correctly excluding public holidays (Bayram weeks, Republic Day, …).
Without the table the probe falls back to a naive Mon-Fri rule that
mis-reports "stale" on TR holidays.

This migration:

  1. Creates ``ref.calendar_tr`` if absent. The detailed schema for
     the BIST puller's M2 work may extend this table later (session
     hours, half-days, etc.); we ship the minimal columns the dq
     probe needs and let downstream additions ALTER the table.
     ``CREATE TABLE IF NOT EXISTS`` makes the migration idempotent
     against a future BIST puller migration that creates a richer
     version first.

  2. Seeds TR public holidays observed by BIST (no trading) for 2026
     + 2027. ``ON CONFLICT (trade_date) DO NOTHING`` so the seed
     does not corrupt rows already present from a richer prior
     migration.

Religious holiday dates (Eid al-Fitr / Eid al-Adha) are computed
from the lunar calendar and are approximate — the canonical TR
public holiday calendar is published by the government a few months
in advance. Production should ALTER these via a new migration when
the official dates differ. The dq probe only needs "is this a
trading day" so a small drift in the religious-holiday window is
acceptable for batch-2 ship-fitness.

Privilege boundary: the table is created in the ``ref`` schema.
GRANTs follow the same pattern as the other ``ref.*`` tables: USAGE
on the schema for the existing roles, SELECT on the table for read
roles. We do NOT grant write to runtime roles — calendar
modifications ship as new migrations.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from alembic import op
from sqlalchemy import text

revision: str = "0066"
down_revision: str | Sequence[str] | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ── TR public holidays observed by BIST (no trading) ────────────────
#
# Religious holidays are approximate — government-published official
# dates may differ by a day. ALTER via a new migration when the
# official calendar lands.
_HOLIDAYS_2026: tuple[tuple[str, str], ...] = (
    ("2026-01-01", "Yılbaşı (New Year)"),  # noqa: RUF001
    ("2026-02-20", "Ramazan Bayramı (Eid al-Fitr) — Day 1"),  # noqa: RUF001
    ("2026-02-21", "Ramazan Bayramı (Eid al-Fitr) — Day 2"),  # noqa: RUF001
    ("2026-02-22", "Ramazan Bayramı (Eid al-Fitr) — Day 3"),  # noqa: RUF001
    ("2026-04-23", "Ulusal Egemenlik ve Çocuk Bayramı (National Sovereignty + Children's Day)"),  # noqa: RUF001
    ("2026-04-29", "Kurban Bayramı (Eid al-Adha) — Eve"),  # noqa: RUF001
    ("2026-04-30", "Kurban Bayramı (Eid al-Adha) — Day 1"),  # noqa: RUF001
    ("2026-05-01", "Labour Day / Kurban Bayramı — Day 2"),
    ("2026-05-02", "Kurban Bayramı (Eid al-Adha) — Day 3"),  # noqa: RUF001
    ("2026-05-19", "Atatürk'ü Anma, Gençlik ve Spor Bayramı (Atatürk Memorial Day)"),  # noqa: RUF001
    ("2026-07-15", "Demokrasi ve Milli Birlik Günü (Democracy Day)"),  # noqa: RUF001
    ("2026-08-30", "Zafer Bayramı (Victory Day)"),  # noqa: RUF001
    ("2026-10-29", "Cumhuriyet Bayramı (Republic Day)"),  # noqa: RUF001
)

_HOLIDAYS_2027: tuple[tuple[str, str], ...] = (
    ("2027-01-01", "Yılbaşı (New Year)"),  # noqa: RUF001
    ("2027-02-09", "Ramazan Bayramı (Eid al-Fitr) — Day 1"),  # noqa: RUF001
    ("2027-02-10", "Ramazan Bayramı (Eid al-Fitr) — Day 2"),  # noqa: RUF001
    ("2027-02-11", "Ramazan Bayramı (Eid al-Fitr) — Day 3"),  # noqa: RUF001
    ("2027-04-18", "Kurban Bayramı (Eid al-Adha) — Eve"),  # noqa: RUF001
    ("2027-04-19", "Kurban Bayramı (Eid al-Adha) — Day 1"),  # noqa: RUF001
    ("2027-04-20", "Kurban Bayramı (Eid al-Adha) — Day 2"),  # noqa: RUF001
    ("2027-04-21", "Kurban Bayramı (Eid al-Adha) — Day 3"),  # noqa: RUF001
    ("2027-04-23", "Ulusal Egemenlik ve Çocuk Bayramı (National Sovereignty)"),  # noqa: RUF001
    ("2027-05-01", "Labour Day"),
    ("2027-05-19", "Atatürk'ü Anma, Gençlik ve Spor Bayramı (Atatürk Memorial Day)"),  # noqa: RUF001
    ("2027-07-15", "Demokrasi ve Milli Birlik Günü (Democracy Day)"),  # noqa: RUF001
    ("2027-08-30", "Zafer Bayramı (Victory Day)"),  # noqa: RUF001
    ("2027-10-29", "Cumhuriyet Bayramı (Republic Day)"),  # noqa: RUF001
)


_INSERT_HOLIDAY = text(
    "INSERT INTO ref.calendar_tr (trade_date, is_trading_day, holiday_name) "
    "VALUES (:trade_date, false, :holiday_name) "
    "ON CONFLICT (trade_date) DO NOTHING"
)


def _parse_iso_date(s: str) -> date:
    """Parse ``YYYY-MM-DD`` into a ``date`` so asyncpg's DATE codec
    accepts the bound parameter directly. Inlined rather than using
    ``date.fromisoformat`` for explicit clarity."""
    yyyy, mm, dd = s.split("-")
    return date(int(yyyy), int(mm), int(dd))


def upgrade() -> None:
    # Schema, then table, then index, then GRANTs, then seed.
    op.execute("CREATE SCHEMA IF NOT EXISTS ref")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ref.calendar_tr (
            trade_date     DATE PRIMARY KEY,
            is_trading_day BOOLEAN NOT NULL,
            holiday_name   TEXT,
            notes          TEXT,
            recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS calendar_tr_trading "
        "ON ref.calendar_tr(trade_date) WHERE is_trading_day"
    )

    # GRANTs — idempotent (PostgreSQL GRANT is a no-op if the
    # privilege already exists). The roles below are the canonical
    # read-side roles seeded by earlier migrations.
    for role in ("audit_reader", "aslan_dashboard"):
        op.execute(f"GRANT USAGE ON SCHEMA ref TO {role}")
        op.execute(f"GRANT SELECT ON ref.calendar_tr TO {role}")

    bind = op.get_bind()
    for trade_date, holiday_name in (*_HOLIDAYS_2026, *_HOLIDAYS_2027):
        bind.execute(
            _INSERT_HOLIDAY,
            {"trade_date": _parse_iso_date(trade_date), "holiday_name": holiday_name},
        )


def downgrade() -> None:
    # Defensive: only drop the holiday rows we seeded; do NOT drop
    # the table itself because a future BIST puller migration may
    # have populated it with trading-day rows we should not destroy.
    bind = op.get_bind()
    for trade_date, _ in (*_HOLIDAYS_2026, *_HOLIDAYS_2027):
        bind.execute(
            text(
                "DELETE FROM ref.calendar_tr "
                "WHERE trade_date = :trade_date "
                "  AND is_trading_day = false"
            ),
            {"trade_date": _parse_iso_date(trade_date)},
        )
