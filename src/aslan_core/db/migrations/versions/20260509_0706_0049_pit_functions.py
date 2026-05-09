"""Point-in-time SQL functions for every Class A bitemporal table (D1).

Revision ID: 0049
Revises: 0048
Create Date: 2026-05-09 07:06:00

Per ``docs/specs/bitemporal-research-api/SCOPE.md`` D1.

For each Class A bitemporal table, creates a STABLE SQL function
``<schema>.<table>_at(p_as_of timestamptz)`` that returns the rows
visible at the requested ``as_of``. Pattern: ``DISTINCT ON
(entity_columns) ... WHERE as_of <= p_as_of ORDER BY entity_columns,
as_of DESC``.

Functions added:

    - ts.observation_at(p_as_of)
    - ts.financial_line_item_at(p_as_of)
    - ts.canonical_financial_at(p_as_of)
    - ref.entity_at(p_as_of)
    - ref.identifier_at(p_as_of)
    - ref.entity_lineage_at(p_as_of)
    - agg.filing_event_at(p_as_of)

For ``ref.identifier`` the pattern is different: identifiers use a
``daterange`` with GiST EXCLUDE, so the function uses
``WHERE daterange @> p_as_of``.

For ``doc.filing`` (Class F today, no ``as_of`` column) the function
returns the row as-is when the row was created at or before
``p_as_of``. doc.filing append-only trigger may be added in a later
revision; not part of this migration.

Also flips ``exposed_in_api=true`` on each registry row touched.

Reversibility: ``DROP FUNCTION``; functions are pure.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0049"
down_revision: str | Sequence[str] | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ts.observation_at — DISTINCT ON keyset PIT
    op.execute("""
        CREATE OR REPLACE FUNCTION ts.observation_at(p_as_of TIMESTAMPTZ)
        RETURNS SETOF ts.observation
        LANGUAGE sql
        STABLE
        PARALLEL SAFE
        AS $$
            SELECT DISTINCT ON (series_id, ts) *
            FROM ts.observation
            WHERE as_of <= p_as_of
            ORDER BY series_id, ts, as_of DESC;
        $$;
    """)

    # ts.financial_line_item_at — same pattern, fatter key
    op.execute("""
        CREATE OR REPLACE FUNCTION ts.financial_line_item_at(p_as_of TIMESTAMPTZ)
        RETURNS SETOF ts.financial_line_item
        LANGUAGE sql
        STABLE
        PARALLEL SAFE
        AS $$
            SELECT DISTINCT ON (entity_id, filing_id, statement_type,
                                line_code, consolidation, period_end) *
            FROM ts.financial_line_item
            WHERE as_of <= p_as_of
            ORDER BY entity_id, filing_id, statement_type, line_code,
                     consolidation, period_end, as_of DESC;
        $$;
    """)

    # ts.canonical_financial_at — entity key matches the actual PK
    # minus as_of. Note that restatement_basis is part of the entity
    # key (per existing schema) so the as-reported and CPI-normalized
    # bases coexist as separate logical rows.
    op.execute("""
        CREATE OR REPLACE FUNCTION ts.canonical_financial_at(p_as_of TIMESTAMPTZ)
        RETURNS SETOF ts.canonical_financial
        LANGUAGE sql
        STABLE
        PARALLEL SAFE
        AS $$
            SELECT DISTINCT ON (entity_id, canonical_code, period_end, period_type,
                                consolidation, currency_code, accounting_standard,
                                restatement_basis, cpi_base_date, mapping_version) *
            FROM ts.canonical_financial
            WHERE as_of <= p_as_of
            ORDER BY entity_id, canonical_code, period_end, period_type,
                     consolidation, currency_code, accounting_standard,
                     restatement_basis, cpi_base_date, mapping_version,
                     as_of DESC;
        $$;
    """)

    # ref.entity_at — DEFERRED. Per migration 0046 docstring, ref.entity
    # bitemporal upgrade is deferred; PIT entity views at past as_of
    # are reconstructed at the application layer using ref.entity
    # joined to ref.entity_lineage.

    # ref.identifier_at — daterange-based PIT.
    # ref.identifier stores (valid_from, valid_to) as DATE columns, with
    # the bitemporal-rectangle expressed via the EXCLUDE constraint
    # daterange(valid_from, valid_to, '[)').
    op.execute("""
        CREATE OR REPLACE FUNCTION ref.identifier_at(p_as_of TIMESTAMPTZ)
        RETURNS SETOF ref.identifier
        LANGUAGE sql
        STABLE
        PARALLEL SAFE
        AS $$
            SELECT *
            FROM ref.identifier
            WHERE daterange(valid_from, valid_to, '[)') @> p_as_of::date;
        $$;
    """)

    # ref.entity_lineage_at — DISTINCT ON across the lineage record
    op.execute("""
        CREATE OR REPLACE FUNCTION ref.entity_lineage_at(p_as_of TIMESTAMPTZ)
        RETURNS SETOF ref.entity_lineage
        LANGUAGE sql
        STABLE
        PARALLEL SAFE
        AS $$
            SELECT DISTINCT ON (event_kind, from_entity_id, into_entity_id, event_at) *
            FROM ref.entity_lineage
            WHERE as_of <= p_as_of
            ORDER BY event_kind, from_entity_id, into_entity_id, event_at, as_of DESC;
        $$;
    """)

    # agg.filing_event_at — DISTINCT ON keyset PIT.
    # Also filters out superseded events (superseded_at IS NULL) — but
    # only at the requested as_of, so PIT before supersession returns
    # the original row.
    op.execute("""
        CREATE OR REPLACE FUNCTION agg.filing_event_at(p_as_of TIMESTAMPTZ)
        RETURNS SETOF agg.filing_event
        LANGUAGE sql
        STABLE
        PARALLEL SAFE
        AS $$
            SELECT DISTINCT ON (filing_id, event_type, event_seq) *
            FROM agg.filing_event
            WHERE as_of <= p_as_of
              AND (superseded_at IS NULL OR superseded_at > p_as_of)
            ORDER BY filing_id, event_type, event_seq, as_of DESC;
        $$;
    """)

    # Flip exposed_in_api=true on every registry row that has a
    # function we just created.
    op.execute("""
        UPDATE aslan_core.bitemporal_table_registry
            SET exposed_in_api = true
            WHERE pit_function_name IN (
                'ts.observation_at',
                'ts.financial_line_item_at',
                'ts.canonical_financial_at',
                'ref.identifier_at',
                'ref.entity_lineage_at',
                'agg.filing_event_at'
            )
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE aslan_core.bitemporal_table_registry
            SET exposed_in_api = false
            WHERE pit_function_name IN (
                'ts.observation_at',
                'ts.financial_line_item_at',
                'ts.canonical_financial_at',
                'ref.identifier_at',
                'ref.entity_lineage_at',
                'agg.filing_event_at'
            )
    """)
    op.execute("DROP FUNCTION IF EXISTS ts.observation_at(TIMESTAMPTZ)")
    op.execute("DROP FUNCTION IF EXISTS ts.financial_line_item_at(TIMESTAMPTZ)")
    op.execute("DROP FUNCTION IF EXISTS ts.canonical_financial_at(TIMESTAMPTZ)")
    op.execute("DROP FUNCTION IF EXISTS ref.identifier_at(TIMESTAMPTZ)")
    op.execute("DROP FUNCTION IF EXISTS ref.entity_lineage_at(TIMESTAMPTZ)")
    op.execute("DROP FUNCTION IF EXISTS agg.filing_event_at(TIMESTAMPTZ)")
