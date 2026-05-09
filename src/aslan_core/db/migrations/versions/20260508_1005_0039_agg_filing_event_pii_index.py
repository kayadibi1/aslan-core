"""agg.filing_event_pii_index — GDPR Art. 17 PII path index per event.

Revision ID: 0039
Revises: 0038
Create Date: 2026-05-08 10:05:00

Per ``aslan-event-extractor/SCOPE.md`` §5.3 / D11 / D21. Every event with
PII fields registers each PII path here on insert. Art. 17 redaction
walks this index. ON DELETE CASCADE on ``filing_event_id`` means dropping
an event row purges its PII pointers cleanly.

INTENTIONALLY NOT GRANTED to ``aslan_dashboard`` — this table is the
inverted index of every PII payload-path in production data. Read access
is privileged-only via the application tier's encrypted-role escalation.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0039"
down_revision: str | Sequence[str] | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE agg.filing_event_pii_index (
            filing_event_id  UUID NOT NULL
                REFERENCES agg.filing_event(filing_event_id) ON DELETE CASCADE,
            pii_kind         TEXT NOT NULL,
            payload_path     TEXT NOT NULL,
            redacted         BOOLEAN NOT NULL DEFAULT false,
            redacted_at      TIMESTAMPTZ,
            redaction_reason TEXT,
            PRIMARY KEY (filing_event_id, payload_path)
        )
    """)
    op.execute("CREATE INDEX pii_kind ON agg.filing_event_pii_index (pii_kind, redacted)")
    # NOT granted to aslan_dashboard — privileged role only.


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agg.filing_event_pii_index")
