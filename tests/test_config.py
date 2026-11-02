import pytest
from pydantic import ValidationError

from patchpilot.config import Settings, get_settings


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
    assert settings.worker_idle_sleep_seconds == 30.0


def test_settings_reject_non_postgresql_url():
    with pytest.raises(ValidationError):
        Settings(database_url="sqlite:///tmp.db", _env_file=None)


def test_settings_reject_missing_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_reject_timeout_below_one_second():
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
            db_connect_timeout_seconds=0,
            _env_file=None,
        )
