"""ts.financial_line_item — structured financial statement rows.

Revision ID: 0029
Revises: 0028
Create Date: 2026-05-02 00:04:00

DBA §4.8 schema with one deliberate extension: ``consolidation`` in the
primary key. KAP filings contain both consolidated and unconsolidated
variants of every statement; without ``consolidation`` in the PK,
``ON CONFLICT DO NOTHING`` silently drops one variant.

No FK to ``doc.filing`` — the financial parser may write line items
before the filing row exists in a concurrent pipeline.  A NOT VALID FK
was attempted in production and dropped; the column is intentionally
unconstrained.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | Sequence[str] | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE ts.financial_line_item (
            entity_id           UUID NOT NULL
                REFERENCES ref.entity(entity_id) ON DELETE RESTRICT,
            filing_id           UUID NOT NULL,
            statement_type      TEXT NOT NULL
                CHECK (statement_type IN ('bs', 'is', 'cf', 'eq', 'notes')),
            line_code           TEXT NOT NULL,
            parent_line_code    TEXT,
            period_start        DATE NOT NULL,
            period_end          DATE NOT NULL,
            period_type         TEXT NOT NULL
                CHECK (period_type IN ('q', 'h', 'y', 'ytd')),
            value               NUMERIC,
            currency_code       CHAR(3) NOT NULL
                REFERENCES ref.currency(currency_code),
            consolidation       TEXT NOT NULL,
            accounting_standard TEXT NOT NULL,
            restatement_basis   TEXT NOT NULL DEFAULT 'nominal',
            as_of               TIMESTAMPTZ NOT NULL,
            ingestion_run_id    BIGINT NOT NULL
                REFERENCES src.ingestion_run(ingestion_run_id),
            PRIMARY KEY (entity_id, filing_id, statement_type, line_code,
                         consolidation, period_end, as_of)
        )
    """)
    op.execute(
        "CREATE INDEX fli_entity_period "
        "ON ts.financial_line_item(entity_id, period_end DESC, line_code)"
    )
    op.execute("GRANT SELECT ON ts.financial_line_item TO aslan_dashboard")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ts.financial_line_item")
