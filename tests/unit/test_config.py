import pytest

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
