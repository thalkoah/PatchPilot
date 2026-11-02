"""Typed runtime settings loaded from the environment."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration. Secrets come from the environment, never from code."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(min_length=1)
    db_connect_timeout_seconds: int = Field(default=3, ge=1, le=30)
    worker_idle_sleep_seconds: float = Field(default=30.0, gt=0, le=3600)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("database_url")
    @classmethod
    def database_url_must_be_postgresql(cls, value: str) -> str:
        scheme = value.split(":", 1)[0]
        if scheme not in {"postgresql", "postgres"}:
            raise ValueError("database_url must use a postgresql:// or postgres:// URL")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
