"""Append-only triggers + bitemporal_table_registry rows for Class A tables.

Revision ID: 0044
Revises: 0043
Create Date: 2026-05-09 07:01:00

Per ``docs/specs/bitemporal-research-api/SCOPE.md`` D1 / D11 and
``bitemporal-api-prompt.md`` H1.

Adds BEFORE UPDATE triggers (rejecting the UPDATE) to every Class A
bitemporal table per ``EXISTING_PATTERNS_AUDIT.md``:

    - ts.observation
    - ts.financial_line_item
    - ts.canonical_financial
    - agg.filing_event   *** see CROSS-REPO COORDINATION below ***
    - ref.identifier

For each, also inserts a row into
``aslan_core.bitemporal_table_registry`` so the H2 invariants checker
finds it.

CROSS-REPO COORDINATION — agg.filing_event
==========================================
``aslan-event-extractor/SCOPE.md`` D6 + the existing
``agg.filing_event_current`` view rely on ``superseded_at`` being
UPDATE-able to mark a prior version as superseded. With the
append-only trigger, that path is broken; ``aslan-event-extractor``
M1 must adopt new-row supersession instead. This is a HANDOFF.md
coordination item flagged for the aslan-event-extractor maintainer.

Because ``agg.filing_event`` is currently empty in production
(M0 stage; STATE.md confirms 0 rows in the LLM-extraction tables),
applying the trigger now incurs zero runtime impact and forces the
M1 work to adopt the correct discipline from day one.

Reversibility: trivial — DROP TRIGGER. Data unaffected.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0044"
down_revision: str | Sequence[str] | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CLASS_A_TABLES = (
    # (schema, table, entity_columns, as_of_column, pit_function_name, api_path)
    (
        "ts",
        "observation",
        ("series_id", "ts"),
        "as_of",
        "ts.observation_at",
        "/v1/research/observations",
    ),
    (
        "ts",
        "financial_line_item",
        (
            "entity_id",
            "filing_id",
            "statement_type",
            "line_code",
            "consolidation",
            "period_end",
        ),
        "as_of",
        "ts.financial_line_item_at",
        "/v1/research/financials/line-items",
    ),
    (
        "ts",
        "canonical_financial",
        (
            "entity_id",
            "canonical_code",
            "period_end",
            "period_type",
            "consolidation",
            "currency_code",
            "accounting_standard",
            "restatement_basis",
            "cpi_base_date",
            "mapping_version",
        ),
        "as_of",
        "ts.canonical_financial_at",
        "/v1/research/financials/canonical",
    ),
    (
        "agg",
        "filing_event",
        ("filing_id", "event_type", "event_seq"),
        "as_of",
        "agg.filing_event_at",
        "/v1/research/events",
    ),
    (
        "ref",
        "identifier",
        ("namespace", "value"),
        "as_of",
        "ref.identifier_at",
        "/v1/research/identifiers/resolve",
    ),
)


def _create_trigger_sql(schema: str, table: str) -> str:
    """Return the SQL to create the BEFORE UPDATE trigger on a table.

    Trigger naming convention is ``<table>_no_update`` per SCOPE.md
    D28 / the registry's contract.
    """
    return f"""
        CREATE TRIGGER {table}_no_update
            BEFORE UPDATE ON {schema}.{table}
            FOR EACH ROW
            EXECUTE FUNCTION aslan_core.reject_bitemporal_update_generic();
    """


def upgrade() -> None:
    for schema, table, entity_cols, as_of_col, pit_fn, api_path in CLASS_A_TABLES:
        # Defensive: skip if table doesn't exist in this environment.
        # Useful when the migration is applied to a shadow DB that's missing
        # some tables. In production the tables all exist.
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = '{schema}' AND table_name = '{table}'
                ) THEN
                    EXECUTE $sql$
                        CREATE TRIGGER {table}_no_update
                            BEFORE UPDATE ON {schema}.{table}
                            FOR EACH ROW
                            EXECUTE FUNCTION aslan_core.reject_bitemporal_update_generic()
                    $sql$;
                END IF;
            END $$;
            """
        )

        entity_array = "ARRAY[" + ", ".join(f"'{c}'" for c in entity_cols) + "]::text[]"
        op.execute(
            f"""
            INSERT INTO aslan_core.bitemporal_table_registry
                (schema_name, table_name, entity_columns, as_of_column,
                 pit_function_name, api_path, exposed_in_api, notes)
            VALUES
                ('{schema}', '{table}', {entity_array}, '{as_of_col}',
                 '{pit_fn}', '{api_path}', false,
                 'Phase 2 enrolment; PIT function lands in 0049; ' ||
                 'exposed_in_api flips on Phase 3 deploy.')
            ON CONFLICT (schema_name, table_name) DO NOTHING
            """
        )


def downgrade() -> None:
    for schema, table, _, _, _, _ in CLASS_A_TABLES:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_trigger t
                    JOIN pg_class c ON c.oid = t.tgrelid
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = '{schema}'
                      AND c.relname = '{table}'
                      AND t.tgname = '{table}_no_update'
                      AND NOT t.tgisinternal
                ) THEN
                    EXECUTE 'DROP TRIGGER {table}_no_update ON {schema}.{table}';
                END IF;
            END $$;
            """
        )
        op.execute(
            f"""
            DELETE FROM aslan_core.bitemporal_table_registry
            WHERE schema_name = '{schema}' AND table_name = '{table}'
            """
        )
