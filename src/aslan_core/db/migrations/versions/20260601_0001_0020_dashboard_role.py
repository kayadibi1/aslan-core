"""dashboard role + column-allowlist GRANTs + audit metadata-key-count helper.

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-01 00:01:00

Creates the read-only ``aslan_dashboard`` role for the v0.6.0 internal
ops dashboard. Per spec at ``crawl/plans/aslan-core-v0.6.0-dashboard.md``
@ ``f261e1c`` (post 10 codex review rounds).

The role:

* LOGINs with its own password (read from env at migration time);
* has SELECT only on tables / columns the dashboard renders;
* has NO INSERT/UPDATE/DELETE/TRUNCATE anywhere;
* has NO EXECUTE on application SECURITY DEFINER functions except the
  read-only metadata-count helper below;
* has ``default_transaction_read_only=on`` as a soft floor.

The single SECURITY DEFINER helper exposes only a key COUNT for an
``audit.events`` row — letting the dashboard render
``metadata_key_count`` without a SELECT on the (otherwise revoked)
``audit.events.metadata`` column. Owned by ``aslan_app`` so SECURITY
DEFINER runs with the role that has SELECT on ``metadata``.

Password handling (codex plan-rounds 1-3):

1. SQLAlchemy bind placeholders (``:name``) are NOT parsed inside
   dollar-quoted PostgreSQL bodies, AND ``SET LOCAL`` does NOT accept
   bind parameters either. The pattern that works: call PG's
   ``set_config('name', value, is_local=true)`` function via
   ``op.execute(text(... :pw ...).bindparams(pw=...))`` — bind IS
   parsed in a function-call argument position.
2. Inside a separate DO block, ``current_setting('aslan.dashboard_password')``
   reads the GUC and ``format(..., %L)`` does server-side literal
   escaping for ``CREATE ROLE``.
3. ``set_config(name, value, true)`` is transaction-scoped, releasing
   the GUC at COMMIT — never persists in pg_settings or
   pg_stat_statements as a session setting.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    password = os.environ.get("ASLAN_DASHBOARD_PASSWORD")
    if not password:
        env = os.environ.get("ASLAN_ENV", "dev")
        if env != "dev":
            raise RuntimeError(
                "ASLAN_DASHBOARD_PASSWORD is required in non-dev environments. "
                "Generate a strong password and set it before running migration 0020. "
                "See docs/dashboard.md for the deployment runbook."
            )
        password = "DEV_ONLY_REPLACE_ME"  # noqa: S105 — intentional dev fallback

    op.execute(
        text("SELECT set_config('aslan.dashboard_password', :pw, true)").bindparams(pw=password)
    )
    op.execute(
        """
        DO $$
        BEGIN
            EXECUTE format(
                'CREATE ROLE aslan_dashboard LOGIN PASSWORD %L',
                current_setting('aslan.dashboard_password')
            );
        END
        $$
        """
    )

    op.execute("ALTER ROLE aslan_dashboard SET default_transaction_read_only = on")

    op.execute("GRANT USAGE ON SCHEMA streams, src, doc, ts, audit, ref, agg TO aslan_dashboard")

    # Tables with NO forbidden columns: explicit table-level SELECT.
    for tbl in (
        "streams.event_id_to_redis",
        "streams.deadletter_redis_index",
        "streams.deadletter_xadd_intent",
        "src.source",
        "ts.series_catalog",
        "ts.observation",
        "audit.observation_batch_keys",
        "ref.entity",
        "ref.identifier",
        "ref.currency",
        "ref.sector",
        "doc.filing_attachment",
    ):
        op.execute(f"GRANT SELECT ON {tbl} TO aslan_dashboard")

    # Tables with forbidden columns: column-allowlist GRANT only.
    op.execute(
        """
        GRANT SELECT (
            outbox_id, stream_name, event_id, schema_version,
            producer_run_id, source_id, created_at, published_at,
            redis_message_id, publish_attempts, last_attempt_at,
            actor_id, actor_kind, client_ip, user_agent, request_id
        ) ON streams.outbox TO aslan_dashboard
        """
    )
    op.execute(
        """
        GRANT SELECT (
            failure_id, stream_name, deadletter_stream, event_id,
            original_message_id, group_name, consumer_name,
            failure_count, routed_at, routed_at_redis, redis_message_id,
            actor_id, actor_kind, request_id
        ) ON streams.deadletter_log TO aslan_dashboard
        """
    )
    op.execute(
        """
        GRANT SELECT (
            event_id, redaction_reason, redacted_at, original_stream,
            redacted_payload_hash, original_payload_hash,
            actor_id, actor_kind, request_id
        ) ON streams.redaction_registry TO aslan_dashboard
        """
    )
    op.execute(
        """
        GRANT SELECT (
            ingestion_run_id, source_id, job_name, started_at, finished_at,
            status, error_count, rows_written, docs_written, bytes_written,
            config_hash, metadata, actor_id, actor_kind
        ) ON src.ingestion_run TO aslan_dashboard
        """
    )
    op.execute(
        """
        GRANT SELECT (
            filing_id, source_id, source_filing_ref, entity_id,
            kind, subkind, language, published_at, period_start, period_end,
            source_url, is_amendment, previous_filing_id,
            primary_object_key, primary_mime, primary_sha256, primary_bytes,
            extracted_text_key, has_xbrl, xbrl_object_key, metadata,
            ingestion_run_id, discovered_at, revision_no,
            actor_id, actor_kind, client_ip, user_agent, request_id
        ) ON doc.filing TO aslan_dashboard
        """
    )
    op.execute(
        """
        GRANT SELECT (
            filing_id, body_lang, extracted_at,
            actor_id, actor_kind, client_ip, user_agent, request_id
        ) ON doc.filing_body TO aslan_dashboard
        """
    )
    op.execute(
        """
        GRANT SELECT (
            event_id, occurred_at, actor_id, actor_kind, client_ip,
            user_agent, request_id, ingestion_run_id,
            operation, target_schema, target_table, target_pk
        ) ON audit.events TO aslan_dashboard
        """
    )

    # SECURITY DEFINER helper: count keys in an audit.events.metadata
    # row WITHOUT exposing the metadata content. Owned by aslan_app so
    # the function runs with SELECT on the (otherwise forbidden) column.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit.event_metadata_key_count(
            p_event_id BIGINT,
            p_occurred_at TIMESTAMPTZ
        )
          RETURNS INTEGER
          LANGUAGE sql
          STABLE
          SECURITY DEFINER
          SET search_path = pg_catalog, pg_temp
          AS $func$
            SELECT count(*)::INTEGER
            FROM audit.events e, jsonb_object_keys(e.metadata)
            WHERE e.event_id = p_event_id AND e.occurred_at = p_occurred_at
          $func$
        """
    )
    op.execute(
        "ALTER FUNCTION audit.event_metadata_key_count(BIGINT, TIMESTAMPTZ) OWNER TO aslan_app"
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION audit.event_metadata_key_count(BIGINT, TIMESTAMPTZ) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "audit.event_metadata_key_count(BIGINT, TIMESTAMPTZ) "
        "TO aslan_dashboard"
    )

    # Defensive re-assertion: the v0.5 SECURITY DEFINER must NOT be
    # callable by aslan_dashboard. Migration 0018 already revokes from
    # PUBLIC at creation; re-issue to be explicit at v0.6.0 boundary.
    op.execute(
        "REVOKE EXECUTE ON FUNCTION "
        "streams.redaction_registry_insert("
        "uuid, text, timestamptz, text, jsonb, char, char"
        ") FROM PUBLIC, aslan_dashboard"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS audit.event_metadata_key_count(BIGINT, TIMESTAMPTZ)")
    # PG cannot DROP a role while ANY object grants privilege to it.
    # ``DROP OWNED BY ... CASCADE`` revokes every grant on every object
    # in any database the role can reach. Required before DROP ROLE.
    op.execute("DROP OWNED BY aslan_dashboard CASCADE")
    op.execute("DROP ROLE IF EXISTS aslan_dashboard")
