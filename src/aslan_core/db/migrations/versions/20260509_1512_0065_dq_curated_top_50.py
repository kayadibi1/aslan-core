"""dq M5.1 follow-up: audit.curated_top_50 backing table + BIST-30+20 seed.

Revision ID: 0065
Revises: 0064
Create Date: 2026-05-09 15:12:00

The ``regression_flag_critical_entity`` severity rule (seeded by
migration 0059, dispatched by ``aslan_core.dq.alert_dispatch``) joins
``audit.regression_flag.entity_id`` against ``audit.curated_top_50`` to
up-promote severity for top-50 BIST entity flags. The dispatcher's
predicate query referenced this table since 0059, but the table was
never created — the rule's WHERE clause silently returned zero rows
on every sweep. This migration creates the table and seeds the
canonical BIST-30 + 20 strategic-coverage extras.

Roster (rank order is the binding curation):

  1-30: BIST-30 — large-cap headline index. AKBNK, ASELS, BIMAS,
        EREGL, FROTO, GARAN, HALKB, HEKTS, ISCTR, KCHOL, KOZAA,
        KOZAL, KRDMD, PETKM, PGSUS, SAHOL, SASA, SISE, TAVHL,
        TCELL, THYAO, TOASO, TUPRS, VAKBN, VESTL, YKBNK, ARCLK,
        ENKAI, KORDS, SOKM.

  31-50: Strategic-coverage extras chosen for sector breadth and
         frequent disclosure cadence — AEFES, AGHOL, ALARK, CCOLA,
         DOAS, DOHOL, EKGYO, ENJSA, GUBRF, ISDMR, KARSN, MGROS,
         NTHOL, ODAS, OYAKC, SELEC, SMRTG, TKFEN, TTRAK, ZOREN.

Entity-id derivation: the testcontainer fresh DB doesn't have
``ref.entity`` populated, so we cannot join on ticker at migration
time. Instead each row carries a deterministic UUIDv5 computed as
``uuid5(NAMESPACE_DNS, f'bist:ticker:{ticker}')`` so the seed is
reproducible across environments. The actual ``ref.entity.entity_id``
linkage is established later (production runtime patches the
``entity_id`` column to match ``ref.entity`` after that registry is
populated; the join key in ``regression_flag_critical_entity`` is
the ticker via ``ref.entity`` lookup).

Privilege boundary:

  * ``audit_reader`` — SELECT (the dispatcher predicate runs under
    audit_reader-equivalent privileges via the writer role's read
    capability).
  * ``aslan_dashboard`` — SELECT (forward-compat: a future
    /dq/regression panel may surface "this flag fired against a
    top-50 entity" labels).
  * Migration role only writes; no INSERT / UPDATE / DELETE granted
    to runtime roles. Curation changes ship as new migrations.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import NAMESPACE_DNS, uuid5

from alembic import op
from sqlalchemy import text

revision: str = "0065"
down_revision: str | Sequence[str] | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (rank, ticker, display_name) — ranks 1..30 are BIST-30, 31..50 are
# strategic-coverage extras. Display name is informational only; the
# table stores ticker + rank, not the display name.
_ROSTER: tuple[tuple[int, str, str], ...] = (
    # BIST-30
    (1, "AKBNK", "Akbank"),
    (2, "ASELS", "Aselsan"),
    (3, "BIMAS", "BIM Birleşik Mağazalar"),
    (4, "EREGL", "Ereğli Demir Çelik"),
    (5, "FROTO", "Ford Otomotiv"),
    (6, "GARAN", "Garanti BBVA"),
    (7, "HALKB", "Halkbank"),
    (8, "HEKTS", "Hektaş"),
    (9, "ISCTR", "İş Bankası C"),  # noqa: RUF001
    (10, "KCHOL", "Koç Holding"),
    (11, "KOZAA", "Koza Anadolu"),
    (12, "KOZAL", "Koza Altın"),  # noqa: RUF001
    (13, "KRDMD", "Kardemir D"),
    (14, "PETKM", "Petkim"),
    (15, "PGSUS", "Pegasus"),
    (16, "SAHOL", "Sabancı Holding"),  # noqa: RUF001
    (17, "SASA", "SASA Polyester"),
    (18, "SISE", "Şişe Cam"),
    (19, "TAVHL", "TAV Havalimanları"),  # noqa: RUF001
    (20, "TCELL", "Turkcell"),
    (21, "THYAO", "Türk Hava Yolları"),  # noqa: RUF001
    (22, "TOASO", "Tofaş"),
    (23, "TUPRS", "Tüpraş"),
    (24, "VAKBN", "VakıfBank"),  # noqa: RUF001
    (25, "VESTL", "Vestel"),
    (26, "YKBNK", "Yapı Kredi"),  # noqa: RUF001
    (27, "ARCLK", "Arçelik"),
    (28, "ENKAI", "Enka İnşaat"),
    (29, "KORDS", "Kordsa"),
    (30, "SOKM", "Şok Marketler"),
    # Strategic-coverage extras
    (31, "AEFES", "Anadolu Efes"),
    (32, "AGHOL", "AG Anadolu Grubu"),
    (33, "ALARK", "Alarko Holding"),
    (34, "CCOLA", "Coca-Cola İçecek"),
    (35, "DOAS", "Doğuş Otomotiv"),
    (36, "DOHOL", "Doğan Holding"),
    (37, "EKGYO", "Emlak Konut GYO"),
    (38, "ENJSA", "Enerjisa Enerji"),
    (39, "GUBRF", "Gübre Fabrikaları"),  # noqa: RUF001
    (40, "ISDMR", "İskenderun Demir Çelik"),
    (41, "KARSN", "Karsan"),
    (42, "MGROS", "Migros"),
    (43, "NTHOL", "Net Holding"),
    (44, "ODAS", "Odaş Elektrik"),
    (45, "OYAKC", "Oyak Çimento"),
    (46, "SELEC", "Selçuk Ecza"),
    (47, "SMRTG", "Smart Güneş Enerjisi"),
    (48, "TKFEN", "Tekfen Holding"),
    (49, "TTRAK", "Türk Traktör"),
    (50, "ZOREN", "Zorlu Enerji"),
)


_INSERT_ROW = text(
    "INSERT INTO audit.curated_top_50("
    "  entity_id, ticker, rank, notes"
    ") VALUES ("
    "  CAST(:entity_id AS UUID), :ticker, :rank, NULL"
    ")"
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE audit.curated_top_50 (
            entity_id      UUID PRIMARY KEY,
            ticker         TEXT NOT NULL UNIQUE,
            rank           INT  NOT NULL CHECK (rank > 0),
            curated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            notes          TEXT
        )
        """
    )
    op.execute("CREATE INDEX curated_top_50_rank ON audit.curated_top_50(rank)")
    # Reader + dashboard SELECT. No writer GRANT — curation changes
    # ship as new migrations, never as runtime mutations.
    op.execute("GRANT SELECT ON audit.curated_top_50 TO audit_reader")
    op.execute("GRANT SELECT ON audit.curated_top_50 TO aslan_dashboard")

    # Seed the 50 rows. Deterministic UUIDv5 from
    # ``uuid5(NAMESPACE_DNS, f'bist:ticker:{ticker}')`` so the seed is
    # reproducible across environments without depending on ref.entity.
    bind = op.get_bind()
    for rank, ticker, _display in _ROSTER:
        entity_id = uuid5(NAMESPACE_DNS, f"bist:ticker:{ticker}")
        bind.execute(
            _INSERT_ROW,
            {"entity_id": str(entity_id), "ticker": ticker, "rank": rank},
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit.curated_top_50")
