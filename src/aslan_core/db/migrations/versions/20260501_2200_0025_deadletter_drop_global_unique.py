"""streams.deadletter_redis_index — drop global UNIQUE(redis_message_id).

Revision ID: 0025
Revises: 0024
Create Date: 2026-05-01 22:00:00

Migration 0017 introduced ``streams.deadletter_redis_index`` with a
table-wide ``UNIQUE (redis_message_id)`` framed as a "defensive
backstop against duplicate XADDs landing two index rows for the same
Redis message_id." That framing assumed Redis-stream ids are globally
unique — they are not.

A Redis-stream message id has shape ``<unix_ms>-<seq>`` where ``seq``
is per-stream state. Two distinct dead-letter streams (one per source
stream) XADDing inside the same millisecond can each return ``<ms>-0``.
Both inserts then carry the same ``redis_message_id`` for different
``failure_id`` values, the table-wide UNIQUE fires, and the routing
helper at ``streams/deadletter.py`` step-3 mistakes the cross-stream
collision for a same-``failure_id`` race. It then XDELs its own valid
XADD (silent dead-letter loss), fails to find a winner row keyed on
``failure_id``, and re-raises — propagating up through ``_route`` →
``consume()`` and crashing the consumer iterator.

The constraint is also redundant for its real purpose: ``failure_id``
is the table's primary key, which already prevents the same
``failure_id`` from being inserted twice. Within a single Redis stream
the per-stream ``seq`` counter is monotonic, so two concurrent workers
on the same ``failure_id`` (already blocked by the PK) cannot collide
on ``redis_message_id`` regardless. Cross-stream collisions on
``redis_message_id`` are *expected* by Redis design, not a defect to
guard against.

This migration drops the constraint by name. The constraint name
defaults to ``deadletter_redis_index_redis_message_id_key`` under
PostgreSQL's standard inline-UNIQUE naming. The DO block looks the
constraint up by shape (UNIQUE on a single column ``redis_message_id``
on this table) and drops whatever name it actually has, so the
migration is robust to a non-default name.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | Sequence[str] | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            cname TEXT;
        BEGIN
            SELECT c.conname INTO cname
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = 'streams'
              AND t.relname = 'deadletter_redis_index'
              AND c.contype = 'u'
              AND (
                  SELECT array_agg(a.attname ORDER BY a.attnum)
                  FROM unnest(c.conkey) k
                  JOIN pg_attribute a
                    ON a.attrelid = c.conrelid AND a.attnum = k
              ) = ARRAY['redis_message_id']::name[];
            IF cname IS NOT NULL THEN
                EXECUTE format(
                    'ALTER TABLE streams.deadletter_redis_index '
                    'DROP CONSTRAINT %I',
                    cname
                );
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE streams.deadletter_redis_index "
        "ADD CONSTRAINT deadletter_redis_index_redis_message_id_key "
        "UNIQUE (redis_message_id)"
    )
