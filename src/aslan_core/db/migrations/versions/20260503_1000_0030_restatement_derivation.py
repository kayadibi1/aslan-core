"""Restatement basis derivation + measuring_unit_date.

Revision ID: 0030
Revises: 0029
Create Date: 2026-05-03 10:00:00

Five changes:

1. ``ref.tas29_exemption`` — entities exempt from TAS 29 restatement.
2. ``ts.financial_line_item.measuring_unit_date`` — the reporting-period
   date used as the CPI measurement point (backfilled to
   ``MAX(period_end)`` per filing).
3. ``ts.financial_line_item.derivation_reason`` — why the restatement
   basis was chosen (exemption, monetary_gl_line, inflation_tags,
   period_mandate, no_evidence).
4. CHECK constraint on ``restatement_basis`` expanded to include
   ``'as_reported'`` and ``'cpi_normalized'``.
5. Per-filing restatement derivation using a ranked ruleset (P0-P4).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | Sequence[str] | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Exemption table
    op.execute("""
        CREATE TABLE ref.tas29_exemption (
            entity_id   UUID PRIMARY KEY
                REFERENCES ref.entity(entity_id) ON DELETE CASCADE,
            reason      TEXT NOT NULL,
            added_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("GRANT SELECT ON ref.tas29_exemption TO aslan_dashboard")

    # 2. New columns on ts.financial_line_item
    op.execute("""
        ALTER TABLE ts.financial_line_item
            ADD COLUMN measuring_unit_date DATE,
            ADD COLUMN derivation_reason   TEXT
    """)

    # 3. CHECK constraint on restatement_basis (none existed before)
    op.execute("""
        ALTER TABLE ts.financial_line_item
            ADD CONSTRAINT financial_line_item_restatement_basis_check
            CHECK (restatement_basis IN (
                'nominal', 'as_reported', 'restated',
                'adjusted', 'cpi_normalized'
            ))
    """)

    # 4. Backfill measuring_unit_date = MAX(period_end) per filing_id
    op.execute("""
        UPDATE ts.financial_line_item fli
        SET    measuring_unit_date = sub.max_period_end
        FROM (
            SELECT filing_id, MAX(period_end) AS max_period_end
            FROM   ts.financial_line_item
            GROUP  BY filing_id
        ) sub
        WHERE fli.filing_id = sub.filing_id
          AND fli.measuring_unit_date IS NULL
    """)

    # 5. Per-filing restatement derivation (P0-P4 ranked ruleset)
    #
    # P0: Entity in ref.tas29_exemption → stays nominal, 'exemption'
    op.execute("""
        UPDATE ts.financial_line_item fli
        SET    derivation_reason = 'exemption'
        FROM   ref.tas29_exemption ex
        WHERE  fli.entity_id = ex.entity_id
          AND  fli.derivation_reason IS NULL
    """)

    # P1: Filing contains ifrs-full_GainsLossesOnNetMonetaryPosition
    op.execute("""
        UPDATE ts.financial_line_item fli
        SET    restatement_basis = 'as_reported',
               derivation_reason = 'monetary_gl_line'
        WHERE  fli.derivation_reason IS NULL
          AND  EXISTS (
              SELECT 1 FROM ts.financial_line_item p1
              WHERE  p1.filing_id = fli.filing_id
                AND  p1.line_code = 'ifrs-full_GainsLossesOnNetMonetaryPosition'
          )
    """)

    # P2: Filing contains any kap-fr_Inflation% or kap-fr_InflationAdjustmentsOnCapital
    op.execute("""
        UPDATE ts.financial_line_item fli
        SET    restatement_basis = 'as_reported',
               derivation_reason = 'inflation_tags'
        WHERE  fli.derivation_reason IS NULL
          AND  EXISTS (
              SELECT 1 FROM ts.financial_line_item p2
              WHERE  p2.filing_id = fli.filing_id
                AND  (p2.line_code LIKE 'kap-fr_Inflation%'
                      OR p2.line_code = 'kap-fr_InflationAdjustmentsOnCapital')
          )
    """)

    # P3: period_end >= 2023-12-31, accounting_standard = 'ifrs', not exempted
    op.execute("""
        UPDATE ts.financial_line_item fli
        SET    restatement_basis = 'as_reported',
               derivation_reason = 'period_mandate'
        WHERE  fli.derivation_reason IS NULL
          AND  fli.period_end >= '2023-12-31'
          AND  fli.accounting_standard = 'ifrs'
          AND  NOT EXISTS (
              SELECT 1 FROM ref.tas29_exemption ex
              WHERE  ex.entity_id = fli.entity_id
          )
    """)

    # P4: No evidence — stays nominal
    op.execute("""
        UPDATE ts.financial_line_item fli
        SET    derivation_reason = 'no_evidence'
        WHERE  fli.derivation_reason IS NULL
    """)


def downgrade() -> None:
    # Reverse derivation columns
    op.execute("""
        ALTER TABLE ts.financial_line_item
            DROP CONSTRAINT IF EXISTS financial_line_item_restatement_basis_check
    """)
    op.execute("""
        ALTER TABLE ts.financial_line_item
            DROP COLUMN IF EXISTS derivation_reason,
            DROP COLUMN IF EXISTS measuring_unit_date
    """)

    # Reset any restatement_basis values that were changed
    op.execute("""
        UPDATE ts.financial_line_item
        SET    restatement_basis = 'nominal'
        WHERE  restatement_basis IN ('as_reported', 'cpi_normalized')
    """)

    op.execute("REVOKE SELECT ON ref.tas29_exemption FROM aslan_dashboard")
    op.execute("DROP TABLE IF EXISTS ref.tas29_exemption")
