import pytest
from pydantic import SecretStr

from aslan_core.config import Settings
from aslan_core.errors import ConfigError


def test_settings_loads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASLAN_PG_DSN", "postgresql+asyncpg://u:p@host:6432/db")
    monkeypatch.setenv("ASLAN_REDIS_URL", "redis://host:6379/0")
    monkeypatch.setenv("ASLAN_S3_ENDPOINT", "http://host:9000")
    monkeypatch.setenv("ASLAN_S3_REGION", "us-east-1")
    monkeypatch.setenv("ASLAN_S3_ACCESS_KEY", "k")
    monkeypatch.setenv("ASLAN_S3_SECRET_KEY", "s")

    s = Settings()
    # ``postgres_dsn`` is now ``SecretStr`` (Task 11); unwrap to compare.
    assert s.postgres_dsn.get_secret_value() == "postgresql+asyncpg://u:p@host:6432/db"
    assert s.redis_url == "redis://host:6379/0"
    assert s.s3_endpoint == "http://host:9000"


def test_settings_missing_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "ASLAN_PG_DSN",
        "ASLAN_REDIS_URL",
        "ASLAN_S3_ENDPOINT",
        "ASLAN_S3_REGION",
        "ASLAN_S3_ACCESS_KEY",
        "ASLAN_S3_SECRET_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ConfigError):
        Settings()


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASLAN_PG_DSN", "postgresql+asyncpg://u:p@host:6432/db")
    monkeypatch.setenv("ASLAN_REDIS_URL", "redis://host:6379/0")
    monkeypatch.setenv("ASLAN_S3_ENDPOINT", "http://host:9000")
    monkeypatch.setenv("ASLAN_S3_REGION", "us-east-1")
    monkeypatch.setenv("ASLAN_S3_ACCESS_KEY", "k")
    monkeypatch.setenv("ASLAN_S3_SECRET_KEY", "s")


def test_observability_and_audit_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    # Ensure none of the new env vars leak in from the developer shell.
    for k in (
        "ASLAN_AUDIT_STRICT",
        "ASLAN_SENTRY_DSN",
        "ASLAN_SENTRY_ENVIRONMENT",
        "ASLAN_SENTRY_SAMPLE_RATE",
        "ASLAN_SENTRY_TRACES_SAMPLE_RATE",
        "ASLAN_OTEL_ENABLED",
        "ASLAN_OTEL_ENDPOINT",
        "ASLAN_OTEL_SERVICE_NAME",
        "ASLAN_METRICS_ENABLED",
    ):
        monkeypatch.delenv(k, raising=False)

    s = Settings()
    assert s.audit_strict is False
    assert s.sentry_dsn is None
    assert s.sentry_environment == "local"
    assert s.sentry_sample_rate == 1.0
    assert s.sentry_traces_sample_rate == 0.1
    assert s.otel_enabled is False
    assert s.otel_endpoint is None
    assert s.otel_service_name == "aslan-core"
    assert s.metrics_enabled is True


def test_audit_strict_overridable_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ASLAN_AUDIT_STRICT", "true")
    s = Settings()
    assert s.audit_strict is True


