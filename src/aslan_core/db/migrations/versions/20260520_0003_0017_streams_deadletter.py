"""streams.deadletter_log + deadletter_redis_index + deadletter_xadd_intent.

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-20 00:03:00

Three tables in one migration (codex F9 round 4 introduced
``deadletter_redis_index``; F14 round 6 + F17 round 7 + F20 round 9 +
F21 round 10 introduced ``deadletter_xadd_intent``). Per the spec all
three are part of the "dead-letter routing" subsystem and ship
together; splitting across separate migrations would force an
intermediate inconsistent state.

The ``streams.deadletter_log`` table includes the UNIQUE
``(stream_name, group_name, original_message_id)`` constraint that
anchors the F18 ``ON CONFLICT DO UPDATE ... RETURNING (xmax = 0)``
step-1 contract.

The ``streams.deadletter_redis_index`` UNIQUE
``(redis_message_id)`` is the defensive backstop against duplicate
XADDs landing two index rows for the same Redis message_id.

The ``streams.deadletter_xadd_intent`` carries
``redis_lower_bound_id TEXT NOT NULL`` (F17) +
``owner_id TEXT NOT NULL`` + ``owner_heartbeat_at TIMESTAMPTZ NOT NULL
DEFAULT now()`` (F20) + the two indexes (``_stale ON intent_at``,
``_heartbeat ON owner_heartbeat_at``) for the janitor pass-1 +
staleness-adoption queries.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE streams.deadletter_log (
            failure_id          BIGSERIAL PRIMARY KEY,
            stream_name         TEXT NOT NULL,
            deadletter_stream   TEXT NOT NULL,
            event_id            UUID NOT NULL,
            original_message_id TEXT NOT NULL,
            group_name          TEXT NOT NULL,
            consumer_name       TEXT NOT NULL,
            failure_count       INTEGER NOT NULL,
            last_error          TEXT NOT NULL,
            payload_excerpt     JSONB,
            routed_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            routed_at_redis     TIMESTAMPTZ,
            redis_message_id    TEXT,
            actor_id            TEXT,
            actor_kind          TEXT
                CHECK (actor_kind IN ('user','service','system')),
            request_id          UUID,
            CONSTRAINT deadletter_routing_uq
                UNIQUE (stream_name, group_name, original_message_id)
        )
    """)
    op.execute("CREATE INDEX deadletter_event_id ON streams.deadletter_log(event_id)")
    op.execute(
        "CREATE INDEX deadletter_stream_routed ON streams.deadletter_log(stream_name, routed_at)"
    )
    op.execute("""
        CREATE INDEX deadletter_pending_redis
        ON streams.deadletter_log(failure_id)
        WHERE routed_at_redis IS NULL
    """)

    # F9 round 4 — durable Postgres-side recovery anchor.
    op.execute("""
        CREATE TABLE streams.deadletter_redis_index (
            failure_id          BIGINT PRIMARY KEY
                                   REFERENCES streams.deadletter_log(failure_id)
                                   ON DELETE CASCADE,
            redis_message_id    TEXT NOT NULL,
            UNIQUE (redis_message_id)
        )
    """)

    # F14/F17/F20 — pre-XADD intent cursor with Redis-ID lower bound +
    # owner-identity + heartbeat.
    op.execute("""
        CREATE TABLE streams.deadletter_xadd_intent (
            failure_id            BIGINT PRIMARY KEY
                                     REFERENCES streams.deadletter_log(failure_id)
                                     ON DELETE CASCADE,
            stream_name           TEXT NOT NULL,
            intent_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
            redis_lower_bound_id  TEXT NOT NULL,
            owner_id              TEXT NOT NULL,
            owner_heartbeat_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX deadletter_xadd_intent_stale ON streams.deadletter_xadd_intent(intent_at)"
    )
    op.execute(
        "CREATE INDEX deadletter_xadd_intent_heartbeat "
        "ON streams.deadletter_xadd_intent(owner_heartbeat_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS streams.deadletter_xadd_intent")
    op.execute("DROP TABLE IF EXISTS streams.deadletter_redis_index")
    op.execute("DROP TABLE IF EXISTS streams.deadletter_log")
