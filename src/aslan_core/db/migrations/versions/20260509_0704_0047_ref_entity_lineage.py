"""ref.entity_lineage table for entity merge/split events (D10).

Revision ID: 0047
Revises: 0046
Create Date: 2026-05-09 07:04:00

Per ``docs/specs/bitemporal-research-api/SCOPE.md`` D10.

A merge of entities {B, C, ...} into entity A creates:

    1. A new row in ``ref.entity`` for A with later ``as_of`` and
       ``merged_from_entity_ids = '["B", "C", ...]'``.
    2. One row in ``ref.entity_lineage`` per (merged_from, merged_into)
       pair, recording the lineage event.

Splits are the inverse: B and C split off from A.

PIT queries traverse the lineage table to reconstruct historical
entity structure.

Reversibility: DROP TABLE; the lineage records are auxiliary — no
data loss in `ref.entity` itself.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0047"
down_revision: str | Sequence[str] | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE ref.entity_lineage (
            lineage_id          BIGSERIAL PRIMARY KEY,
            event_kind          TEXT NOT NULL CHECK (event_kind IN ('merge', 'split', 'rename', 'continuation')),
            from_entity_id      TEXT NOT NULL,
            into_entity_id      TEXT NOT NULL,
            event_at            TIMESTAMPTZ NOT NULL,
            as_of               TIMESTAMPTZ NOT NULL DEFAULT now(),
            reason              TEXT,
            evidence_filing_id  UUID,
            UNIQUE (event_kind, from_entity_id, into_entity_id, event_at, as_of)
        )
    """)
    op.execute(
        "CREATE INDEX entity_lineage_from_idx ON ref.entity_lineage (from_entity_id, event_at)"
    )
    op.execute(
        "CREATE INDEX entity_lineage_into_idx ON ref.entity_lineage (into_entity_id, event_at)"
    )
    op.execute(
        "CREATE INDEX entity_lineage_as_of_idx ON ref.entity_lineage (as_of)"
    )

    op.execute("""
        DROP TRIGGER IF EXISTS entity_lineage_no_update ON ref.entity_lineage
    """)
    op.execute("""
        CREATE TRIGGER entity_lineage_no_update
            BEFORE UPDATE ON ref.entity_lineage
            FOR EACH ROW
            EXECUTE FUNCTION aslan_core.reject_bitemporal_update_generic()
    """)

    op.execute("""
        INSERT INTO aslan_core.bitemporal_table_registry
            (schema_name, table_name, entity_columns, as_of_column,
             pit_function_name, api_path, exposed_in_api, notes)
        VALUES
            ('ref', 'entity_lineage',
             ARRAY['from_entity_id', 'into_entity_id', 'event_kind', 'event_at']::text[],
             'as_of',
             'ref.entity_lineage_at',
             NULL, false,
             'Auxiliary to ref.entity. Lineage records are bitemporal '
             'so a re-cast of a prior merge/split is itself a new as_of row.')
        ON CONFLICT (schema_name, table_name) DO UPDATE
        SET entity_columns = EXCLUDED.entity_columns,
            pit_function_name = EXCLUDED.pit_function_name,
            notes = EXCLUDED.notes
    """)

    op.execute("GRANT SELECT ON ref.entity_lineage TO aslan_dashboard")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON ref.entity_lineage FROM aslan_dashboard")
    op.execute("""
        DELETE FROM aslan_core.bitemporal_table_registry
            WHERE schema_name='ref' AND table_name='entity_lineage'
    """)
    op.execute("DROP TRIGGER IF EXISTS entity_lineage_no_update ON ref.entity_lineage")
    op.execute("DROP TABLE IF EXISTS ref.entity_lineage")
