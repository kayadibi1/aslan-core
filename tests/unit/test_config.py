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
    assert s.postgres_dsn == "postgresql+asyncpg://u:p@host:6432/db"
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