def test_sentry_dsn_is_secret_str(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ASLAN_SENTRY_DSN", "https://abc@sentry.example/1")
    s = Settings()
    assert isinstance(s.sentry_dsn, SecretStr)
    assert s.sentry_dsn.get_secret_value() == "https://abc@sentry.example/1"
    # A repr or str should not leak the DSN.
    assert "abc@sentry.example" not in repr(s.sentry_dsn)
    assert "abc@sentry.example" not in str(s.sentry_dsn)


def test_otel_endpoint_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ASLAN_OTEL_ENABLED", "true")
    monkeypatch.setenv("ASLAN_OTEL_ENDPOINT", "http://otel-collector:4317")
    monkeypatch.setenv("ASLAN_OTEL_SERVICE_NAME", "kap-scraper")
    s = Settings()
    assert s.otel_enabled is True
    assert s.otel_endpoint == "http://otel-collector:4317"
    assert s.otel_service_name == "kap-scraper"


def test_metrics_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ASLAN_METRICS_ENABLED", "false")
    s = Settings()
    assert s.metrics_enabled is False


# ─── Task 11: SecretStr hardening ──────────────────────────────────────────


def test_settings_repr_does_not_leak_postgres_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """``repr(Settings)`` must not leak the DSN (which contains the password).

    The DSN field is wrapped in ``SecretStr``; pydantic's repr shows the
    type, not the value.
    """
    monkeypatch.setenv(
        "ASLAN_PG_DSN",
        "postgresql+asyncpg://u:supersecret-do-not-leak@host:6432/db",
    )
    monkeypatch.setenv("ASLAN_REDIS_URL", "redis://host:6379/0")
    monkeypatch.setenv("ASLAN_S3_ENDPOINT", "http://host:9000")
    monkeypatch.setenv("ASLAN_S3_REGION", "us-east-1")
    monkeypatch.setenv("ASLAN_S3_ACCESS_KEY", "k")
    monkeypatch.setenv("ASLAN_S3_SECRET_KEY", "s")

    s = Settings()
    rep = repr(s)
    assert "supersecret-do-not-leak" not in rep
    assert "SecretStr" in rep


def test_settings_repr_does_not_leak_s3_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASLAN_PG_DSN", "postgresql+asyncpg://u:p@host:6432/db")
    monkeypatch.setenv("ASLAN_REDIS_URL", "redis://host:6379/0")
    monkeypatch.setenv("ASLAN_S3_ENDPOINT", "http://host:9000")
    monkeypatch.setenv("ASLAN_S3_REGION", "us-east-1")
    monkeypatch.setenv("ASLAN_S3_ACCESS_KEY", "k")
    monkeypatch.setenv("ASLAN_S3_SECRET_KEY", "supersecret-do-not-leak")

    s = Settings()
    rep = repr(s)
    assert "supersecret-do-not-leak" not in rep
    assert "SecretStr" in rep


def test_settings_repr_does_not_leak_s3_access_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASLAN_PG_DSN", "postgresql+asyncpg://u:p@host:6432/db")
    monkeypatch.setenv("ASLAN_REDIS_URL", "redis://host:6379/0")
    monkeypatch.setenv("ASLAN_S3_ENDPOINT", "http://host:9000")
    monkeypatch.setenv("ASLAN_S3_REGION", "us-east-1")
    monkeypatch.setenv("ASLAN_S3_ACCESS_KEY", "supersecret-do-not-leak")
    monkeypatch.setenv("ASLAN_S3_SECRET_KEY", "s")

    s = Settings()
    rep = repr(s)
    assert "supersecret-do-not-leak" not in rep
    assert "SecretStr" in rep


def test_settings_repr_does_not_leak_sentry_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ASLAN_SENTRY_DSN", "https://supersecret-do-not-leak@sentry.example/1")
    s = Settings()
    rep = repr(s)
    assert "supersecret-do-not-leak" not in rep
    assert "SecretStr" in rep


def test_secret_fields_are_secret_str_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every credential-bearing field is a ``SecretStr`` instance."""
    _set_required_env(monkeypatch)
    s = Settings()
    assert isinstance(s.postgres_dsn, SecretStr)
    assert isinstance(s.s3_access_key, SecretStr)
    assert isinstance(s.s3_secret_key, SecretStr)
    # ``sentry_dsn`` is None by default; covered separately by
    # ``test_sentry_dsn_is_secret_str`` when ASLAN_SENTRY_DSN is set.


def test_secret_fields_unwrap_via_get_secret_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """``.get_secret_value()`` returns the original env value verbatim."""
    monkeypatch.setenv("ASLAN_PG_DSN", "postgresql+asyncpg://u:p@host:6432/db")
    monkeypatch.setenv("ASLAN_REDIS_URL", "redis://host:6379/0")
    monkeypatch.setenv("ASLAN_S3_ENDPOINT", "http://host:9000")
    monkeypatch.setenv("ASLAN_S3_REGION", "us-east-1")
    monkeypatch.setenv("ASLAN_S3_ACCESS_KEY", "ak-123")
    monkeypatch.setenv("ASLAN_S3_SECRET_KEY", "sk-456")
    s = Settings()
    assert s.postgres_dsn.get_secret_value() == "postgresql+asyncpg://u:p@host:6432/db"
    assert s.s3_access_key.get_secret_value() == "ak-123"
    assert s.s3_secret_key.get_secret_value() == "sk-456"


def test_config_error_does_not_leak_pg_dsn_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex 2026-04-29: Pydantic's default validation errors include
    `input_value`. Settings catches ValidationError and re-raises ConfigError;
    without `hide_input_in_errors=True`, raw inputs (including DSNs containing
    passwords) leak through ConfigError text."""
    from aslan_core.config import Settings
    from aslan_core.errors import ConfigError

    monkeypatch.delenv("ASLAN_PG_DSN", raising=False)
    secret = "supersecret-do-not-leak-via-error"
    with pytest.raises(ConfigError) as exc_info:
        Settings(ASLAN_PG_DSN={"unexpected": f"postgresql://u:{secret}@h/d"})
    msg = str(exc_info.value)
    assert secret not in msg


def test_config_error_does_not_leak_s3_secret_input(monkeypatch: pytest.MonkeyPatch) -> None:
    from aslan_core.config import Settings
    from aslan_core.errors import ConfigError

    secret = "supersecret-s3-do-not-leak"
    monkeypatch.delenv("ASLAN_S3_SECRET_KEY", raising=False)
    with pytest.raises(ConfigError) as exc_info:
        Settings(ASLAN_S3_SECRET_KEY={"weird": secret})
    msg = str(exc_info.value)
    assert secret not in msg
