"""streams schema (v0.5.0 — Task 3).

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-20 00:01:00

Establishes the ``streams`` schema namespace for the v0.5.0 outbox +
deadletter + redaction-registry subsystem. Subsequent migrations
0016-0019 create the actual tables.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS streams")


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS streams CASCADE")
