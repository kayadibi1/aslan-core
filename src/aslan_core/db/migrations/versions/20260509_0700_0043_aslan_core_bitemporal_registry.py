"""aslan_core schema + bitemporal_table_registry + reject_bitemporal_update template.

Revision ID: 0043
Revises: 0042
Create Date: 2026-05-09 07:00:00

Per ``docs/specs/bitemporal-research-api/SCOPE.md`` D28 and
``bitemporal-api-prompt.md`` Phase 2c.

Creates:
    1. The ``aslan_core`` schema if it does not already exist (it does
       — alembic_version is in ``public``; the application uses
       ``aslan_core`` for bookkeeping tables that the bitemporal API
       owns).
    2. ``aslan_core.bitemporal_table_registry`` — the registry that
       lists every bitemporal-disciplined table. Used by:
       - ``scripts/check_bitemporal_invariants.py`` to enumerate
         invariants;
       - ``scripts/check_bitemporal_table_registry.py`` (CI) to enforce
         that every ``as_of``-bearing table has a registry row;
       - the bitemporal-research-API to enumerate exposed tables.
    3. ``aslan_core.reject_bitemporal_update_generic()`` — the trigger
       function applied to every bitemporal table per H1. It raises
       ``feature_not_supported`` with the standard hint. Per-table
       triggers reference this function with their own table name in
       the error message.

Downgrade drops the trigger function, the registry table, and (only
if it was created here) the ``aslan_core`` schema.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0043"
down_revision: str | Sequence[str] | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS aslan_core")

    op.execute("""
        CREATE TABLE aslan_core.bitemporal_table_registry (
            schema_name        TEXT NOT NULL,
            table_name         TEXT NOT NULL,
            entity_columns     TEXT[] NOT NULL,
            as_of_column       TEXT NOT NULL DEFAULT 'as_of',
            pit_function_name  TEXT NOT NULL,
            api_path           TEXT,
            exposed_in_api     BOOLEAN NOT NULL DEFAULT false,
            registered_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            notes              TEXT,
            PRIMARY KEY (schema_name, table_name)
        )
    """)
    op.execute(
        "COMMENT ON TABLE aslan_core.bitemporal_table_registry IS "
        "'Per SCOPE.md D28: every bitemporal-disciplined table is "
        "listed here. CI fails if a table has an as_of column but no "
        "registry row.'"
    )

    op.execute("""
        CREATE OR REPLACE FUNCTION aslan_core.reject_bitemporal_update_generic()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'bitemporal table %.% is append-only; UPDATE rejected. '
                'Insert a new (entity, as_of) row instead.',
                TG_TABLE_SCHEMA, TG_TABLE_NAME
                USING ERRCODE = 'feature_not_supported',
                      HINT = 'Bitemporal tables advance via INSERT of new '
                             'as_of rows; existing rows are immutable per '
                             'ADR-003 Moat 2.';
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute(
        "COMMENT ON FUNCTION aslan_core.reject_bitemporal_update_generic() IS "
        "'Per SCOPE.md H1 / ADR-003 Moat 2: applied as a BEFORE UPDATE "
        "trigger on every bitemporal table to enforce append-only at the "
        "schema level.'"
    )

    op.execute("GRANT USAGE ON SCHEMA aslan_core TO aslan_dashboard")
    op.execute("GRANT SELECT ON aslan_core.bitemporal_table_registry TO aslan_dashboard")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON aslan_core.bitemporal_table_registry FROM aslan_dashboard")
    op.execute("REVOKE USAGE ON SCHEMA aslan_core FROM aslan_dashboard")
    op.execute("DROP FUNCTION IF EXISTS aslan_core.reject_bitemporal_update_generic()")
    op.execute("DROP TABLE IF EXISTS aslan_core.bitemporal_table_registry")
    # We do NOT drop the aslan_core schema on downgrade because subsequent
    # migrations (api_key et al.) own tables in it; an in-flight downgrade
    # past 0043 should be preceded by their own downgrades.
