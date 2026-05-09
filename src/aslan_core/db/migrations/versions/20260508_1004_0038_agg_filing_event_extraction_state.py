"""agg.filing_event_extraction_state — drives re-extraction queues for prompt/model upgrades.

Revision ID: 0038
Revises: 0037
Create Date: 2026-05-08 10:04:00

Per ``aslan-event-extractor/SCOPE.md`` §5.2 / D24. Composite PK on
(filing_id, model_version, prompt_version) means the same filing has
one row per (model, prompt) tuple, enabling targeted re-extraction
when a prompt or model upgrades. ``status`` ∈ {pending, extracted,
quarantined, review}.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0038"
down_revision: str | Sequence[str] | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE agg.filing_event_extraction_state (
            filing_id         UUID NOT NULL REFERENCES doc.filing(filing_id),
            model_version     TEXT NOT NULL,
            prompt_version    TEXT NOT NULL,
            status            TEXT NOT NULL,
            last_extracted_at TIMESTAMPTZ,
            last_error        TEXT,
            PRIMARY KEY (filing_id, model_version, prompt_version)
        )
    """)
    op.execute(
        "CREATE INDEX fes_pending "
        "ON agg.filing_event_extraction_state "
        "(model_version, prompt_version, last_extracted_at) "
        "WHERE status = 'pending'"
    )
    op.execute("GRANT SELECT ON agg.filing_event_extraction_state TO aslan_dashboard")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON agg.filing_event_extraction_state FROM aslan_dashboard")
    op.execute("DROP TABLE IF EXISTS agg.filing_event_extraction_state")
