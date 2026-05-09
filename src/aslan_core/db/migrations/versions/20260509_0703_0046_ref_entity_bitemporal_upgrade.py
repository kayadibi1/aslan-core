"""DEFERRED: ref.entity bitemporal upgrade — too invasive for v1.

Revision ID: 0046
Revises: 0045
Create Date: 2026-05-09 07:03:00

Original plan: add ``as_of`` and ``merged_from_entity_ids`` columns to
``ref.entity``, change PK from ``(entity_id)`` to ``(entity_id, as_of)``,
add BEFORE UPDATE trigger.

Why deferred (verified via shadow-DB inspection 2026-05-09):
``ref.entity_pkey`` is referenced by **14+ foreign key constraints**
across ``agg.*``, ``doc.*``, ``kap.*``, ``ref.*``, ``ts.*`` schemas.
Dropping it requires CASCADE and a coordinated re-creation of every
dependent FK with new (entity_id, as_of) shape — which would itself
require re-evaluating each consumer's lookup semantics. That's a
multi-week coordination scoped beyond this build.

Alternative for v1 (per ``IMPLEMENTATION_NOTES.md`` update 2026-05-09):
- ``ref.entity`` stays Class F (current-only).
- ``ref.entity_lineage`` (added in 0047) carries bitemporal merge/split
  records. The bitemporal-research-API derives PIT entity reconstruction
  by joining ``ref.entity`` with ``ref.entity_lineage`` and applying
  lineage events ≤ p_as_of in reverse.
- The PIT-correct view of ``/v1/research/entities`` at a past ``as_of``
  before a merge surfaces both pre-merge entities; at or after the
  merge, surfaces only the merged-into entity.
- This is sufficient for Moat 2 on entity lineage; full bitemporal
  ``ref.entity`` is a v2 follow-up.

Reversibility: trivial (no-op).
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0046"
down_revision: str | Sequence[str] | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
