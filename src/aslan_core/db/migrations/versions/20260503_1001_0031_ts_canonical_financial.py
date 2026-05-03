"""ts.canonical_financial — cross-source canonical financial data.

Revision ID: 0031
Revises: 0030
Create Date: 2026-05-03 10:01:00

Stores the normalised, deduplicated output of the financial canonicaliser:
one row per (entity, canonical_code, period_end, period_type, consolidation,
currency_code, accounting_standard, restatement_basis, cpi_base_date,
mapping_version, as_of) with full source-contribution provenance and
quality-flag metadata.

The sentinel cpi_base_date value '9999-01-01' represents the absence of a
CPI base date for as_reported rows.  A CHECK constraint enforces that
cpi_normalized rows carry a non-sentinel cpi_base_date and a
measuring_unit_date, while as_reported rows carry the sentinel and no
measuring_unit_date.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0031"
down_revision: str | Sequence[str] | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE ts.canonical_financial (
            entity_id           UUID NOT NULL
                REFERENCES ref.entity(entity_id) ON DELETE RESTRICT,
            canonical_code      TEXT NOT NULL,
            period_end          DATE NOT NULL,
            period_type         TEXT NOT NULL
                CHECK (period_type IN ('q', 'h', 'y', 'ytd')),
            consolidation       TEXT NOT NULL,
            restatement_basis   TEXT NOT NULL
                CHECK (restatement_basis IN ('as_reported', 'cpi_normalized')),
            currency_code       CHAR(3) NOT NULL
                REFERENCES ref.currency(currency_code),
            accounting_standard TEXT NOT NULL,
            value               NUMERIC,
            payload_hash        CHAR(64) NOT NULL,
            source_contributions JSONB NOT NULL,
            computed            BOOLEAN NOT NULL DEFAULT FALSE,
            quality_flags       JSONB NOT NULL DEFAULT '{}',
            cpi_base_date       DATE NOT NULL DEFAULT '9999-01-01',
            measuring_unit_date DATE,
            mapping_version     INT NOT NULL,
            manifest_hash       CHAR(64) NOT NULL,
            as_of               TIMESTAMPTZ NOT NULL,
            ingestion_run_id    BIGINT NOT NULL
                REFERENCES src.ingestion_run(ingestion_run_id),
            PRIMARY KEY (entity_id, canonical_code, period_end, period_type,
                         consolidation, currency_code, accounting_standard,
                         restatement_basis, cpi_base_date,
                         mapping_version, as_of),
            CHECK (
                (restatement_basis = 'cpi_normalized'
                    AND cpi_base_date <> '9999-01-01'
                    AND measuring_unit_date IS NOT NULL)
                OR
                (restatement_basis = 'as_reported'
                    AND cpi_base_date = '9999-01-01')
            )
        )
    """)
    op.execute(
        "CREATE INDEX cf_entity_period "
        "ON ts.canonical_financial(entity_id, period_end DESC, canonical_code)"
    )
    op.execute("GRANT SELECT ON ts.canonical_financial TO aslan_dashboard")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ts.canonical_financial")
