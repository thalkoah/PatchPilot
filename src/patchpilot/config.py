"""Typed runtime settings loaded from the environment."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from patchpilot.agents.limits import (
    DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS,
    DEFAULT_MAX_REPAIRS,
    HARD_MAX_INVESTIGATOR_TOOL_CALLS,
    HARD_MAX_REPAIRS,
)
from patchpilot.llm.budget import (
    DEFAULT_MAX_MODEL_CALLS,
    DEFAULT_MAX_RUNTIME_SECONDS,
    DEFAULT_MAX_TASK_TOOL_CALLS,
    DEFAULT_MAX_TOKENS,
    HARD_MAX_MODEL_CALLS,
    HARD_MAX_RUNTIME_SECONDS,
    HARD_MAX_TASK_TOOL_CALLS,
    HARD_MAX_TOKENS,
)
from patchpilot.llm.limits import (
    MAX_LLM_RETRIES,
    MAX_LLM_TIMEOUT_SECONDS,
    normalize_model_name,
    normalize_ollama_base_url,
)
from patchpilot.runner.limits import (
    DEFAULT_RUNNER_TIMEOUT_SECONDS,
    DEFAULT_RUNNER_WAIT_SECONDS,
    MAX_RUNNER_TIMEOUT_SECONDS,
    MAX_RUNNER_WAIT_SECONDS,
)

DEFAULT_WORKER_LEASE_SECONDS = 30.0
DEFAULT_WORKER_MAX_ATTEMPTS = 3
# Fits an 8 GB GPU with an 8k context. Larger tags such as gemma4:12b do not.
DEFAULT_LLM_MODEL = "qwen2.5-coder:7b"
# triage, plan, and supervisor are short classifications. investigate, develop,
# and review read or write code. An empty LLM_STAGE_MODELS uses LLM_MODEL for all.
DEFAULT_STAGE_MODELS = (
    "triage=ollama:qwen3.5:4b,"
    "investigate=ollama:qwen2.5-coder:7b,"
    "plan=ollama:qwen3.5:4b,"
    "develop=ollama:qwen2.5-coder:7b,"
    "review=ollama:qwen2.5-coder:7b,"
    "supervisor=ollama:qwen3.5:4b"
)


class LLMSettings(BaseSettings):
    """Model provider settings. The smoke script can load these without a database."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_provider: Literal["fake", "ollama", "openai"] = "ollama"
    llm_model: str = DEFAULT_LLM_MODEL
    ollama_base_url: str = "http://127.0.0.1:11434"
    llm_timeout_seconds: float = Field(default=180.0, gt=0, le=MAX_LLM_TIMEOUT_SECONDS)
    llm_max_retries: int = Field(default=2, ge=0, le=MAX_LLM_RETRIES)
    llm_retry_backoff_seconds: float = Field(default=0.5, ge=0, le=8)
    # Blank uses llm_provider and llm_model for every stage.
    # Example: triage=ollama:qwen3.5:4b,develop=ollama:qwen2.5-coder:7b
    llm_stage_models: str = DEFAULT_STAGE_MODELS
    ollama_tool_calling: bool = False
    # Cloud stays off until this is true and a base URL, key, and capability are set.
    llm_cloud_enabled: bool = False
    llm_cloud_base_url: str | None = None
    llm_cloud_api_key: SecretStr | None = None
    llm_cloud_capabilities: str = ""
    task_max_model_calls: int = Field(
        default=DEFAULT_MAX_MODEL_CALLS,
        ge=1,
        le=HARD_MAX_MODEL_CALLS,
    )
    task_max_tool_calls: int = Field(
        default=DEFAULT_MAX_TASK_TOOL_CALLS,
        ge=1,
        le=HARD_MAX_TASK_TOOL_CALLS,
    )
    task_max_tokens: int = Field(default=DEFAULT_MAX_TOKENS, ge=1, le=HARD_MAX_TOKENS)
    task_max_runtime_seconds: float = Field(
        default=DEFAULT_MAX_RUNTIME_SECONDS,
        ge=1,
        le=HARD_MAX_RUNTIME_SECONDS,
    )
    # Local model calls stay at one. This cannot be raised.
    task_max_parallelism: int = Field(default=1, ge=1, le=1)
    task_max_repairs: int = Field(default=DEFAULT_MAX_REPAIRS, ge=0, le=HARD_MAX_REPAIRS)
    llm_pricing_path: str | None = None

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

    @field_validator("llm_cloud_base_url", "llm_cloud_api_key", mode="before")
    @classmethod
    def blank_cloud_setting_is_absent(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("llm_cloud_base_url")
    @classmethod
    def cloud_base_url_is_http(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_ollama_base_url(value)

    @model_validator(mode="after")
    def cloud_routes_are_explicit(self) -> "LLMSettings":
        from patchpilot.llm.capabilities import require_capabilities
        from patchpilot.llm.errors import LLMCapabilityError
        from patchpilot.llm.routing import parse_stage_models, provider_capabilities

        try:
            choices = parse_stage_models(
                self.llm_stage_models,
                default_provider=self.llm_provider,
                default_model=self.llm_model,
                capabilities_for=lambda provider: provider_capabilities(self, provider),
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        uses_cloud = self.llm_provider == "openai" or any(
            choice.provider == "openai" for choice in choices.values()
        )
        if uses_cloud and not self.llm_cloud_enabled:
            raise ValueError("cloud model calls require llm_cloud_enabled")
        if self.llm_cloud_enabled and (
            self.llm_cloud_base_url is None or self.llm_cloud_api_key is None
        ):
            raise ValueError("cloud model calls require a base URL and an API key")
        for stage, choice in choices.items():
            try:
                require_capabilities(stage, choice.capabilities)
            except LLMCapabilityError as exc:
                raise ValueError(str(exc)) from None
        return self


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
    # When true, a model may recommend a supervisor route. Policy still decides
    # which routes are allowed. The default is rules only.
    supervisor_model: bool = False
    repository_root: str | None = None
    artifacts_root: str | None = None
    workspace_root: str | None = None
    runner_root: str | None = None
    runner_timeout_seconds: float = Field(
        default=DEFAULT_RUNNER_TIMEOUT_SECONDS,
        gt=0,
        le=MAX_RUNNER_TIMEOUT_SECONDS,
    )
    runner_wait_seconds: float = Field(
        default=DEFAULT_RUNNER_WAIT_SECONDS,
        gt=0,
        le=MAX_RUNNER_WAIT_SECONDS,
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # Publication stays off until this is true, a token is set, and the
    # repository is on the allowlist. The API and the runner do not read these.
    github_enabled: bool = False
    github_token: SecretStr | None = None
    github_api_base: str = "https://api.github.com"
    github_allowed_repos: str = ""

    @field_validator("github_token", mode="before")
    @classmethod
    def blank_github_token_is_absent(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("github_api_base")
    @classmethod
    def github_api_is_the_public_host(cls, value: str) -> str:
        if value != "https://api.github.com":
            raise ValueError("github_api_base must be https://api.github.com")
        return value

    @field_validator("github_allowed_repos")
    @classmethod
    def github_allowlist_is_owner_name(cls, value: str) -> str:
        from patchpilot.github.names import parse_allowed_repos

        try:
            parse_allowed_repos(value)
        except ValueError:
            raise ValueError("github_allowed_repos must be owner/name pairs") from None
        return value

    @field_validator("database_url")
    @classmethod
    def database_url_must_be_postgresql(cls, value: str) -> str:
        scheme = value.split(":", 1)[0]
        if scheme not in {"postgresql", "postgres"}:
            raise ValueError("database_url must use a postgresql:// or postgres:// URL")
        return value

    @model_validator(mode="after")
    def runner_wait_exceeds_timeout(self) -> "Settings":
        if self.runner_wait_seconds <= self.runner_timeout_seconds:
            raise ValueError("runner_wait_seconds must be greater than runner_timeout_seconds")
        return self

    @field_validator(
        "repository_root",
        "artifacts_root",
        "workspace_root",
        "runner_root",
        "llm_pricing_path",
        mode="before",
    )
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
