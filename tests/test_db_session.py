import pytest

from patchpilot.db.session import migration_database_url, sqlalchemy_url


def test_sqlalchemy_url_selects_psycopg_dialect():
    assert (
        sqlalchemy_url("postgresql://patchpilot:patchpilot_dev@db:5432/patchpilot")
        == "postgresql+psycopg://patchpilot:patchpilot_dev@db:5432/patchpilot"
    )
    assert sqlalchemy_url("postgres://user:secret@localhost/patchpilot") == (
        "postgresql+psycopg://user:secret@localhost/patchpilot"
    )
    already = "postgresql+psycopg://user:secret@localhost/patchpilot"
    assert sqlalchemy_url(already) == already


def test_sqlalchemy_url_rejects_other_drivers():
    with pytest.raises(ValueError, match="postgresql"):
        sqlalchemy_url("sqlite:///tmp.db")


def test_migration_requires_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
        migration_database_url()


def test_migration_rejects_blank_database_url():
    with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
        migration_database_url({"DATABASE_URL": "   "})


def test_migration_database_url_uses_psycopg_dialect():
    url = migration_database_url(
        {"DATABASE_URL": "postgresql://patchpilot:patchpilot_dev@db:5432/patchpilot"}
    )
    assert url.startswith("postgresql+psycopg://")
    assert "patchpilot_dev" in url
