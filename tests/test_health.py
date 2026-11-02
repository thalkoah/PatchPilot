import logging

import psycopg
import pytest
from fastapi.testclient import TestClient

from patchpilot.api import create_app
from patchpilot.config import Settings, get_settings
from patchpilot.readiness import database_is_ready

UNREACHABLE_URL = "postgresql://patchpilot:super-secret-db-password@127.0.0.1:1/patchpilot"


@pytest.fixture
def unreachable_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_URL)
    monkeypatch.setenv("DB_CONNECT_TIMEOUT_SECONDS", "1")
    get_settings.cache_clear()


def test_live_does_not_contact_database(unreachable_database, monkeypatch):
    def fail_connect(*args, **kwargs):
        raise AssertionError("live probe must not open a database connection")

    monkeypatch.setattr("patchpilot.readiness.psycopg.connect", fail_connect)
    client = TestClient(create_app())

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "live"}


def test_ready_reports_ok_when_postgres_answers_select_one(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://patchpilot:patchpilot_dev@db:5432/patchpilot",
    )
    get_settings.cache_clear()

    class FakeCursor:
        def fetchone(self):
            return (1,)

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query):
            assert query == "SELECT 1"
            return FakeCursor()

    def fake_connect(database_url, *, autocommit, connect_timeout):
        assert database_url.startswith("postgresql://")
        assert autocommit is True
        assert connect_timeout == 3
        return FakeConnection()

    monkeypatch.setattr("patchpilot.readiness.psycopg.connect", fake_connect)
    client = TestClient(create_app())

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ready"}


def test_ready_fails_when_database_is_unreachable(unreachable_database, caplog):
    client = TestClient(create_app())

    with caplog.at_level(logging.WARNING):
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unavailable"}
    assert "super-secret-db-password" not in response.text
    assert "super-secret-db-password" not in caplog.text


def test_database_is_ready_rejects_unexpected_select_result(monkeypatch):
    class FakeCursor:
        def fetchone(self):
            return (0,)

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query):
            return FakeCursor()

    monkeypatch.setattr(
        "patchpilot.readiness.psycopg.connect",
        lambda *args, **kwargs: FakeConnection(),
    )
    settings = Settings(database_url=UNREACHABLE_URL, _env_file=None)

    assert database_is_ready(settings) is False


def test_database_is_ready_swallows_driver_errors_without_leaking_url(monkeypatch, caplog):
    def explode(*args, **kwargs):
        raise psycopg.OperationalError("connection failed")

    monkeypatch.setattr("patchpilot.readiness.psycopg.connect", explode)
    settings = Settings(database_url=UNREACHABLE_URL, db_connect_timeout_seconds=1, _env_file=None)

    with caplog.at_level(logging.WARNING):
        assert database_is_ready(settings) is False

    assert "super-secret-db-password" not in caplog.text
