from __future__ import annotations

from typing import Any

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from aslan_core.errors import ConfigError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
        # Codex 2026-04-29: prevent ValidationError text from echoing
        # raw secret input back through ConfigError. Without this,
        # `Settings(ASLAN_PG_DSN={"x": "...secret..."})` would surface
        # the secret in the error message via Pydantic's default
        # `input_value=...` rendering.
        hide_input_in_errors=True,
    )

    # Credential-bearing fields are wrapped in ``SecretStr`` so that
    # ``repr(Settings)`` and accidental ``logger.info(s)`` calls do not
    # leak passwords. Unwrap with ``.get_secret_value()`` at the call
    # site (engine DSN construction, S3 client config, Sentry init).
    postgres_dsn: SecretStr = Field(validation_alias="ASLAN_PG_DSN")
    redis_url: str = Field(validation_alias="ASLAN_REDIS_URL")
    s3_endpoint: str = Field(validation_alias="ASLAN_S3_ENDPOINT")
    s3_region: str = Field(validation_alias="ASLAN_S3_REGION")
    s3_access_key: SecretStr = Field(validation_alias="ASLAN_S3_ACCESS_KEY")
    s3_secret_key: SecretStr = Field(validation_alias="ASLAN_S3_SECRET_KEY")

    # ── Audit (v0.3.0) ────────────────────────────────────────────────
    # If True, audit.record() raises AuditMissingActor when no actor is
    # set in the ContextVar. If False (default during the v0.3 → v1.0
    # migration window), a structured warning is logged and the audit
    # row is written with actor_id='system:unknown'. v1.0 will flip this
    # default to True; v1.1 will remove the lenient path entirely.
    audit_strict: bool = Field(
        default=False,
        validation_alias="ASLAN_AUDIT_STRICT",
    )

    # ── Sentry (v0.3.0) ───────────────────────────────────────────────
    # Opt-in via DSN. None = no-op.
    sentry_dsn: SecretStr | None = Field(
        default=None,
        validation_alias="ASLAN_SENTRY_DSN",
    )
    sentry_environment: str = Field(
        default="local",
        validation_alias="ASLAN_SENTRY_ENVIRONMENT",
    )
    sentry_sample_rate: float = Field(
        default=1.0,
        validation_alias="ASLAN_SENTRY_SAMPLE_RATE",
    )
    sentry_traces_sample_rate: float = Field(
        default=0.1,
        validation_alias="ASLAN_SENTRY_TRACES_SAMPLE_RATE",
    )

    # ── OpenTelemetry (v0.3.0) ────────────────────────────────────────
    # Opt-in via endpoint. otel_enabled is a redundant on/off switch for
    # tests that want to disable tracing without unsetting the endpoint.
    otel_enabled: bool = Field(
        default=False,
        validation_alias="ASLAN_OTEL_ENABLED",
    )
    otel_endpoint: str | None = Field(
        default=None,
        validation_alias="ASLAN_OTEL_ENDPOINT",
    )
    otel_service_name: str = Field(
        default="aslan-core",
        validation_alias="ASLAN_OTEL_SERVICE_NAME",
    )

    # ── Prometheus (v0.3.0) ───────────────────────────────────────────
    # Toggleable for unit tests that don't want metric side effects.
    metrics_enabled: bool = Field(
        default=True,
        validation_alias="ASLAN_METRICS_ENABLED",
    )

    # ── DQ alert sinks (M3) ──────────────────────────────────────────
    # Optional fanout targets for `aslan_core.dq.alert_dispatch`. Each
    # is independently nullable: when a sink is unconfigured the
    # dispatcher marks affected `audit.alert_dispatch` rows as
    # ``status='suppressed'`` with a ``not_configured`` reason rather
    # than raising. Production must set at least one (typically all
    # three) — see docs/superpowers/handoffs/2026-05-09-dq-m1-emitter-wiring.md
    # `## M3 alert deployment` for env-var wiring.
    audit_glitchtip_dsn: SecretStr | None = Field(
        default=None,
        validation_alias="ASLAN_AUDIT_GLITCHTIP_DSN",
    )
    audit_smtp_url: SecretStr | None = Field(
        default=None,
        validation_alias="ASLAN_AUDIT_SMTP_URL",
    )
    # Comma-separated; the email sink splits on `,` and trims whitespace.
    audit_email_to: str = Field(
        default="",
        validation_alias="ASLAN_AUDIT_EMAIL_TO",
    )
    audit_slack_webhook_url: SecretStr | None = Field(
        default=None,
        validation_alias="ASLAN_AUDIT_SLACK_WEBHOOK_URL",
    )

    # ── DQ probe upstream HTTP (Batch 2 follow-up) ───────────────────
    # Opt-in URLs used by the per-source recency probes (KAP / MKK)
    # to compute ``upstream_latest_at`` against a real upstream rather
    # than the conservative DB-only fallback. When unset, the probes
    # use DB-only mode (assumes upstream publishes continuously, lag
    # = "time since our last ingest"). When set, the probes issue an
    # HTTP GET via the proxy-aware httpx client (KAP_PROXY_URL when
    # present, else direct) and parse for the latest event timestamp.
    #
    # CRITICAL (per workspace CLAUDE.md + crawl commit eb78619): KAP
    # HTTP from any aslan service MUST honor the rotating-proxy pool
    # via ``KAP_PROXY_URL``. The probe's httpx client reads
    # ``KAP_PROXY_URL`` directly so a forgotten env var does not
    # silently bypass the proxy. ``DQ_KAP_LISTING_URL=`` (empty) keeps
    # the probe in DB-only mode regardless of ``KAP_PROXY_URL``.
    dq_kap_listing_url: str | None = Field(
        default=None,
        validation_alias="DQ_KAP_LISTING_URL",
    )
    dq_mkk_api_url: str | None = Field(
        default=None,
        validation_alias="DQ_MKK_API_URL",
    )
    dq_mkk_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="DQ_MKK_API_KEY",
    )

    def __init__(self, **kw: Any) -> None:
        try:
            super().__init__(**kw)
        except ValidationError as e:
            raise ConfigError(str(e)) from e


class _DbConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    postgres_dsn: SecretStr = Field(validation_alias="ASLAN_PG_DSN")

    def __init__(self, **kw: Any) -> None:
        try:
            super().__init__(**kw)
        except ValidationError as e:
            raise ConfigError(str(e)) from e


def postgres_dsn_from_env() -> str:
    """Load ``ASLAN_PG_DSN`` without requiring Redis/S3 settings.

    Use from DB-only entry points (CLI seed/registry/migrate) so a missing
    Redis or S3 env var doesn't mask a Postgres failure.

    Returns the unwrapped DSN string (callers like SQLAlchemy/Alembic need
    a plain ``str``). Inside the ``Settings`` object itself the DSN stays
    wrapped in ``SecretStr`` so ``repr`` cannot leak it.
    """
    return _DbConfig().postgres_dsn.get_secret_value()
