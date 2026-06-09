"""ts.entity_quality_score append-only trigger + registry + PIT function.

Revision ID: 0050
Revises: 0049
Create Date: 2026-05-09 07:07:00

Discovered during Phase 2g shadow validation (2026-05-09):
``ts.entity_quality_score`` is Class A bitemporal (proper as_of PK)
but was missed in ``EXISTING_PATTERNS_AUDIT.md``. The H2 invariants
checker flags it via ``bitemporal_table_registry_completeness``.

This migration:
- Adds the BEFORE UPDATE trigger.
- Inserts a registry row.
- Creates the ``ts.entity_quality_score_at(p_as_of)`` PIT function.
- Flips ``exposed_in_api=true``.

Reversibility: trivial.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0050"
down_revision: str | Sequence[str] | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ENTITY_COLUMNS = (
    "entity_id",
    "period_end",
    "period_type",
    "consolidation",
    "currency_code",
    "accounting_standard",
    "restatement_basis",
    "cpi_base_date",
    "mapping_version",
)


def upgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_schema='ts' AND table_name='entity_quality_score') THEN
                EXECUTE $sql$
                    CREATE TRIGGER entity_quality_score_no_update
                        BEFORE UPDATE ON ts.entity_quality_score
                        FOR EACH ROW
                        EXECUTE FUNCTION aslan_core.reject_bitemporal_update_generic()
                $sql$;
            END IF;
        END $$;
    """)

    cols_array = "ARRAY[" + ", ".join(f"'{c}'" for c in _ENTITY_COLUMNS) + "]::text[]"
    op.execute(
        f"""
        INSERT INTO aslan_core.bitemporal_table_registry
            (schema_name, table_name, entity_columns, as_of_column,
             pit_function_name, api_path, exposed_in_api, notes)
        VALUES
            ('ts', 'entity_quality_score', {cols_array}, 'as_of',
             'ts.entity_quality_score_at',
             '/v1/research/quality-scores',
             true,
             'Discovered Phase 2g; same shape as ts.canonical_financial.')
        ON CONFLICT (schema_name, table_name) DO NOTHING
        """  # noqa: S608
    )

    op.execute("""
        CREATE OR REPLACE FUNCTION ts.entity_quality_score_at(p_as_of TIMESTAMPTZ)
        RETURNS SETOF ts.entity_quality_score
        LANGUAGE sql STABLE PARALLEL SAFE AS $$
            SELECT DISTINCT ON (entity_id, period_end, period_type,
                                consolidation, currency_code, accounting_standard,
                                restatement_basis, cpi_base_date, mapping_version) *
            FROM ts.entity_quality_score
            WHERE as_of <= p_as_of
            ORDER BY entity_id, period_end, period_type,
                     consolidation, currency_code, accounting_standard,
                     restatement_basis, cpi_base_date, mapping_version, as_of DESC;
        $$;
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS ts.entity_quality_score_at(TIMESTAMPTZ)")
    op.execute(
        "DELETE FROM aslan_core.bitemporal_table_registry "
        "WHERE schema_name='ts' AND table_name='entity_quality_score'"
    )
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_trigger t
                       JOIN pg_class c ON c.oid = t.tgrelid
                       JOIN pg_namespace n ON n.oid = c.relnamespace
                       WHERE n.nspname='ts' AND c.relname='entity_quality_score'
                         AND t.tgname='entity_quality_score_no_update'
                         AND NOT t.tgisinternal) THEN
                EXECUTE 'DROP TRIGGER entity_quality_score_no_update ON ts.entity_quality_score';
            END IF;
        END $$;
    """)
