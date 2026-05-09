"""Research API support tables: api_key, api_query_audit, rate_limit_state, feature_flags.

Revision ID: 0048
Revises: 0047
Create Date: 2026-05-09 07:05:00

Per ``docs/specs/bitemporal-research-api/SCOPE.md`` D5, D6, D18, and
the master flag implementation per D27 / H7.

Creates four tables in ``aslan_core``:

1. ``aslan_core.api_key``  — long-lived API keys (D5).
2. ``aslan_core.api_query_audit`` — per-request audit log (D18).
3. ``aslan_core.api_rate_limit_state`` — token-bucket state (D6).
4. ``aslan_core.feature_flags`` — runtime feature flag state (H3, H7).

These tables are themselves NOT bitemporal (they are operational
metadata, not facts), so no append-only triggers and no registry
rows.

Reversibility: drops the four tables.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0048"
down_revision: str | Sequence[str] | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. api_key — long-lived API keys per D5
    op.execute("""
        CREATE TABLE aslan_core.api_key (
            key_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            secret_hash         TEXT NOT NULL,
            description         TEXT,
            rate_tier           TEXT NOT NULL DEFAULT 'partner'
                                CHECK (rate_tier IN ('internal', 'partner', 'public')),
            rate_overrides      JSONB,
            pii_unredacted      BOOLEAN NOT NULL DEFAULT false,
            scopes              TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at          TIMESTAMPTZ NOT NULL DEFAULT now() + interval '1 year',
            revoked_at          TIMESTAMPTZ,
            last_used_at        TIMESTAMPTZ,
            created_by          TEXT
        )
    """)
    op.execute(
        "CREATE INDEX api_key_active_filter_idx "
        "ON aslan_core.api_key (revoked_at, expires_at)"
    )

    # 2. api_query_audit — per-request audit log per D18
    # 13-month retention is enforced by a separate cron, not in-DDL.
    op.execute("""
        CREATE TABLE aslan_core.api_query_audit (
            audit_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            requested_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            api_key_id            UUID REFERENCES aslan_core.api_key(key_id),
            request_ip            INET,
            request_id            UUID NOT NULL,
            endpoint              TEXT NOT NULL,
            query_params_sha256   BYTEA,
            as_of_requested       TIMESTAMPTZ,
            as_of_resolved        TIMESTAMPTZ NOT NULL,
            rows_returned         BIGINT NOT NULL DEFAULT 0,
            latency_ms            INT NOT NULL,
            status_code           INT NOT NULL,
            error_code            TEXT,
            feature_flags_active  TEXT[] NOT NULL DEFAULT ARRAY[]::text[]
        )
    """)
    op.execute("CREATE INDEX aqa_requested_at_idx ON aslan_core.api_query_audit (requested_at DESC)")
    op.execute("CREATE INDEX aqa_api_key_idx ON aslan_core.api_query_audit (api_key_id, requested_at DESC)")
    op.execute("CREATE INDEX aqa_status_idx ON aslan_core.api_query_audit (status_code) WHERE status_code >= 400")

    # 3. api_rate_limit_state — token-bucket state per D6
    op.execute("""
        CREATE TABLE aslan_core.api_rate_limit_state (
            api_key_id      UUID NOT NULL REFERENCES aslan_core.api_key(key_id),
            window_kind     TEXT NOT NULL CHECK (window_kind IN ('minute', 'hour', 'day')),
            window_start    TIMESTAMPTZ NOT NULL,
            tokens_used     INT NOT NULL DEFAULT 0,
            last_request_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (api_key_id, window_kind, window_start)
        )
    """)
    op.execute("CREATE INDEX rls_window_idx ON aslan_core.api_rate_limit_state (window_kind, window_start)")

    # 4. feature_flags — per H3, H7
    op.execute("""
        CREATE TABLE aslan_core.feature_flags (
            flag_name      TEXT PRIMARY KEY,
            value_text     TEXT,
            value_bool     BOOLEAN,
            value_int      INT,
            scope          TEXT NOT NULL DEFAULT 'global'
                           CHECK (scope IN ('global', 'environment', 'api_key')),
            scope_value    TEXT,
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by     TEXT,
            notes          TEXT
        )
    """)

    # Seed the flags from SCOPE.md §3 with defaults. Master flag
    # BITEMPORAL_API_ENABLED defaults to false on every environment;
    # it must be deliberately enabled per environment.
    op.execute("""
        INSERT INTO aslan_core.feature_flags (flag_name, value_bool, scope, notes) VALUES
            ('BITEMPORAL_API_ENABLED', false, 'global',
             'Master kill-switch for the bitemporal-research-API. '
             'Per D27/H7, enable per-environment after Phase 5 gate (staging) '
             'or Phase 7e (production).'),
            ('BITEMPORAL_API_INTERVAL_QUERIES', true, 'global', 'D2'),
            -- BITEMPORAL_API_ALLOW_NULL_AS_OF retired in round 8;
            -- migration 0051's kap.disclosures_version.as_of NOT NULL
            -- makes the gated behavior unreachable. Row preserved
            -- here for upgrade compatibility but marked retired in
            -- the notes column so operators don't expect it to do
            -- anything.
            ('BITEMPORAL_API_ALLOW_NULL_AS_OF', true, 'global',
             'RETIRED round 8 (D3 PROVISIONAL->FINAL). No-op flag; '
             'the NULL-as_of behavior it gated is not reachable '
             'because kap.disclosures_version.as_of is NOT NULL.'),
            ('BITEMPORAL_API_RATE_LIMIT_ENFORCED', true, 'global', 'D6'),
            ('BITEMPORAL_API_TAS29_RESTATEMENT_CHAIN', false, 'global', 'D8 — flip true after Phase 2j'),
            ('BITEMPORAL_API_ENTITY_MERGE_LINEAGE', false, 'global', 'D10 — flip true after Phase 2j'),
            ('BITEMPORAL_API_KAP_APPEND_ONLY_TRIGGER', false, 'global', 'D11 — flip true after CRAWL_PATCHES applied'),
            ('BITEMPORAL_API_AUDIT_LOG_ENABLED', true, 'global', 'D18'),
            ('BITEMPORAL_API_COST_LIMITS_ENFORCED', true, 'global', 'D19'),
            ('BITEMPORAL_API_V2_PREVIEW', false, 'global', 'D23'),
            ('BITEMPORAL_TABLE_REGISTRY_CI_ENFORCED', true, 'global', 'D28'),
            ('BITEMPORAL_API_PII_EXPOSURE', false, 'global', 'D30')
    """)
    op.execute("""
        INSERT INTO aslan_core.feature_flags (flag_name, value_text, scope, notes) VALUES
            ('BITEMPORAL_API_CURSOR_VERSION', '1', 'global', 'D7'),
            ('BITEMPORAL_API_ENVELOPE_VERSION', '1', 'global', 'D15'),
            ('BITEMPORAL_API_ERROR_FORMAT_VERSION', '1', 'global', 'D16'),
            ('BITEMPORAL_API_AUTH_ADDITIONAL_SCHEMES', '', 'global', 'D5')
    """)

    op.execute("GRANT SELECT ON aslan_core.api_key TO aslan_dashboard")
    op.execute("GRANT SELECT ON aslan_core.api_query_audit TO aslan_dashboard")
    op.execute("GRANT SELECT ON aslan_core.feature_flags TO aslan_dashboard")
    # api_rate_limit_state intentionally not granted to aslan_dashboard


def downgrade() -> None:
    op.execute("REVOKE SELECT ON aslan_core.feature_flags FROM aslan_dashboard")
    op.execute("REVOKE SELECT ON aslan_core.api_query_audit FROM aslan_dashboard")
    op.execute("REVOKE SELECT ON aslan_core.api_key FROM aslan_dashboard")
    op.execute("DROP TABLE IF EXISTS aslan_core.feature_flags")
    op.execute("DROP TABLE IF EXISTS aslan_core.api_rate_limit_state")
    op.execute("DROP TABLE IF EXISTS aslan_core.api_query_audit")
    op.execute("DROP TABLE IF EXISTS aslan_core.api_key")
