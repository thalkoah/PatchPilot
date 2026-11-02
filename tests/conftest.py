from collections.abc import Iterator

import pytest
from tests.database import disposable_postgres, truncate_tasks

from patchpilot.config import get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def postgres_database_url() -> Iterator[str]:
    """One migrated PostgreSQL database for the test session. Never the Compose volume."""
    with disposable_postgres() as database_url:
        yield database_url


@pytest.fixture
def clean_tasks(postgres_database_url: str) -> None:
    truncate_tasks(postgres_database_url)
