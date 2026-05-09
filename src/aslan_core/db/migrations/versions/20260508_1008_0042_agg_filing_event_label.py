"""agg.filing_event_label — ground-truth labels for the 500-disclosure benchmark set.

Revision ID: 0042
Revises: 0041
Create Date: 2026-05-08 10:08:00

Per ``aslan-event-extractor/SCOPE.md`` §5.4 / D20. 100 hand-labelled by
sidar; 300 LLM-proposed-and-confirmed via /review UI; 100 final hand-
labelled holdout (``is_holdout=true``) for acceptance gating in M6.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0042"
down_revision: str | Sequence[str] | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE agg.filing_event_label (
            label_id          BIGSERIAL PRIMARY KEY,
            filing_id         UUID NOT NULL REFERENCES doc.filing(filing_id),
            event_type        agg.filing_event_type NOT NULL,
            event_seq         SMALLINT NOT NULL DEFAULT 1,
            expected_payload  JSONB NOT NULL,
            labeller          TEXT NOT NULL,
            confirmed_by      TEXT,
            confidence        REAL,
            is_holdout        BOOLEAN NOT NULL DEFAULT false,
            notes             TEXT,
            labelled_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX fel_filing ON agg.filing_event_label (filing_id)")
    op.execute("CREATE INDEX fel_event_type ON agg.filing_event_label (event_type)")
    op.execute(
        "CREATE INDEX fel_holdout ON agg.filing_event_label (is_holdout) WHERE is_holdout = true"
    )
    op.execute("GRANT SELECT ON agg.filing_event_label TO aslan_dashboard")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON agg.filing_event_label FROM aslan_dashboard")
    op.execute("DROP TABLE IF EXISTS agg.filing_event_label")
