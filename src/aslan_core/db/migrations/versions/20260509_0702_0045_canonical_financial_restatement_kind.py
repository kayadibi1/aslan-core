"""No-op stub: ts.canonical_financial.restatement_basis already exists.

Revision ID: 0045
Revises: 0044
Create Date: 2026-05-09 07:02:00

SCOPE.md D8 originally proposed adding a ``restatement_kind`` column
to ``ts.canonical_financial`` for TAS 29 chain semantics. The actual
schema (verified via shadow-DB inspection 2026-05-09) already has a
``restatement_basis`` column constrained to
``('as_reported', 'cpi_normalized')`` and the PK already includes
``restatement_basis`` so multiple bases coexist as separate rows for
the same logical fact. The bitemporal contract for TAS 29 is
therefore already satisfied by the existing schema; no column-add is
needed.

This migration is a no-op preserved to keep the alembic chain
contiguous. SCOPE.md D8 is amended in
``IMPLEMENTATION_NOTES.md`` to reflect the existing convention.

Reversibility: trivial; no DDL.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0045"
down_revision: str | Sequence[str] | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
