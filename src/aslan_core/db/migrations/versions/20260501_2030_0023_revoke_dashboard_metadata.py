"""Revoke ``metadata`` SELECT from aslan_dashboard on src.ingestion_run + doc.filing.

Revision ID: 0023
Revises: 0022
Create Date: 2026-05-01 20:30:00

Codex post-implementation review (HIGH): migration 0020's
column-allowlist GRANT for the dashboard role included
``src.ingestion_run.metadata`` and ``doc.filing.metadata``.
Spec §6.3 narrows the forbidden list to ``audit.events.metadata``,
but the in-code VM contract (``view_models.py`` module docstring)
treats ``metadata`` as a forbidden column class across the board:
every VM field must map to a "scalar metadata id, count, timestamp,
hash, status enum" and explicitly NOT a JSONB metadata blob. Today's
``queries.py`` projects neither column, but the lint allowlist
mirrors the GRANT and would let a future query reach in.

The defense-in-depth fix is to remove the GRANT — the privilege
layer becomes the load-bearing floor instead of relying on
``queries.py`` reviewers to remember the convention.

This migration only revokes column-level SELECT on two metadata
JSONB columns from ``aslan_dashboard``. No DML, no schema changes,
no impact on running consumers (the dashboard role's currently
deployed queries do not project either column).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("REVOKE SELECT (metadata) ON src.ingestion_run FROM aslan_dashboard")
    op.execute("REVOKE SELECT (metadata) ON doc.filing FROM aslan_dashboard")


def downgrade() -> None:
    # Restore the prior column-level SELECT for symmetry. Production
    # rollbacks would only run this if the dashboard role's GRANT
    # surface needed to revert — there is no DML to undo.
    op.execute("GRANT SELECT (metadata) ON src.ingestion_run TO aslan_dashboard")
    op.execute("GRANT SELECT (metadata) ON doc.filing TO aslan_dashboard")
