"""ts.entity_quality_score — per-entity quality scoring across financial periods.

Revision ID: 0032
Revises: 0031
Create Date: 2026-05-03 10:02:00

Stores the per-entity, per-period quality score produced by the financial
quality scorer: one row per (entity, period_end, period_type, consolidation,
currency_code, accounting_standard, restatement_basis, cpi_base_date,
mapping_version, as_of) with a 0-100 score, an insufficient_data flag, and
a JSONB checks payload carrying per-check detail.

The sentinel cpi_base_date value '9999-12-31' represents the absence of a
CPI base date for as_reported rows, matching the convention used in
ts.canonical_financial.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0032"
down_revision: str | Sequence[str] | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE ts.entity_quality_score (
            entity_id           UUID NOT NULL
                REFERENCES ref.entity(entity_id) ON DELETE RESTRICT,
            period_end          DATE NOT NULL,
            period_type         TEXT NOT NULL
                CHECK (period_type IN ('q', 'h', 'y', 'ytd')),
            consolidation       TEXT NOT NULL,
            currency_code       CHAR(3) NOT NULL
                REFERENCES ref.currency(currency_code),
            accounting_standard TEXT NOT NULL,
            restatement_basis   TEXT NOT NULL,
            cpi_base_date       DATE NOT NULL DEFAULT '9999-12-31',
            score               SMALLINT NOT NULL
                CHECK (score BETWEEN 0 AND 100),
            insufficient_data   BOOLEAN NOT NULL DEFAULT FALSE,
            checks              JSONB NOT NULL DEFAULT '{}',
            mapping_version     INT NOT NULL,
            manifest_hash       CHAR(64) NOT NULL,
            as_of               TIMESTAMPTZ NOT NULL,
            ingestion_run_id    BIGINT NOT NULL
                REFERENCES src.ingestion_run(ingestion_run_id),
            PRIMARY KEY (entity_id, period_end, period_type,
                         consolidation, currency_code, accounting_standard,
                         restatement_basis, cpi_base_date,
                         mapping_version, as_of)
        )
    """)
    op.execute("GRANT SELECT ON ts.entity_quality_score TO aslan_dashboard")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ts.entity_quality_score")
