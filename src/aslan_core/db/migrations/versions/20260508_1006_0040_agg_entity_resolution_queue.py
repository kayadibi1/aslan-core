"""agg.entity_resolution_queue — counterparty / mentioned-entity resolution backlog.

Revision ID: 0040
Revises: 0039
Create Date: 2026-05-08 10:06:00

Per ``aslan-event-extractor/SCOPE.md`` §5.3 / D7. Async resolution worker
drains this queue; the hot path stays sub-second by deferring NER lookup
+ registry match. Resolved rows trigger an ``as_of`` advance on the
parent ``agg.filing_event``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0040"
down_revision: str | Sequence[str] | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE agg.entity_resolution_queue (
            queue_id           BIGSERIAL PRIMARY KEY,
            source_event_id    UUID NOT NULL
                REFERENCES agg.filing_event(filing_event_id) ON DELETE CASCADE,
            candidate_name     TEXT NOT NULL,
            candidate_kind     TEXT,
            candidate_country  CHAR(2),
            payload_path       TEXT NOT NULL,
            resolved_entity_id UUID REFERENCES ref.entity(entity_id),
            resolved_at        TIMESTAMPTZ,
            resolved_by        TEXT,
            enqueued_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX erq_pending "
        "ON agg.entity_resolution_queue (enqueued_at) "
        "WHERE resolved_entity_id IS NULL"
    )
    op.execute("GRANT SELECT ON agg.entity_resolution_queue TO aslan_dashboard")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON agg.entity_resolution_queue FROM aslan_dashboard")
    op.execute("DROP TABLE IF EXISTS agg.entity_resolution_queue")
