"""doc filing_body

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-28 23:24:58.845274

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE doc.filing_body (
            filing_id        UUID PRIMARY KEY REFERENCES doc.filing(filing_id) ON DELETE CASCADE,
            body_text        TEXT NOT NULL,
            body_lang        CHAR(2) NOT NULL,
            body_fts         tsvector GENERATED ALWAYS AS (to_tsvector('simple', body_text)) STORED,
            extracted_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX filing_body_fts ON doc.filing_body USING gin(body_fts)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS doc.filing_body")
