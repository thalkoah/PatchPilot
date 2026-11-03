"""Wires HTTP requests to a database session. Routes depend on this, not on SQL."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchpilot.repositories.events import EventRepository
from patchpilot.repositories.tasks import TaskRepository


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session


def get_task_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TaskRepository:
    return TaskRepository(session)


def get_event_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> EventRepository:
    return EventRepository(session)
