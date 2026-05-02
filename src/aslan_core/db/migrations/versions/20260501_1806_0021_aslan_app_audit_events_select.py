"""aslan_app needs SELECT on audit.events.metadata for the v0.6.0
SECURITY DEFINER helper.

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-01 18:06:00

Surfaced by the v0.6.0 dashboard boundary test suite (Task 2).
Migration 0020 created ``audit.event_metadata_key_count`` SECURITY
DEFINER and ``ALTER FUNCTION … OWNER TO aslan_app`` so the helper
runs with the role that holds SELECT on ``audit.events.metadata``.
But ``aslan_app`` had never been granted USAGE on the audit schema
or SELECT on ``audit.events`` — calling the helper from the
dashboard role failed at the helper body's first read with
``permission denied for schema audit``.

This migration grants the minimum:

  * ``USAGE ON SCHEMA audit`` so aslan_app can name objects in the
    schema during the helper body's name resolution.
  * ``SELECT (event_id, occurred_at, metadata) ON audit.events`` —
    column-level rather than full-table so ``aslan_app`` does NOT
    gain SELECT on ``before`` / ``after`` / other PII columns. The
    helper body only reads these three columns; column-level matches
    the helper's actual surface.

Codex round-9's analysis correctly identified that the SECURITY
DEFINER needed an explicit OWNER but assumed (without checking) that
``aslan_app`` already held SELECT on the target. That assumption was
true for ``streams.outbox`` (the original spec helper, dropped during
plan-rounds) but never true for ``audit.events``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("GRANT USAGE ON SCHEMA audit TO aslan_app")
    op.execute("GRANT SELECT (event_id, occurred_at, metadata) ON audit.events TO aslan_app")


def downgrade() -> None:
    op.execute("REVOKE SELECT (event_id, occurred_at, metadata) ON audit.events FROM aslan_app")
    op.execute("REVOKE USAGE ON SCHEMA audit FROM aslan_app")
