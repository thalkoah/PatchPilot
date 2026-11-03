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


def test_llm_settings_default_to_the_offline_provider(monkeypatch):
    for name in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "OLLAMA_BASE_URL",
        "LLM_TIMEOUT_SECONDS",
        "LLM_MAX_RETRIES",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(
        database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
        _env_file=None,
    )

    assert settings.llm_provider == "fake"
    assert settings.llm_model == "gemma4:12b"
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
        "OLLAMA_BASE_URL",
        "LLM_TIMEOUT_SECONDS",
        "LLM_MAX_RETRIES",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = LLMSettings(_env_file=None)

    assert settings.llm_provider == "fake"
    assert settings.llm_model == "gemma4:12b"


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


def test_settings_reject_timeout_below_one_second():
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
            db_connect_timeout_seconds=0,
            _env_file=None,
        )
