"""Typed runtime settings loaded from the environment."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from patchpilot.agents.limits import (
    DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS,
    HARD_MAX_INVESTIGATOR_TOOL_CALLS,
)
from patchpilot.llm.limits import (
    MAX_LLM_RETRIES,
    MAX_LLM_TIMEOUT_SECONDS,
    normalize_model_name,
    normalize_ollama_base_url,
)

DEFAULT_WORKER_LEASE_SECONDS = 30.0
DEFAULT_WORKER_MAX_ATTEMPTS = 3


class LLMSettings(BaseSettings):
    """Model provider settings. The smoke script can load these without a database."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_provider: Literal["fake", "ollama"] = "fake"
    llm_model: str = "gemma4:12b"
    ollama_base_url: str = "http://127.0.0.1:11434"
    llm_timeout_seconds: float = Field(default=180.0, gt=0, le=MAX_LLM_TIMEOUT_SECONDS)
    llm_max_retries: int = Field(default=2, ge=0, le=MAX_LLM_RETRIES)

    @field_validator("llm_provider", mode="before")
    @classmethod
    def llm_provider_is_lowercase(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().casefold()
        return value

    @field_validator("llm_model")
    @classmethod
    def llm_model_is_a_name(cls, value: str) -> str:
        return normalize_model_name(value)

    @field_validator("ollama_base_url")
    @classmethod
    def ollama_base_url_is_http(cls, value: str) -> str:
        return normalize_ollama_base_url(value)


class Settings(LLMSettings):
    """Process configuration. Secrets come from the environment, never from code."""

    database_url: str = Field(min_length=1)
    db_connect_timeout_seconds: int = Field(default=3, ge=1, le=30)
    worker_idle_sleep_seconds: float = Field(default=2.0, gt=0, le=3600)
    worker_lease_seconds: float = Field(default=DEFAULT_WORKER_LEASE_SECONDS, gt=0, le=3600)
    worker_max_attempts: int = Field(default=DEFAULT_WORKER_MAX_ATTEMPTS, ge=1, le=20)
    investigator_max_tool_calls: int = Field(
        default=DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS,
        ge=1,
        le=HARD_MAX_INVESTIGATOR_TOOL_CALLS,
    )
    repository_root: str | None = None
    artifacts_root: str | None = None
    workspace_root: str | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("database_url")
    @classmethod
    def database_url_must_be_postgresql(cls, value: str) -> str:
        scheme = value.split(":", 1)[0]
        if scheme not in {"postgresql", "postgres"}:
            raise ValueError("database_url must use a postgresql:// or postgres:// URL")
        return value

    @field_validator("repository_root", "artifacts_root", "workspace_root", mode="before")
    @classmethod
    def optional_path_is_absolute(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("path settings must be absolute paths")
        stripped = value.strip()
        if stripped == "":
            return None
        path = Path(stripped).expanduser()
        if not path.is_absolute():
            raise ValueError("path settings must be absolute paths")
        return str(path)


@lru_cache
def get_settings() -> Settings:
    return Settings()
