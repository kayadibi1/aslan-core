"""audit schema + audit.events hypertable

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-30 00:02:00

System-of-record for "who did what to which row, when, before/after."

  - CREATE SCHEMA audit
  - CREATE TABLE audit.events with composite PK (event_id, occurred_at)
    so the chunking column is part of the PK as Timescale requires.
  - create_hypertable('audit.events', 'occurred_at',
                      chunk_time_interval => INTERVAL '7 days')
  - 4 indexes: actor+time, target+time, request_id (partial),
    ingestion_run_id (partial).

Retention chunks older than 2 years will be dropped by a cron in v1.0;
not enforced in v0.3.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS audit")
    op.execute("""
        CREATE TABLE audit.events (
            event_id          BIGSERIAL,
            occurred_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            actor_id          TEXT NOT NULL,
            actor_kind        TEXT NOT NULL
                CHECK (actor_kind IN ('user', 'service', 'system')),
            client_ip         INET,
            user_agent        TEXT,
            request_id        UUID,
            ingestion_run_id  BIGINT,
            operation         TEXT NOT NULL,
            target_schema     TEXT NOT NULL,
            target_table      TEXT NOT NULL,
            target_pk         JSONB NOT NULL,
            before            JSONB,
            after             JSONB,
            metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY (event_id, occurred_at)
        )
    """)
    op.execute(
        "SELECT create_hypertable('audit.events', 'occurred_at', "
        "chunk_time_interval => INTERVAL '7 days')"
    )
    op.execute("CREATE INDEX events_actor_time ON audit.events (actor_id, occurred_at DESC)")
    op.execute(
        "CREATE INDEX events_target ON audit.events (target_schema, target_table, occurred_at DESC)"
    )
    op.execute(
        "CREATE INDEX events_request_id ON audit.events (request_id) WHERE request_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX events_run "
        "ON audit.events (ingestion_run_id) "
        "WHERE ingestion_run_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit.events")
    op.execute("DROP SCHEMA IF EXISTS audit")
