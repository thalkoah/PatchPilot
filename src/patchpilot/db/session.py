"""Async SQLAlchemy engine. The public setting stays a plain postgresql:// URL."""

import os
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from patchpilot.config import Settings


def sqlalchemy_url(database_url: str) -> str:
    """Map a libpq URL onto the psycopg 3 SQLAlchemy dialect."""
    if database_url.startswith("postgresql+psycopg://"):
        return database_url
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgres://")
    raise ValueError("database_url must use a postgresql:// or postgres:// URL")


def migration_database_url(environ: Mapping[str, str] | None = None) -> str:
    """Return the SQLAlchemy URL Alembic should use. Never falls back to a built-in database."""
    source = os.environ if environ is None else environ
    raw = source.get("DATABASE_URL", "").strip()
    if not raw:
        raise RuntimeError("DATABASE_URL is required to run Alembic migrations")
    return sqlalchemy_url(raw)


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        sqlalchemy_url(settings.database_url),
        pool_pre_ping=True,
        connect_args={"connect_timeout": settings.db_connect_timeout_seconds},
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
