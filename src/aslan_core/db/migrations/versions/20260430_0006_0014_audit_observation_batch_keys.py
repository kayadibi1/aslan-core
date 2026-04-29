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
  - join key ``(event_id, occurred_at)`` rather than a Postgres FK
    (FKs on Timescale composite-PK hypertables are awkward and the
    chunk-interval + same-tx atomicity already enforce the contract)

CHECK constraints:

  - ``action IN ('inserted', 'unchanged')`` — only the two outcomes
    a Phase-2 ON CONFLICT DO NOTHING returning-set walk can record.
    Same-key different-payload raises ObservationConflict and rolls
    back the whole batch, so 'updated' / 'conflicted' actions never
    land here.
  - ``char_length(payload_hash) = 64`` — defense-in-depth on top of
    CHAR(64) so a writer bug feeding a short hex prefix is rejected
    at the storage boundary instead of silently right-padded.
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
            payload_hash      CHAR(64) NOT NULL CHECK (char_length(payload_hash) = 64),
            action            TEXT NOT NULL CHECK (action IN ('inserted', 'unchanged')),
            PRIMARY KEY (event_id, occurred_at, series_id, ts, as_of)
        )
    """)
    op.execute(
        "SELECT create_hypertable('audit.observation_batch_keys', 'occurred_at', "
        "chunk_time_interval => INTERVAL '7 days')"
    )
    op.execute("CREATE INDEX obkeys_event ON audit.observation_batch_keys(event_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit.observation_batch_keys")
