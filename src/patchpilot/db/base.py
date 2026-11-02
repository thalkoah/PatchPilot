"""SQLAlchemy declarative base for business tables."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared metadata for Alembic and the ORM."""
