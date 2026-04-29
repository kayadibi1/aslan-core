"""doc filing_attachment

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-28 23:24:27.077955

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE doc.filing_attachment (
            attachment_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            filing_id        UUID NOT NULL REFERENCES doc.filing(filing_id) ON DELETE CASCADE,
            object_key       TEXT NOT NULL,
            mime             TEXT NOT NULL,
            sha256           CHAR(64) NOT NULL,
            bytes            BIGINT NOT NULL,
            role             TEXT NOT NULL,
            sequence         INT NOT NULL DEFAULT 0,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (filing_id, sha256)
        )
    """)
    op.execute("CREATE INDEX att_filing ON doc.filing_attachment(filing_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS doc.filing_attachment")
