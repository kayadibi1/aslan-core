"""agg.filing_event_type ENUM

Revision ID: 0034
Revises: 0033
Create Date: 2026-05-08 10:00:00

The 32-value ENUM that types every row in agg.filing_event. Locked by
``aslan-event-extractor/SCOPE.md`` D18; the count is asserted in
``test_migration_0034_agg_filing_event_type_enum.py`` so a future
shrink/grow surfaces against the tests, not in production.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0034"
down_revision: str | Sequence[str] | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_VALUES: tuple[str, ...] = (
    # Capital structure (10)
    "dividend",
    "dividend_revision",
    "capital_action",
    "capital_action_revision",
    "share_buyback",
    "rights_offering",
    "treasury_share_change",
    "prospectus_published",
    "name_change",
    "delisting",
    # Corporate actions (5)
    "merger_acquisition",
    "tender_offer",
    "divestiture",
    "articles_amendment",
    "bankruptcy_event",
    # Governance & ownership (5)
    "board_change",
    "executive_change",
    "auditor_change",
    "insider_trade",
    "material_shareholder_change",
    # Operating & financial (7)
    "earnings_release",
    "guidance_update",
    "material_contract",
    "material_contract_termination",
    "litigation_update",
    "regulatory_action",
    "going_concern",
    # Status & ratings (2)
    "force_majeure",
    "credit_rating_change",
    # Assembly + ESG + catch-all (3)
    "general_assembly",
    "esg_disclosure",
    "other_material",
)


def upgrade() -> None:
    values_sql = ", ".join(f"'{v}'" for v in _VALUES)
    op.execute(f"CREATE TYPE agg.filing_event_type AS ENUM ({values_sql})")


def downgrade() -> None:
    op.execute("DROP TYPE IF EXISTS agg.filing_event_type")
