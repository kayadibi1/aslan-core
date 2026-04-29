"""streams.outbox table + indexes (v0.5.0 — Task 4).

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-20 00:02:00

The producer (StreamProducer; Tasks 8-9) writes to this table inside
the caller's transaction; the drainer (drain_outbox; Task 10) scans
the ``outbox_pending`` partial index and XADDs to Redis. The
``event_id UUID NOT NULL UNIQUE`` constraint is the end-to-end dedup
anchor (codex spec §2). Audit columns are stamped IN the original
INSERT — never via a post-write UPDATE (v0.3 contract carried forward).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE streams.outbox (
            outbox_id          BIGSERIAL PRIMARY KEY,
            stream_name        TEXT NOT NULL,
            event_id           UUID NOT NULL UNIQUE,
            schema_version     INTEGER NOT NULL,
            payload            JSONB NOT NULL,
            producer_run_id    BIGINT NOT NULL
                                   REFERENCES src.ingestion_run(ingestion_run_id),
            source_id          TEXT NOT NULL REFERENCES src.source(source_id),
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            published_at       TIMESTAMPTZ,
            redis_message_id   TEXT,
            publish_attempts   INTEGER NOT NULL DEFAULT 0,
            last_attempt_at    TIMESTAMPTZ,
            last_error         TEXT,
            actor_id           TEXT,
            actor_kind         TEXT
                CHECK (actor_kind IN ('user','service','system')),
            client_ip          INET,
            user_agent         TEXT,
            request_id         UUID
        )
    """)
    op.execute("""
        CREATE INDEX outbox_pending ON streams.outbox(created_at)
            WHERE published_at IS NULL
    """)
    op.execute("CREATE INDEX outbox_event_id ON streams.outbox(event_id)")
    op.execute("CREATE INDEX outbox_stream_name ON streams.outbox(stream_name, created_at)")
    op.execute("CREATE INDEX outbox_run ON streams.outbox(producer_run_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS streams.outbox")
