"""ts.series_subject — structured Art. 17 deletion path (codex F4)

Revision ID: 0013
Revises: 0012
Create Date: 2026-04-30 00:05:00

Codex F4 — when a series carries identifying PII the GDPR Art. 17
deletion runtime needs a reverse lookup from
``subject_id -> series_id`` so a data-subject erasure request can
locate every series that references the subject without scanning
every JSONB metadata blob.

Composite PK ``(series_id, subject_id, role)`` lets the same person
appear under multiple roles in the same series (e.g. both
``reporter`` and ``beneficial_owner``). FK to ``ts.series_catalog``
is ON DELETE CASCADE because subject links are forensic-irrelevant
on their own — they only carry meaning while the parent series
exists. The ``series_subject_lookup`` index covers
``(subject_id, role)`` for the deletion-runtime reverse lookup.

The role CHECK list MUST stay in sync with ``SubjectRole`` Literal in
``aslan_core.schemas.timeseries``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE ts.series_subject (
            series_id          BIGINT NOT NULL
                REFERENCES ts.series_catalog(series_id) ON DELETE CASCADE,
            subject_id         TEXT NOT NULL,
            role               TEXT NOT NULL CHECK (role IN (
                'data_subject', 'reporter', 'beneficial_owner',
                'insider', 'executive', 'board_member', 'other'
            )),
            actor_id           TEXT,
            actor_kind         TEXT CHECK (actor_kind IN ('user','service','system')),
            request_id         UUID,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (series_id, subject_id, role)
        )
    """)
    op.execute("CREATE INDEX series_subject_lookup ON ts.series_subject(subject_id, role)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ts.series_subject")
