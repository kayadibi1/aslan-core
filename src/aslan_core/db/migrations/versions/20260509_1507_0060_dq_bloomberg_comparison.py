"""dq M-AU-07: bloomberg_comparison_run + bloomberg_comparison_cell

Revision ID: 0060
Revises: 0059
Create Date: 2026-05-09 15:07:00

Spec §5.6 — Bloomberg-comparison surface. The aslan-team operates a
quarterly rotation: open a run at the start of each quarter, manually
enter Bloomberg-side values for each (entity, field) cell over the
following weeks, run the auto-sampler nightly to populate the
Aslan-side values, and close the run at quarter-end. The closed-run
data feeds the public claim "Aslan beats Bloomberg's TR coverage on
N of 12 fields" via the per-field aggregate at the bottom of
``/dq/bloomberg`` and the markdown render of
``aslan-event-extractor/docs/comparisons/bloomberg.md``.

Tables:

  * ``audit.bloomberg_comparison_run`` — one row per (quarter, opened_at).
    ``closed_at`` flips on close. The labelling flow inserts cells; the
    auto-sampler updates them; the close CLI sets ``closed_at``.

  * ``audit.bloomberg_comparison_cell`` — 60 rows per run (5 entities x
    12 fields). Both ``bloomberg_value`` (mutable, manual entry) and
    ``aslan_value`` (mutable, auto-sampler output) start NULL. The
    auto-sampler also writes ``variance_pct`` + ``aslan_advantage``
    (one of ``wins``/``ties``/``loses``) when both values are known.

Roles:

  * ``audit_writer`` — INSERT on both tables (the open-quarter helper
    creates 60 cells idempotently); UPDATE on ``aslan_value``,
    ``aslan_sampled_at``, ``variance_pct``, ``aslan_advantage`` for the
    auto-sampler.
  * ``audit_admin`` — UPDATE on ``bloomberg_value`` and the run-closure
    column; manual entry by sidar runs under this role on production.
  * ``audit_reader`` — SELECT.
  * ``aslan_dashboard`` — SELECT only (the dashboard process is
    read-only by design); manual entry POSTs go through a
    write-capable role per the spot-check pattern.

Both tables are append-only at the row level — once a cell is created
the row is never deleted, only its ``aslan_value`` / ``bloomberg_value``
get filled in. ``recorded_at`` carries the create time for audit.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0060"
down_revision: str | Sequence[str] | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE audit.bloomberg_comparison_run (
            run_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            quarter       TEXT NOT NULL UNIQUE
                CHECK (quarter ~ '^[0-9]{4}Q[1-4]$'),
            opened_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            opened_by     TEXT NOT NULL DEFAULT current_user,
            closed_at     TIMESTAMPTZ,
            closed_by     TEXT,
            recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX bcr_open ON audit.bloomberg_comparison_run(quarter) WHERE closed_at IS NULL"
    )

    op.execute("""
        CREATE TABLE audit.bloomberg_comparison_cell (
            cell_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id            UUID NOT NULL
                REFERENCES audit.bloomberg_comparison_run(run_id) ON DELETE CASCADE,
            entity_ticker     TEXT NOT NULL,
            field             TEXT NOT NULL,
            bloomberg_value   TEXT,
            bloomberg_entered_by  TEXT,
            bloomberg_entered_at  TIMESTAMPTZ,
            aslan_value       TEXT,
            aslan_sampled_at  TIMESTAMPTZ,
            variance_pct      NUMERIC,
            aslan_advantage   TEXT
                CHECK (aslan_advantage IN ('wins','ties','loses') OR aslan_advantage IS NULL),
            recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (run_id, entity_ticker, field)
        )
    """)
    op.execute(
        "CREATE INDEX bcc_run_advantage ON audit.bloomberg_comparison_cell(run_id, aslan_advantage)"
    )
    op.execute(
        "CREATE INDEX bcc_field_advantage "
        "ON audit.bloomberg_comparison_cell(field, aslan_advantage)"
    )
    op.execute(
        "CREATE INDEX bcc_pending_bloomberg "
        "ON audit.bloomberg_comparison_cell(run_id) "
        "WHERE bloomberg_value IS NULL"
    )

    # ── audit_writer: append cells + auto-sampler updates ──
    op.execute("GRANT INSERT ON audit.bloomberg_comparison_run TO audit_writer")
    op.execute("GRANT INSERT ON audit.bloomberg_comparison_cell TO audit_writer")
    op.execute(
        "GRANT UPDATE (aslan_value, aslan_sampled_at, variance_pct, aslan_advantage) "
        "ON audit.bloomberg_comparison_cell TO audit_writer"
    )

    # ── audit_admin: manual entry for bloomberg_value + run closure ──
    # audit_admin already has GRANT ALL via 0053; this is documentation
    # only (the column-level UPDATE GRANT below is a defence-in-depth
    # guard if a future migration narrows audit_admin).
    op.execute(
        "GRANT UPDATE (bloomberg_value, bloomberg_entered_by, bloomberg_entered_at) "
        "ON audit.bloomberg_comparison_cell TO audit_admin"
    )
    op.execute(
        "GRANT UPDATE (closed_at, closed_by) ON audit.bloomberg_comparison_run TO audit_admin"
    )

    # ── audit_reader + aslan_dashboard: SELECT-only ──
    op.execute(
        "GRANT SELECT ON audit.bloomberg_comparison_run, "
        "audit.bloomberg_comparison_cell TO audit_reader"
    )
    op.execute(
        "GRANT SELECT ON audit.bloomberg_comparison_run, "
        "audit.bloomberg_comparison_cell TO aslan_dashboard"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit.bloomberg_comparison_cell")
    op.execute("DROP TABLE IF EXISTS audit.bloomberg_comparison_run")
