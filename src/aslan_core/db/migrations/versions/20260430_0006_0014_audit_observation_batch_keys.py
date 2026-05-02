"""audit.observation_batch_keys — per-key bulk-write forensics (codex F3)

Revision ID: 0014
Revises: 0013
Create Date: 2026-04-30 00:06:00

Per-key forensic detail for bulk observation writes — keeps
``audit.events`` low-cardinality (one row per write_batch call) while
preserving full key-level traceability.

Lifecycle is enforced by:

  - identical ``chunk_time_interval=7 days`` to ``audit.events`` so
    retention drops in lockstep (codex F6)
  - same-transaction atomicity inside ``ObservationWriter.write`` —
    the audit.events row and its matching observation_batch_keys
    rows commit together or roll back together
  - constraint trigger ``batch_keys_parent_check`` (codex Batch 1 F2)
    rejects orphan rows and rows whose ``occurred_at`` doesn't match
    the parent event's; cascade trigger ``events_cascade_keys`` deletes
    matching keys when a parent event is deleted. Native composite-PK
    FKs to a Timescale hypertable were attempted first and rejected
    by Timescale ("hypertables cannot be used as foreign key references
    of hypertables"), so the trigger pair is the documented fallback.

CHECK constraints:

  - ``action IN ('inserted', 'unchanged')`` — only the two outcomes
    a Phase-2 ON CONFLICT DO NOTHING returning-set walk can record.
    Same-key different-payload raises ObservationConflict and rolls
    back the whole batch, so 'updated' / 'conflicted' actions never
    land here.
  - ``char_length(payload_hash) = 64`` — defense-in-depth on top of
    CHAR(64) so a writer bug feeding a short hex prefix is rejected
    at the storage boundary instead of silently right-padded.
  - ``payload_hash ~ '^[0-9a-f]{64}$'`` — codex Batch 1 F3, lowercase
    SHA-256 hex (64 hex chars). The canonical payload-hash contract
    in ``aslan_core.schemas.timeseries`` is hashlib.sha256(...).hexdigest()
    which is always lowercase; this CHECK rejects uppercase or
    non-hex strings before they reach the audit chunk.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE audit.observation_batch_keys (
            event_id          BIGINT NOT NULL,
            occurred_at       TIMESTAMPTZ NOT NULL,
            series_id         BIGINT NOT NULL,
            ts                TIMESTAMPTZ NOT NULL,
            as_of             TIMESTAMPTZ NOT NULL,
            payload_hash      CHAR(64) NOT NULL
                CHECK (char_length(payload_hash) = 64)
                CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
            action            TEXT NOT NULL CHECK (action IN ('inserted', 'unchanged')),
            PRIMARY KEY (event_id, occurred_at, series_id, ts, as_of)
        )
    """)
    op.execute(
        "SELECT create_hypertable('audit.observation_batch_keys', 'occurred_at', "
        "chunk_time_interval => INTERVAL '7 days')"
    )
    op.execute("CREATE INDEX obkeys_event ON audit.observation_batch_keys(event_id)")

    # codex Batch 1 F2 — Timescale rejects native FKs across hypertables
    # ("hypertables cannot be used as foreign key references of
    # hypertables"), so we enforce parent-row presence with a constraint
    # trigger and cascade-on-delete with a regular trigger on
    # audit.events. Both stay inside the same transaction as the writer's
    # INSERTs so a missing parent or mismatched occurred_at rolls back
    # the whole batch.
    op.execute("""
        CREATE OR REPLACE FUNCTION audit.observation_batch_keys_check_parent()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM audit.events
                WHERE event_id = NEW.event_id AND occurred_at = NEW.occurred_at
            ) THEN
                RAISE EXCEPTION
                    'audit.observation_batch_keys row references missing audit.events '
                    '(event_id=%, occurred_at=%)',
                    NEW.event_id, NEW.occurred_at
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER batch_keys_parent_check
        AFTER INSERT OR UPDATE ON audit.observation_batch_keys
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION audit.observation_batch_keys_check_parent();
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION audit.events_cascade_to_batch_keys()
        RETURNS TRIGGER AS $$
        BEGIN
            DELETE FROM audit.observation_batch_keys
            WHERE event_id = OLD.event_id AND occurred_at = OLD.occurred_at;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER events_cascade_keys
        AFTER DELETE ON audit.events
        FOR EACH ROW EXECUTE FUNCTION audit.events_cascade_to_batch_keys();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS events_cascade_keys ON audit.events")
    op.execute("DROP FUNCTION IF EXISTS audit.events_cascade_to_batch_keys()")
    op.execute("DROP TABLE IF EXISTS audit.observation_batch_keys")
    op.execute("DROP FUNCTION IF EXISTS audit.observation_batch_keys_check_parent()")
