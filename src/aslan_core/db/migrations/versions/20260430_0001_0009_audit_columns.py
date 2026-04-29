"""audit columns on mutation tables

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-30 00:01:00

Adds five nullable audit columns to every mutation table in the data
plane (per spec §2):

  actor_id     TEXT
  actor_kind   TEXT  CHECK (actor_kind IN ('user', 'service', 'system'))
  client_ip    INET
  user_agent   TEXT
  request_id   UUID

src.ingestion_run gets only actor_id + actor_kind — per-call fields
(client_ip, user_agent, request_id) don't apply to a per-run row.

NULL is the historical-write marker; v1.0 will flip these to NOT NULL
once every consumer is propagating actor identity. The CHECK constraint
on actor_kind is defined as a column-level CHECK so it is recorded
under the column itself in pg_attribute / information_schema.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Codex F2 (2026-04-29) — src.watermark IS in the full-attribution list
# (a watermark mutation has full per-call attribution).
_TABLES_FULL: tuple[str, ...] = (
    "ref.entity",
    "ref.identifier",
    "ref.entity_sector",
    "ref.entity_relationship",
    "doc.filing",
    "doc.filing_attachment",
    "doc.filing_body",
    "src.watermark",
)
# Per-run rows: only actor identity applies; per-call fields don't.
_TABLES_RUN_ONLY: tuple[str, ...] = ("src.ingestion_run",)


def upgrade() -> None:
    for tbl in _TABLES_FULL:
        op.execute(
            f"ALTER TABLE {tbl} "
            "ADD COLUMN actor_id    TEXT, "
            "ADD COLUMN actor_kind  TEXT "
            "  CHECK (actor_kind IN ('user', 'service', 'system')), "
            "ADD COLUMN client_ip   INET, "
            "ADD COLUMN user_agent  TEXT, "
            "ADD COLUMN request_id  UUID"
        )
    for tbl in _TABLES_RUN_ONLY:
        op.execute(
            f"ALTER TABLE {tbl} "
            "ADD COLUMN actor_id    TEXT, "
            "ADD COLUMN actor_kind  TEXT "
            "  CHECK (actor_kind IN ('user', 'service', 'system'))"
        )


def downgrade() -> None:
    for tbl in _TABLES_FULL:
        op.execute(
            f"ALTER TABLE {tbl} "
            "DROP COLUMN IF EXISTS request_id, "
            "DROP COLUMN IF EXISTS user_agent, "
            "DROP COLUMN IF EXISTS client_ip, "
            "DROP COLUMN IF EXISTS actor_kind, "
            "DROP COLUMN IF EXISTS actor_id"
        )
    for tbl in _TABLES_RUN_ONLY:
        op.execute(
            f"ALTER TABLE {tbl} DROP COLUMN IF EXISTS actor_kind, DROP COLUMN IF EXISTS actor_id"
        )
