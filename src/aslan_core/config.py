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
