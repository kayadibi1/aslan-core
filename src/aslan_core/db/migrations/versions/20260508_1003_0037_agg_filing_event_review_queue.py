"""agg.filing_event_review_queue — Tier 1/2 disagreements awaiting human resolution.

Revision ID: 0037
Revises: 0036
Create Date: 2026-05-08 10:03:00

Per ``aslan-event-extractor/SCOPE.md`` §5.2 / D9. Filed when Tier 1
extractor and Tier 2 verifier disagree (or the verifier rejects the
primary). The ``/review`` operator route walks unresolved rows.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0037"
down_revision: str | Sequence[str] | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE agg.filing_event_review_queue (
            review_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            filing_id            UUID NOT NULL REFERENCES doc.filing(filing_id),
            primary_event_id     UUID,
            primary_model        TEXT NOT NULL,
            primary_payload      JSONB NOT NULL,
            primary_confidence   REAL NOT NULL,
            verifier_model       TEXT NOT NULL,
            verifier_payload     JSONB NOT NULL,
            verifier_confidence  REAL NOT NULL,
            diff_summary         TEXT NOT NULL,
            enqueued_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            resolved_at          TIMESTAMPTZ,
            resolved_by          TEXT,
            resolution           TEXT,
            resolution_payload   JSONB
        )
    """)
    op.execute(
        "CREATE INDEX freview_unresolved "
        "ON agg.filing_event_review_queue (enqueued_at) "
        "WHERE resolved_at IS NULL"
    )
    op.execute("GRANT SELECT ON agg.filing_event_review_queue TO aslan_dashboard")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON agg.filing_event_review_queue FROM aslan_dashboard")
    op.execute("DROP TABLE IF EXISTS agg.filing_event_review_queue")
