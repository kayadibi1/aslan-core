"""doc filing republished_as gin index

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-29 01:36:00.000000

Codex 2026-04-29 (F6): find_by_source_ref now resolves republished
aliases via metadata->'republished_as' jsonb containment. A partial
GIN index on that path keeps the lookup O(log n) at scale.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX filing_republished_as_gin "
        "ON doc.filing USING gin ((metadata->'republished_as') jsonb_path_ops) "
        "WHERE metadata ? 'republished_as'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS doc.filing_republished_as_gin")
