"""Revoke raw client_ip + user_agent from aslan_dashboard.

Revision ID: 0022
Revises: 0021
Create Date: 2026-05-01 19:08:00

Codex branch-state review F-1 (CRITICAL): migration 0020's column-
allowlist GRANT included raw ``client_ip`` and ``user_agent`` on every
table the dashboard renders rows from (``streams.outbox``,
``doc.filing``, ``doc.filing_body``, ``audit.events``). The audit page
queries truncate ``client_ip`` to /24 (v4) or /48 (v6) at the SQL
layer — but per the spec's own threat model, the privilege layer is
the load-bearing GDPR boundary; a future in-process bypass that
reaches ``connection.exec_driver_sql("SELECT client_ip FROM ...")``
would still read the raw bytes.

This migration tightens the boundary:

  * REVOKE SELECT (client_ip, user_agent) on ``streams.outbox``,
    ``doc.filing``, ``doc.filing_body``, ``audit.events`` from
    ``aslan_dashboard``. Other tables with these columns
    (``streams.deadletter_log``, ``streams.redaction_registry``,
    ``src.ingestion_run``) never had ``client_ip`` / ``user_agent``
    granted in the first place.

  * Add a SECURITY DEFINER helper
    ``audit.event_client_ip_truncated(p_event_id BIGINT,
    p_occurred_at TIMESTAMPTZ)`` returning the host-truncated CIDR
    string. Owned by ``aslan_app`` so it runs with the role that
    holds SELECT on the underlying column. ``REVOKE EXECUTE FROM
    PUBLIC`` + ``GRANT EXECUTE TO aslan_dashboard`` per the
    SECURITY DEFINER lint contract.

  * Grant ``aslan_app`` column-level SELECT on ``audit.events.client_ip``
    so the helper body can read the column. Companion to migration
    0021's ``audit.events.metadata`` grant.

The dashboard's ``audit_recent`` query in ``aslan_core.dashboard.queries``
is updated in the same commit to call the helper instead of computing
the truncation inline against the raw column.

This migration only touches dashboard-role privileges and adds a
new SECURITY DEFINER helper — no DML, no schema changes to existing
tables, no risk to running consumers.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Revoke raw PII columns from aslan_dashboard ──────────────
    # ``table`` is from the static tuple below — never user input — so
    # the f-string is safe (PG also has no parameterised form for
    # REVOKE column lists).
    for table in (
        "streams.outbox",
        "doc.filing",
        "doc.filing_body",
        "audit.events",
    ):
        # ``table`` is from the static tuple above — never user input.
        # PG has no parameterised form for REVOKE column lists.
        sql = f"REVOKE SELECT (client_ip, user_agent) ON {table} FROM aslan_dashboard"  # noqa: S608
        op.execute(sql)

    # ── Grant aslan_app SELECT on audit.events.client_ip ─────────
    # The SECURITY DEFINER helper below runs as aslan_app and reads
    # client_ip — without this grant the helper body would fail with
    # ``permission denied for column "client_ip"`` (the same gap
    # codex round-9 missed for ``audit.events.metadata`` in 0020,
    # closed by 0021).
    op.execute("GRANT SELECT (client_ip) ON audit.events TO aslan_app")

    # ── SECURITY DEFINER: truncated client_ip ────────────────────
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit.event_client_ip_truncated(
            p_event_id BIGINT,
            p_occurred_at TIMESTAMPTZ
        )
          RETURNS TEXT
          LANGUAGE sql
          STABLE
          SECURITY DEFINER
          SET search_path = pg_catalog, pg_temp
          AS $func$
            SELECT
              COALESCE(
                host(network(set_masklen(
                  e.client_ip,
                  CASE family(e.client_ip) WHEN 4 THEN 24 ELSE 48 END
                )))
                || CASE family(e.client_ip) WHEN 4 THEN '/24' ELSE '/48' END,
                '<no client_ip>'
              )
            FROM audit.events e
            WHERE e.event_id = p_event_id
              AND e.occurred_at = p_occurred_at
          $func$
        """
    )
    op.execute(
        "ALTER FUNCTION audit.event_client_ip_truncated(BIGINT, TIMESTAMPTZ) OWNER TO aslan_app"
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION "
        "audit.event_client_ip_truncated(BIGINT, TIMESTAMPTZ) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "audit.event_client_ip_truncated(BIGINT, TIMESTAMPTZ) "
        "TO aslan_dashboard"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS audit.event_client_ip_truncated(BIGINT, TIMESTAMPTZ)")
    op.execute("REVOKE SELECT (client_ip) ON audit.events FROM aslan_app")
    for table in (
        "streams.outbox",
        "doc.filing",
        "doc.filing_body",
        "audit.events",
    ):
        op.execute(f"GRANT SELECT (client_ip, user_agent) ON {table} TO aslan_dashboard")
