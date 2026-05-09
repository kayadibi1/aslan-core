"""agg.extraction_audit_log — every LLM call audited (SOC II Processing Integrity).

Revision ID: 0041
Revises: 0040
Create Date: 2026-05-08 10:07:00

Per ``aslan-event-extractor/SCOPE.md`` §5.3 / D11. Hypertable with
30-day chunks, compressed after 90 days. PK is composite
``(audit_id, started_at)`` because TimescaleDB requires the
partitioning column in the PK — SCOPE §5.3's wording shows
``audit_id BIGSERIAL PK`` but that is a hypertable contradiction;
the composite PK is the resolved form.

INTENTIONALLY NOT GRANTED to ``aslan_dashboard`` — the audit log
contains every LLM input/output hash and full prompt sha; access
is privileged-only via the application tier's audit-read scope.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0041"
down_revision: str | Sequence[str] | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE agg.extraction_audit_log (
            audit_id           BIGSERIAL,
            filing_id          UUID NOT NULL,
            pass_kind          TEXT NOT NULL,
            model_version      TEXT NOT NULL,
            prompt_version     TEXT NOT NULL,
            prompt_sha256      CHAR(64) NOT NULL,
            input_text_sha256  CHAR(64) NOT NULL,
            input_token_count  INT NOT NULL,
            output_token_count INT NOT NULL,
            output_sha256      CHAR(64) NOT NULL,
            output_storage_key TEXT,
            latency_ms         INT NOT NULL,
            cost_micro_usd     INT,
            actor_id           TEXT NOT NULL,
            ingestion_run_id   BIGINT NOT NULL,
            started_at         TIMESTAMPTZ NOT NULL,
            finished_at        TIMESTAMPTZ NOT NULL,
            error              TEXT,
            PRIMARY KEY (audit_id, started_at)
        )
    """)
    op.execute(
        "SELECT create_hypertable('agg.extraction_audit_log', 'started_at', "
        "chunk_time_interval => INTERVAL '30 days', if_not_exists => TRUE)"
    )
    op.execute("CREATE INDEX eal_filing ON agg.extraction_audit_log (filing_id, started_at DESC)")
    op.execute("CREATE INDEX eal_run ON agg.extraction_audit_log (ingestion_run_id)")
    op.execute("""
        ALTER TABLE agg.extraction_audit_log SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'pass_kind',
            timescaledb.compress_orderby = 'started_at DESC'
        )
    """)
    op.execute("SELECT add_compression_policy('agg.extraction_audit_log', INTERVAL '90 days')")
    # NOT granted to aslan_dashboard — privileged role only.


def downgrade() -> None:
    op.execute("SELECT remove_compression_policy('agg.extraction_audit_log', if_exists => TRUE)")
    op.execute("DROP TABLE IF EXISTS agg.extraction_audit_log")
