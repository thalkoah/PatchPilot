import pytest
from pydantic import ValidationError

from patchpilot.config import LLMSettings, Settings, get_settings


def test_settings_load_database_url_from_environment(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://patchpilot:patchpilot_dev@db:5432/patchpilot",
    )
    monkeypatch.setenv("DB_CONNECT_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.database_url.endswith("@db:5432/patchpilot")
    assert settings.db_connect_timeout_seconds == 5
    assert settings.log_level == "DEBUG"
    assert settings.worker_idle_sleep_seconds == 2.0
    assert settings.worker_lease_seconds == 30.0
    assert settings.worker_max_attempts == 3


def test_settings_reject_non_postgresql_url():
    with pytest.raises(ValidationError):
        Settings(database_url="sqlite:///tmp.db", _env_file=None)


def test_settings_reject_missing_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_llm_settings_default_to_the_local_models(monkeypatch):
    for name in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_STAGE_MODELS",
        "OLLAMA_BASE_URL",
        "LLM_TIMEOUT_SECONDS",
        "LLM_MAX_RETRIES",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(
        database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
        _env_file=None,
    )

    assert settings.llm_provider == "ollama"
    assert settings.llm_model == "qwen2.5-coder:7b"
    assert settings.llm_stage_models == (
        "triage=ollama:qwen3.5:4b,"
        "investigate=ollama:qwen2.5-coder:7b,"
        "plan=ollama:qwen3.5:4b,"
        "develop=ollama:qwen2.5-coder:7b,"
        "review=ollama:qwen2.5-coder:7b,"
        "supervisor=ollama:qwen3.5:4b"
    )
    assert settings.ollama_base_url == "http://127.0.0.1:11434"
    assert settings.llm_timeout_seconds == 180
    assert settings.llm_max_retries == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("llm_provider", "openai"),
        ("llm_model", "gemma 4"),
        ("ollama_base_url", "http://user:secret@127.0.0.1:11434"),
        ("ollama_base_url", "file:///tmp/ollama"),
        ("ollama_base_url", "http://127.0.0.1:11434/?token=secret"),
        ("llm_max_retries", 4),
        ("llm_timeout_seconds", 0),
        ("llm_timeout_seconds", 601),
    ],
)
def test_llm_settings_reject_unsafe_values(field, value):
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
            _env_file=None,
            **{field: value},
        )


def test_ollama_base_url_strips_a_trailing_slash():
    settings = Settings(
        database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
        ollama_base_url="http://host.docker.internal:11434/",
        _env_file=None,
    )

    assert settings.ollama_base_url == "http://host.docker.internal:11434"


def test_llm_settings_load_without_a_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for name in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_STAGE_MODELS",
        "OLLAMA_BASE_URL",
        "LLM_TIMEOUT_SECONDS",
        "LLM_MAX_RETRIES",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = LLMSettings(_env_file=None)

    assert settings.llm_provider == "ollama"
    assert settings.llm_model == "qwen2.5-coder:7b"


def test_llm_provider_name_is_case_insensitive():
    settings = Settings(
        database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
        llm_provider="FAKE",
        _env_file=None,
    )

    assert settings.llm_provider == "fake"


def test_repository_root_is_optional_and_must_be_absolute():
    settings = Settings(
        database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
        repository_root="   ",
        _env_file=None,
    )

    assert settings.repository_root is None

    rooted = Settings(
        database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
        repository_root="/var/patchpilot/repositories",
        _env_file=None,
    )
    assert rooted.repository_root == "/var/patchpilot/repositories"

    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
            repository_root="examples",
            _env_file=None,
        )


def test_artifacts_root_is_optional_and_must_be_absolute():
    settings = Settings(
        database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
        artifacts_root="   ",
        _env_file=None,
    )

    assert settings.artifacts_root is None

    rooted = Settings(
        database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
        artifacts_root="/var/patchpilot/artifacts",
        _env_file=None,
    )
    assert rooted.artifacts_root == "/var/patchpilot/artifacts"

    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
            artifacts_root="artifacts",
            _env_file=None,
        )


def test_workspace_root_is_optional_and_must_be_absolute():
    settings = Settings(
        database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
        workspace_root="   ",
        _env_file=None,
    )

    assert settings.workspace_root is None

    rooted = Settings(
        database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
        workspace_root="/var/patchpilot/workspaces",
        _env_file=None,
    )
    assert rooted.workspace_root == "/var/patchpilot/workspaces"

    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
            workspace_root="workspaces",
            _env_file=None,
        )


def test_runner_wait_must_exceed_the_timeout():
    settings = Settings(
        database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
        _env_file=None,
    )

    assert settings.runner_timeout_seconds == 30
    assert settings.runner_wait_seconds == 45
    assert settings.runner_root is None

    rooted = Settings(
        database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
        runner_root="/var/patchpilot/runner",
        _env_file=None,
    )
    assert rooted.runner_root == "/var/patchpilot/runner"

    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
            runner_root="runner",
            _env_file=None,
        )
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
            runner_timeout_seconds=30,
            runner_wait_seconds=30,
            _env_file=None,
        )


def test_investigator_budget_defaults_to_six_and_stays_bounded():
    settings = Settings(
        database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
        _env_file=None,
    )

    assert settings.investigator_max_tool_calls == 6
    assert settings.supervisor_model is False
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
            investigator_max_tool_calls=13,
            _env_file=None,
        )


def test_github_publication_is_off_until_it_is_configured():
    settings = Settings(
        database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
        _env_file=None,
    )

    assert settings.github_enabled is False
    assert settings.github_token is None
    assert settings.github_api_base == "https://api.github.com"
    assert settings.github_allowed_repos == ""
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
            github_api_base="https://example.invalid",
            _env_file=None,
        )
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
            github_allowed_repos="https://github.com/octocat/demo",
            _env_file=None,
        )


def test_settings_reject_timeout_below_one_second():
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
            db_connect_timeout_seconds=0,
            _env_file=None,
        )
