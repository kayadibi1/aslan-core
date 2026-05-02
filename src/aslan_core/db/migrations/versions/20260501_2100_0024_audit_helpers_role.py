"""Dedicated NOLOGIN owner role for audit SECURITY DEFINER helpers.

Revision ID: 0024
Revises: 0023
Create Date: 2026-05-01 21:00:00

Codex post-implementation review (HIGH): migrations 0021 + 0022
gave the broad runtime ``aslan_app`` role direct ``SELECT`` on
``audit.events.metadata`` and ``audit.events.client_ip`` so the
two SECURITY DEFINER helpers
(``audit.event_metadata_key_count``, ``audit.event_client_ip_truncated``)
could read the underlying columns when they ran as ``aslan_app``.

That works, but it also expands ``aslan_app``'s privileges: any
compromised application path or SQL-injection vector running
under ``aslan_app`` now reads raw audit metadata + client IP
directly, bypassing the dashboard-only "derived value" design.

This migration tightens the boundary by introducing a dedicated
no-login owner role for the helpers:

  * ``aslan_audit_helpers`` (NOLOGIN) is created.
  * The role gets USAGE on the audit schema + column-level SELECT
    on ``audit.events.event_id``, ``occurred_at``, ``metadata``,
    ``client_ip`` — the exact set the two helper bodies need.
  * Both SECURITY DEFINER helpers transfer ownership from
    ``aslan_app`` to ``aslan_audit_helpers``. Because the function
    body runs with the OWNER's privileges, the helpers continue
    to work; the dashboard role's ``EXECUTE`` grant is unaffected.
  * Raw ``metadata`` + ``client_ip`` SELECT is REVOKEd from
    ``aslan_app``. The runtime application role is back to
    insert-only on audit.events (or whatever it had before
    migrations 0021/0022 expanded it).

The migration is privilege-only — no DML, no schema changes, no
helper-function rewrites. Existing ``EXECUTE`` GRANTs on the two
helpers (set up in 0020/0022) survive because EXECUTE ACL is
separate from owner.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── New NOLOGIN role + privileges ────────────────────────────
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'aslan_audit_helpers'
            ) THEN
                CREATE ROLE aslan_audit_helpers NOLOGIN;
            END IF;
        END $$;
        """
    )
    op.execute("GRANT USAGE ON SCHEMA audit TO aslan_audit_helpers")
    op.execute(
        "GRANT SELECT (event_id, occurred_at, metadata, client_ip) "
        "ON audit.events TO aslan_audit_helpers"
    )

    # ── Transfer helper ownership ────────────────────────────────
    # Both signatures are pinned by the v0.6.0 plan + migrations
    # 0020 / 0022. The OWNER swap is what flips the SECURITY DEFINER
    # body's effective role from aslan_app to aslan_audit_helpers.
    op.execute(
        "ALTER FUNCTION audit.event_metadata_key_count("
        "p_event_id BIGINT, p_occurred_at TIMESTAMPTZ"
        ") OWNER TO aslan_audit_helpers"
    )
    op.execute(
        "ALTER FUNCTION audit.event_client_ip_truncated("
        "p_event_id BIGINT, p_occurred_at TIMESTAMPTZ"
        ") OWNER TO aslan_audit_helpers"
    )

    # ── Revoke aslan_app raw access ──────────────────────────────
    # aslan_app no longer needs SELECT on these columns now that the
    # helpers run as aslan_audit_helpers. Migrations 0021 + 0022
    # introduced the GRANTs purely for the helper bodies; with the
    # owner change, the GRANTs become unnecessary surface.
    op.execute("REVOKE SELECT (event_id, occurred_at, metadata) ON audit.events FROM aslan_app")
    op.execute("REVOKE SELECT (client_ip) ON audit.events FROM aslan_app")


def downgrade() -> None:
    # Restore the prior shape: helpers owned by aslan_app + column
    # GRANTs back to aslan_app + drop the helper-owner role.
    op.execute("GRANT SELECT (event_id, occurred_at, metadata) ON audit.events TO aslan_app")
    op.execute("GRANT SELECT (client_ip) ON audit.events TO aslan_app")
    op.execute(
        "ALTER FUNCTION audit.event_metadata_key_count("
        "p_event_id BIGINT, p_occurred_at TIMESTAMPTZ"
        ") OWNER TO aslan_app"
    )
    op.execute(
        "ALTER FUNCTION audit.event_client_ip_truncated("
        "p_event_id BIGINT, p_occurred_at TIMESTAMPTZ"
        ") OWNER TO aslan_app"
    )
    # Revoke aslan_audit_helpers privileges before dropping the role.
    op.execute(
        "REVOKE SELECT (event_id, occurred_at, metadata, client_ip) "
        "ON audit.events FROM aslan_audit_helpers"
    )
    op.execute("REVOKE USAGE ON SCHEMA audit FROM aslan_audit_helpers")
    op.execute("DROP ROLE IF EXISTS aslan_audit_helpers")
