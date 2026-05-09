"""agg.filing_event_quarantine — bodies the extractor cannot process.

Revision ID: 0036
Revises: 0035
Create Date: 2026-05-08 10:02:00

Per ``aslan-event-extractor/SCOPE.md`` §5.2. Reasons: ``too_large``,
``no_body``, ``parse_error``, ``model_error``. ``detail`` JSONB carries
type-specific context (e.g., chunk count, body size, model error string).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0036"
down_revision: str | Sequence[str] | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE agg.filing_event_quarantine (
            filing_id      UUID PRIMARY KEY REFERENCES doc.filing(filing_id),
            reason         TEXT NOT NULL,
            detail         JSONB NOT NULL DEFAULT '{}',
            last_attempt   TIMESTAMPTZ,
            attempt_count  INT NOT NULL DEFAULT 0,
            quarantined_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("GRANT SELECT ON agg.filing_event_quarantine TO aslan_dashboard")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON agg.filing_event_quarantine FROM aslan_dashboard")
    op.execute("DROP TABLE IF EXISTS agg.filing_event_quarantine")
