"""streams.event_id_to_redis (codex F3 — drainer-populated index).

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-20 00:05:00

The drainer (Task 10) inserts one row here for every successful XADD,
inside the SAME Postgres transaction as the outbox ``published_at``
UPDATE. The redaction runtime (Task 18) queries this table by
``event_id`` to find every in-Redis copy of an event so the appropriate
XDELs can be issued.

Composite PK ``(event_id, stream_name, redis_message_id)`` because a
single event_id can land in MULTIPLE Redis stream entries (drainer-retry
producing two XADDs; future fan-out to multiple consumer streams).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE streams.event_id_to_redis (
            event_id          UUID NOT NULL,
            stream_name       TEXT NOT NULL,
            redis_message_id  TEXT NOT NULL,
            published_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            redacted_at       TIMESTAMPTZ,
            PRIMARY KEY (event_id, stream_name, redis_message_id)
        )
    """)
    op.execute("CREATE INDEX event_id_to_redis_event_id ON streams.event_id_to_redis(event_id)")
    op.execute("""
        CREATE INDEX event_id_to_redis_pending
        ON streams.event_id_to_redis(stream_name, published_at)
        WHERE redacted_at IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS streams.event_id_to_redis")
