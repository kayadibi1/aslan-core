"""dq Batch 3 follow-up: audit.investing_com_slug — BIST → Investing.com slug map.

Revision ID: 0067
Revises: 0066
Create Date: 2026-05-09 15:14:00

The NG6 corroborator's ``investing_com`` adapter previously built a
URL of the form ``/equities/<ticker>-istanbul-stock-exchange`` and
relied on Investing.com's edge to redirect to the canonical slug
(``/equities/akbank``). That redirect is brittle — Investing's URL
slugs are editorial and not deterministic from the BIST ticker
(ASELS → ``aselsan-elektronik-sanayi``, BIMAS →
``bim-birlesik-magazalar``, etc.).

This migration creates ``audit.investing_com_slug`` as a small
ticker-keyed lookup table and seeds it with the 50 anchor tickers
from migration 0065 (``audit.curated_top_50``) plus a best-effort
slug map. The slugs are sampled from public Investing.com pages on
2026-05-09; they are unverified — production should run a
verification crawl (Firecrawl spend approval required) to confirm
each slug resolves to the correct equity page. Unverified rows
carry ``notes='unverified — sampled 2026-05-09'`` so the dashboard
can flag them for re-validation.

The corroborator's ``investing_com`` URL builder reads from this
table and falls back to the legacy ``lower(ticker)`` pattern when
a row is missing (emitting a ``corroborator_slug_missing`` event
so the gap surfaces on /dq/validation).

Privilege boundary:

  * ``audit_reader`` — SELECT.
  * ``aslan_dashboard`` — SELECT (the corroborator panel reads
    from this table at render time via the dashboard process).
  * Migration role only writes; no INSERT / UPDATE / DELETE
    granted to runtime roles. Slug refinements ship as new
    migrations so the curation history stays auditable.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0067"
down_revision: str | Sequence[str] | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (ticker, slug) — slugs are best-effort, sampled 2026-05-09 from
# public Investing.com pages. Production should run a verification
# crawl to confirm each slug resolves to the correct equity page.
_SLUG_MAP: tuple[tuple[str, str], ...] = (
    ("AKBNK", "akbank"),
    ("ASELS", "aselsan-elektronik-sanayi"),
    ("BIMAS", "bim-birlesik-magazalar"),
    ("EREGL", "eregli-demir-celik-fabrikalari"),
    ("FROTO", "ford-otomotiv-sanayi"),
    ("GARAN", "garanti-bankasi"),
    ("HALKB", "t.-halk-bankasi"),
    ("HEKTS", "hektas"),
    ("ISCTR", "t.-is-bankasi-c"),
    ("KCHOL", "koc-holding"),
    ("KOZAA", "koza-anadolu-metal-madencilik"),
    ("KOZAL", "koza-altin-isletmeleri"),
    ("KRDMD", "kardemir-d"),
    ("PETKM", "petkim-petrokimya"),
    ("PGSUS", "pegasus-hava-tasimaciligi"),
    ("SAHOL", "sabanci-holding"),
    ("SASA", "sasa-polyester"),
    ("SISE", "sise-cam"),
    ("TAVHL", "tav-havalimanlari-holding"),
    ("TCELL", "turkcell-iletisim-hizmetleri"),
    ("THYAO", "turk-hava-yollari"),
    ("TOASO", "tofas-turk-otomobil-fabrikasi"),
    ("TUPRS", "tupras-turkiye-petrol-rafinerileri"),
    ("VAKBN", "t-vakiflar-bankasi-vakifbank"),
    ("VESTL", "vestel-elektronik"),
    ("YKBNK", "yapi-ve-kredi-bankasi"),
    ("ARCLK", "arcelik"),
    ("ENKAI", "enka-insaat-ve-sanayi"),
    ("KORDS", "kordsa-teknik-tekstil"),
    ("SOKM", "sok-marketler"),
    ("AEFES", "anadolu-efes"),
    ("AGHOL", "ag-anadolu-grubu-holding"),
    ("ALARK", "alarko-holding"),
    ("CCOLA", "coca-cola-icecek"),
    ("DOAS", "dogus-otomotiv-servis-ticaret"),
    ("DOHOL", "dogan-sirketler-grubu-holding"),
    ("EKGYO", "emlak-konut-gayrimenkul-yatirim-ortakligi"),
    ("ENJSA", "enerjisa-enerji"),
    ("GUBRF", "gubre-fabrikalari"),
    ("ISDMR", "iskenderun-demir-celik"),
    ("KARSN", "karsan-otomotiv-sanayii-ve-ticaret"),
    ("MGROS", "migros-ticaret"),
    ("NTHOL", "net-holding"),
    ("ODAS", "odas-elektrik-uretim-sanayi-ticaret"),
    ("OYAKC", "oyak-cimento-fabrikalari"),
    ("SELEC", "selcuk-ecza-deposu"),
    (
        "SMRTG",
        "smart-gunes-enerjisi-teknolojileri-arastirma-gelistirme-uretim-sanayi-ve-ticaret",
    ),
    ("TKFEN", "tekfen-holding"),
    ("TTRAK", "turk-traktor-ve-ziraat-makineleri"),
    ("ZOREN", "zorlu-enerji-elektrik-uretim"),
)


_INSERT_ROW = text(
    "INSERT INTO audit.investing_com_slug(ticker, slug, notes) VALUES (:ticker, :slug, :notes)"
)


def upgrade() -> None:
    # ``full_url`` is GENERATED ALWAYS AS STORED so the dashboard /
    # corroborator can read the canonical URL without recomputing the
    # prefix in application code. Stored (not virtual) so the index
    # below works and so the column appears in pg_dump output for
    # downstream tooling.
    op.execute(
        """
        CREATE TABLE audit.investing_com_slug (
            ticker        TEXT PRIMARY KEY,
            slug          TEXT NOT NULL,
            full_url      TEXT GENERATED ALWAYS AS (
                              'https://tr.investing.com/equities/' || slug
                          ) STORED,
            verified_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            notes         TEXT
        )
        """
    )

    # Reader + dashboard SELECT. No writer GRANT — slug refinements
    # ship as new migrations so the curation history stays auditable.
    op.execute("GRANT SELECT ON audit.investing_com_slug TO audit_reader")
    op.execute("GRANT SELECT ON audit.investing_com_slug TO aslan_dashboard")

    # Seed the 50 rows. Every row is marked ``unverified`` so a future
    # production verification crawl can flip the notes column once a
    # slug has been confirmed to resolve to the correct page.
    bind = op.get_bind()
    notes = "unverified — sampled 2026-05-09"
    for ticker, slug in _SLUG_MAP:
        bind.execute(_INSERT_ROW, {"ticker": ticker, "slug": slug, "notes": notes})


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit.investing_com_slug")
