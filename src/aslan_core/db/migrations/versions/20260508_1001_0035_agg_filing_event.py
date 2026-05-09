"""agg.filing_event — typed, bitemporal, multi-pass-aware extracted events.

Revision ID: 0035
Revises: 0034
Create Date: 2026-05-08 10:01:00

Per ``aslan-event-extractor/SCOPE.md`` §5.1. Cross-schema FKs to:

* ``doc.filing(filing_id)`` — source filing reference (D3)
* ``ref.entity(entity_id)`` — subject entity + counterparty
* ``src.ingestion_run(ingestion_run_id)`` — write attribution

Carries verbose extraction-provenance columns (primary + verifier model
versions, prompt versions, confidences, token counts, input hash) so
every event is replayable from (raw bytes, model_version, prompt_version,
seed) — verified by replay test in M5.

Bitemporal pattern: every state change is a NEW row with NEW ``as_of``,
the prior row gets ``superseded_at = new.as_of``. ``filing_event_current``
view selects the latest non-superseded row per natural key.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0035"
down_revision: str | Sequence[str] | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE agg.filing_event (
            filing_event_id         UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,

            filing_id               UUID NOT NULL REFERENCES doc.filing(filing_id),
            source_id               TEXT NOT NULL,
            source_filing_ref       TEXT NOT NULL,

            event_type              agg.filing_event_type NOT NULL,
            event_seq               SMALLINT NOT NULL DEFAULT 1,

            entity_id               UUID NOT NULL REFERENCES ref.entity(entity_id),
            counterparty_entity_id  UUID REFERENCES ref.entity(entity_id),

            event_ts                TIMESTAMPTZ NOT NULL,
            effective_dt            DATE,
            as_of                   TIMESTAMPTZ NOT NULL,
            superseded_at           TIMESTAMPTZ,

            payload                 JSONB NOT NULL,

            primary_model_version   TEXT NOT NULL,
            primary_prompt_version  TEXT NOT NULL,
            primary_confidence      REAL NOT NULL,
            verifier_model_version  TEXT,
            verifier_prompt_version TEXT,
            verifier_confidence     REAL,
            verifier_agreement      BOOLEAN,
            final_confidence        REAL NOT NULL,

            input_text_sha256       CHAR(64) NOT NULL,
            input_text_chars        INT NOT NULL,
            input_token_count       INT,
            output_token_count      INT,

            ingestion_run_id        BIGINT NOT NULL REFERENCES src.ingestion_run(ingestion_run_id),
            extracted_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT fe_primary_conf_range CHECK (primary_confidence >= 0.0 AND primary_confidence <= 1.0),
            CONSTRAINT fe_final_conf_range   CHECK (final_confidence >= 0.0 AND final_confidence <= 1.0),
            CONSTRAINT fe_event_seq_pos      CHECK (event_seq >= 1),
            CONSTRAINT fe_natural_key        UNIQUE (filing_id, event_type, event_seq, as_of)
        )
    """)
    op.execute(
        "CREATE INDEX fe_entity_type_event_ts "
        "ON agg.filing_event (entity_id, event_type, event_ts DESC)"
    )
    op.execute("CREATE INDEX fe_filing ON agg.filing_event (filing_id, as_of DESC)")
    op.execute("CREATE INDEX fe_event_ts ON agg.filing_event (event_ts DESC)")
    op.execute("CREATE INDEX fe_source_ref ON agg.filing_event (source_id, source_filing_ref)")
    op.execute("CREATE INDEX fe_payload_gin ON agg.filing_event USING gin (payload jsonb_path_ops)")
    op.execute(
        "CREATE INDEX fe_current_only "
        "ON agg.filing_event (entity_id, event_type, event_ts DESC) "
        "WHERE superseded_at IS NULL"
    )

    op.execute("""
        CREATE VIEW agg.filing_event_current AS
        SELECT DISTINCT ON (filing_id, event_type, event_seq) *
        FROM agg.filing_event
        WHERE superseded_at IS NULL
        ORDER BY filing_id, event_type, event_seq, as_of DESC
    """)

    op.execute("GRANT SELECT ON agg.filing_event TO aslan_dashboard")
    op.execute("GRANT SELECT ON agg.filing_event_current TO aslan_dashboard")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON agg.filing_event_current FROM aslan_dashboard")
    op.execute("REVOKE SELECT ON agg.filing_event FROM aslan_dashboard")
    op.execute("DROP VIEW IF EXISTS agg.filing_event_current")
    op.execute("DROP TABLE IF EXISTS agg.filing_event")
