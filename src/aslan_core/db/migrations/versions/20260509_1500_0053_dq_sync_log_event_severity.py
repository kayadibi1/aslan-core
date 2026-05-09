"""dq M-AU-01: sync_log, event, severity_rule, alert_dispatch + roles

Revision ID: 0053
Revises: 0042 (origin/main head; skips unmerged in-flight 0043-0052
on feature/bitemporal-research-api - multi-head will resolve via
`alembic merge` when that branch lands)
Create Date: 2026-05-09 15:00:00

First slice of the operational-audit subsystem
(docs/superpowers/specs/2026-05-09-data-quality-audit-design.md §5.1,
§5.8, §5.9). Coexists with audit.events from migration 0010.

Tables:
  - audit.sync_log         (puller invocations)
  - audit.event            (catch-all event stream)
  - audit.severity_rule    (alert routing config; only mutable table)
  - audit.alert_dispatch   (sent-alert ledger; dedupes via content_hash)

Roles:
  - audit_writer  - INSERT on the four tables; nothing else
  - audit_reader  - SELECT only
  - audit_admin   - full

Trigger: audit.severity_rule_change_audit fires AFTER INSERT/UPDATE/DELETE
on audit.severity_rule and writes a row into audit.event with
event_type='severity_rule_changed'.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0053"
down_revision: str | Sequence[str] | None = "0042"  # skip unmerged 0043-0052
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS audit")

    op.execute("""
        CREATE TABLE audit.sync_log (
            sync_id           BIGSERIAL PRIMARY KEY,
            source            TEXT NOT NULL
                CHECK (source IN ('kap','evds','bist','tefas','mkk',
                                  'extractor','dashboard')),
            operation         TEXT NOT NULL,
            started_at        TIMESTAMPTZ NOT NULL,
            completed_at      TIMESTAMPTZ,
            status            TEXT NOT NULL
                CHECK (status IN ('running','ok','partial','failed')),
            records_ingested  BIGINT,
            records_failed    BIGINT,
            upstream_max_at   TIMESTAMPTZ,
            db_max_at         TIMESTAMPTZ,
            error_summary     TEXT,
            git_sha           TEXT NOT NULL,
            host              TEXT NOT NULL,
            recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX sync_log_src_op_started ON audit.sync_log(source, operation, started_at DESC)"
    )
    op.execute(
        "CREATE INDEX sync_log_status_started "
        "ON audit.sync_log(status, started_at DESC) "
        "WHERE status IN ('running','failed')"
    )

    op.execute("""
        CREATE TABLE audit.event (
            event_id     BIGSERIAL PRIMARY KEY,
            event_type   TEXT NOT NULL,
            emitted_at   TIMESTAMPTZ NOT NULL,
            emitter      TEXT NOT NULL,
            severity     TEXT
                CHECK (severity IN ('info','warn','error','critical')),
            payload      JSONB NOT NULL,
            recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX event_type_emitted ON audit.event(event_type, emitted_at DESC)")

    op.execute("""
        CREATE TABLE audit.severity_rule (
            rule_name         TEXT PRIMARY KEY,
            predicate         TEXT NOT NULL,
            severity          TEXT NOT NULL
                CHECK (severity IN ('info','warn','error','critical')),
            sinks             TEXT[] NOT NULL,
            throttle_seconds  INT NOT NULL DEFAULT 300,
            enabled           BOOLEAN NOT NULL DEFAULT true,
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by        TEXT NOT NULL DEFAULT current_user
        )
    """)

    op.execute("""
        CREATE TABLE audit.alert_dispatch (
            dispatch_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            rule_name       TEXT NOT NULL REFERENCES audit.severity_rule(rule_name),
            fired_at        TIMESTAMPTZ NOT NULL,
            sink            TEXT NOT NULL,
            delivered_at    TIMESTAMPTZ,
            status          TEXT NOT NULL
                CHECK (status IN ('pending','delivered','failed','suppressed')),
            payload         JSONB NOT NULL,
            content_hash    TEXT NOT NULL,
            recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    # date_trunc(text, timestamptz) is STABLE (depends on session TZ);
    # Postgres requires IMMUTABLE expressions in index definitions.
    # `fired_at AT TIME ZONE 'UTC'` returns timestamp without time zone,
    # for which date_trunc(text, timestamp) is IMMUTABLE.
    op.execute(
        "CREATE UNIQUE INDEX ad_throttle_dedup "
        "ON audit.alert_dispatch(rule_name, sink, content_hash, "
        "                       date_trunc('minute', fired_at AT TIME ZONE 'UTC'))"
    )
    op.execute("CREATE INDEX ad_pending ON audit.alert_dispatch(fired_at) WHERE status = 'pending'")

    op.execute("""
        CREATE OR REPLACE FUNCTION audit.fn_severity_rule_change_audit()
        RETURNS TRIGGER AS $$
        BEGIN
            INSERT INTO audit.event(event_type, emitted_at, emitter,
                                    severity, payload)
            VALUES (
                'severity_rule_changed',
                now(),
                'audit.severity_rule.trigger',
                'info',
                jsonb_build_object(
                    'op', TG_OP,
                    'rule_name', COALESCE(NEW.rule_name, OLD.rule_name),
                    'before', to_jsonb(OLD),
                    'after',  to_jsonb(NEW),
                    'updated_by', current_user
                )
            );
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER severity_rule_change_audit
        AFTER INSERT OR UPDATE OR DELETE ON audit.severity_rule
        FOR EACH ROW EXECUTE FUNCTION audit.fn_severity_rule_change_audit();
    """)

    for role in ("audit_writer", "audit_reader", "audit_admin"):
        op.execute(
            f"DO $$ BEGIN "
            f"  CREATE ROLE {role} NOLOGIN; "
            f"EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )

    op.execute("GRANT USAGE ON SCHEMA audit TO audit_writer, audit_reader, audit_admin")
    op.execute("GRANT INSERT ON audit.sync_log, audit.event, audit.alert_dispatch TO audit_writer")
    op.execute(
        "GRANT USAGE ON SEQUENCE audit.sync_log_sync_id_seq, "
        "audit.event_event_id_seq TO audit_writer"
    )
    op.execute(
        "GRANT SELECT ON audit.sync_log, audit.event, audit.severity_rule, "
        "audit.alert_dispatch TO audit_reader"
    )
    op.execute("GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA audit TO audit_admin")
    op.execute("GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA audit TO audit_admin")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS severity_rule_change_audit ON audit.severity_rule")
    op.execute("DROP FUNCTION IF EXISTS audit.fn_severity_rule_change_audit()")
    op.execute("DROP TABLE IF EXISTS audit.alert_dispatch")
    op.execute("DROP TABLE IF EXISTS audit.severity_rule")
    op.execute("DROP TABLE IF EXISTS audit.event")
    op.execute("DROP TABLE IF EXISTS audit.sync_log")
