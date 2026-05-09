"""dq NG6: external_corroborator_cache for spot-check second-opinion data.

Revision ID: 0064
Revises: 0063
Create Date: 2026-05-09 15:11:00

Spec: NG6 (workspace CLAUDE.md autonomy directive — promotes spec §7.2
"v2 lands later" to in-scope).

Backs the firecrawl-driven external corroborator panel on the
``/dq/spot-check/<sample_id>`` page. Each row caches one extracted
"second opinion" payload (latest price / market cap / revenue, etc.)
fetched from a registered external source (Investing.com Turkey, KAP
IR page, ...) keyed on ``(source, entity_ticker)``.

Cost discipline (workspace CLAUDE.md §3): firecrawl is paid; the
24-hour cache + manual-refresh-only design caps spend even if
labellers open hundreds of spot-check pages a week.

Privilege boundary:

  * ``audit_writer`` — INSERT only. The cache is append-only; refreshes
    write a new row rather than overwriting an old one so the labeller
    can see the historical fetch trail.
  * ``audit_reader`` — SELECT.
  * ``aslan_dashboard`` — SELECT (the spot-check page reads the latest
    non-stale row through a write-capable session in production, but
    the dashboard role itself stays read-only).

UPDATE is intentionally never granted — every refresh is a fresh INSERT.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0064"
down_revision: str | Sequence[str] | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE audit.external_corroborator_cache (
            cache_id          BIGSERIAL PRIMARY KEY,
            source            TEXT NOT NULL,
            entity_ticker     TEXT NOT NULL,
            fetched_at        TIMESTAMPTZ NOT NULL,
            cached_payload    JSONB NOT NULL,
            fetch_url         TEXT NOT NULL,
            fetch_latency_ms  INT  NOT NULL,
            fetch_status      TEXT NOT NULL
                CHECK (fetch_status IN ('ok','error','rate_limited','blocked')),
            error_summary     TEXT,
            recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX ecc_source_ticker "
        "ON audit.external_corroborator_cache(source, entity_ticker, fetched_at DESC)"
    )

    # Writer role: INSERT only. UPDATE not granted — the cache is
    # append-only; "refresh" is a new INSERT, not a row mutation.
    op.execute("GRANT INSERT ON audit.external_corroborator_cache TO audit_writer")
    op.execute(
        "GRANT USAGE ON SEQUENCE audit.external_corroborator_cache_cache_id_seq TO audit_writer"
    )

    # Reader role: SELECT.
    op.execute("GRANT SELECT ON audit.external_corroborator_cache TO audit_reader")

    # Dashboard role: SELECT only — same posture as spot_check tables
    # (migration 0058). The corroborator panel render path is read-only;
    # refresh-button POSTs run under the labeller's write-capable session.
    op.execute("GRANT SELECT ON audit.external_corroborator_cache TO aslan_dashboard")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit.external_corroborator_cache")
