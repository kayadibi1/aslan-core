from __future__ import annotations

from typing import Any

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from aslan_core.errors import ConfigError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    postgres_dsn: str = Field(validation_alias="ASLAN_PG_DSN")
    redis_url: str = Field(validation_alias="ASLAN_REDIS_URL")
    s3_endpoint: str = Field(validation_alias="ASLAN_S3_ENDPOINT")
    s3_region: str = Field(validation_alias="ASLAN_S3_REGION")
    s3_access_key: str = Field(validation_alias="ASLAN_S3_ACCESS_KEY")
    s3_secret_key: str = Field(validation_alias="ASLAN_S3_SECRET_KEY")

    def __init__(self, **kw: Any) -> None:
        try:
            super().__init__(**kw)
        except ValidationError as e:
            raise ConfigError(str(e)) from e
