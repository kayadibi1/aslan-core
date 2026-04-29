"""doc schema

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-28 23:22:51.035618

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS doc")


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS doc CASCADE")
