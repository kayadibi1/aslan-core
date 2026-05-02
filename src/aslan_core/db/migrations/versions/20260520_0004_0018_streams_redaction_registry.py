"""streams.redaction_registry + aslan_app role +
redaction_registry_insert SECURITY DEFINER function + REVOKE/GRANT
(codex F3 + F6 + F15 round 6).

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-20 00:04:00

Codex F3 (round 1) introduced the registry; F6 (round 2) made
``redacted_payload`` + ``original_payload_hash`` NOT NULL so the
registry persists the FULL redacted payload (Art. 17 audit-trail
requirement: "what did we keep" must be inspectable post-redaction);
F15 (round 6) closed the bypass surface by REVOKEing direct
INSERT/UPDATE/DELETE from the ``aslan_app`` role and exposing only the
``redaction_registry_insert(...)`` SECURITY DEFINER function whose
FIRST statement is ``pg_advisory_xact_lock(...)``. The lock-then-insert
contract is non-bypassable at the DB level — a SQL injection or
out-of-process direct write CANNOT skip the lock because the table's
mutation surface is closed.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Idempotent role creation — run as the migration role (typically
    # the test container's superuser); the CREATE ROLE is wrapped in a
    # DO block to be re-runnable.
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='aslan_app') THEN
                CREATE ROLE aslan_app NOLOGIN;
            END IF;
        END $$;
    """)

    op.execute("""
        CREATE TABLE streams.redaction_registry (
            event_id                UUID PRIMARY KEY,
            redaction_reason        TEXT NOT NULL,
            redacted_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
            original_stream         TEXT NOT NULL,
            redacted_payload        JSONB NOT NULL,
            redacted_payload_hash   TEXT NOT NULL,
            original_payload_hash   CHAR(64) NOT NULL,
            actor_id                TEXT,
            actor_kind              TEXT
                CHECK (actor_kind IN ('user','service','system')),
            request_id              UUID
        )
    """)
    op.execute(
        "CREATE INDEX redaction_registry_redacted_at ON streams.redaction_registry(redacted_at)"
    )
    op.execute(
        "CREATE INDEX redaction_registry_stream ON streams.redaction_registry(original_stream)"
    )

    # Codex F15 round 6 — SECURITY DEFINER function whose FIRST
    # statement is the advisory-lock acquisition. The lock auto-releases
    # on the calling transaction's commit/rollback.
    op.execute("""
        CREATE OR REPLACE FUNCTION streams.redaction_registry_insert(
            p_event_id              UUID,
            p_redaction_reason      TEXT,
            p_redacted_at           TIMESTAMPTZ,
            p_original_stream       TEXT,
            p_redacted_payload      JSONB,
            p_redacted_payload_hash CHAR(64),
            p_original_payload_hash CHAR(64)
        ) RETURNS VOID
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = streams, pg_temp
        AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended('streams.redaction:' || p_event_id::text, 0)
            );
            INSERT INTO streams.redaction_registry (
                event_id, redaction_reason, redacted_at, original_stream,
                redacted_payload, redacted_payload_hash, original_payload_hash
            ) VALUES (
                p_event_id, p_redaction_reason, p_redacted_at, p_original_stream,
                p_redacted_payload, p_redacted_payload_hash, p_original_payload_hash
            );
        END;
        $$;
    """)

    # aslan_app needs USAGE on the schema to reference any object
    # inside it (function call or SELECT). USAGE is the minimum;
    # CREATE is intentionally NOT granted — the role cannot add new
    # tables / functions / etc.
    op.execute("GRANT USAGE ON SCHEMA streams TO aslan_app")

    # Codex F15 round 6 — REVOKE direct table writes; GRANT function
    # execution + read-only SELECT.
    op.execute("REVOKE INSERT, UPDATE, DELETE ON streams.redaction_registry FROM aslan_app")
    op.execute("GRANT SELECT ON streams.redaction_registry TO aslan_app")
    op.execute(
        "GRANT EXECUTE ON FUNCTION streams.redaction_registry_insert("
        "  UUID, TEXT, TIMESTAMPTZ, TEXT, JSONB, CHAR(64), CHAR(64)"
        ") TO aslan_app"
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS streams.redaction_registry_insert("
        "  UUID, TEXT, TIMESTAMPTZ, TEXT, JSONB, CHAR(64), CHAR(64))"
    )
    op.execute("DROP TABLE IF EXISTS streams.redaction_registry")
    # Do NOT drop the aslan_app role on downgrade — it may be referenced
    # by other objects in a partial-rollback scenario; operator can drop
    # manually if truly retiring v0.5.
