"""dq NG1: public_status_reader role for the public /status page.

Revision ID: 0063
Revises: 0062
Create Date: 2026-05-09 15:10:00

Spec: NG1 (workspace CLAUDE.md autonomy directive — public status page).

Creates the dedicated low-privilege ``public_status_reader`` role for
the public, no-auth ``/status`` page. Defense-in-depth: separated
from ``aslan_dashboard`` (which has GRANTs on streams, doc, ts, src,
ref, agg) so a misconfigured public-status process logs in to a role
that can ONLY SELECT the two tables the public status page consumes:

  - ``audit.recency_observation``  (current freshness lag per source)
  - ``audit.coverage_snapshot``    (latest coverage % per source/dim)

Even a SQL-injection escape from the read-only page cannot reach
``streams.outbox.payload``, ``doc.filing_body``, ``audit.events`` or
any other internal surface — the role simply lacks USAGE on those
schemas.

The role:

  * LOGINs with its own password (``ASLAN_PUBLIC_STATUS_PASSWORD``);
  * has ``default_transaction_read_only=on`` as a soft floor;
  * holds USAGE on ``audit`` schema only (no other schema is reachable);
  * holds SELECT on the two source-of-truth tables.

Password handling mirrors migration 0020 (set_config + DO block + ``%L``
literal escaping inside a dollar-quoted body so SQLAlchemy bind
parameters work).
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0063"
down_revision: str | Sequence[str] | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    password = os.environ.get("ASLAN_PUBLIC_STATUS_PASSWORD")
    if not password:
        env = os.environ.get("ASLAN_ENV", "dev")
        if env != "dev":
            raise RuntimeError(
                "ASLAN_PUBLIC_STATUS_PASSWORD is required in non-dev "
                "environments. Generate a strong password and set it before "
                "running migration 0063. See docs/dq-handoff.md (NG1 public "
                "status page) for the deployment runbook."
            )
        password = "DEV_ONLY_REPLACE_ME"  # noqa: S105 — intentional dev fallback

    op.execute(
        text("SELECT set_config('aslan.public_status_password', :pw, true)").bindparams(pw=password)
    )
    op.execute(
        """
        DO $$
        BEGIN
            EXECUTE format(
                'CREATE ROLE public_status_reader LOGIN PASSWORD %L',
                current_setting('aslan.public_status_password')
            );
        EXCEPTION WHEN duplicate_object THEN
            NULL;
        END
        $$
        """
    )

    op.execute("ALTER ROLE public_status_reader SET default_transaction_read_only = on")

    # USAGE on audit schema only — every other schema (streams, doc,
    # ts, src, ref, agg) is unreachable from this role.
    op.execute("GRANT USAGE ON SCHEMA audit TO public_status_reader")

    # SELECT on the two public-safe tables only. No other audit.* table
    # is granted — alert_dispatch payloads, validation_failure raw rows,
    # spot_check labels, scorecard notes, etc. all stay private.
    op.execute("GRANT SELECT ON audit.recency_observation TO public_status_reader")
    op.execute("GRANT SELECT ON audit.coverage_snapshot TO public_status_reader")


def downgrade() -> None:
    # PG cannot DROP a role while ANY object grants privilege to it.
    # ``DROP OWNED BY ... CASCADE`` revokes every grant on every object
    # in any database the role can reach. Required before DROP ROLE.
    op.execute("DROP OWNED BY public_status_reader CASCADE")
    op.execute("DROP ROLE IF EXISTS public_status_reader")
