"""Postgres-backed triage graph for tests that follow the worker path."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.graph.state import CompiledStateGraph

from patchpilot.checkpoints import open_checkpointer, setup_checkpoints
from patchpilot.workflow.graph import compile_triage_graph


@asynccontextmanager
async def compiled_worker_graph(
    database_url: str,
    *,
    interrupt_after: list[str] | None = None,
) -> AsyncIterator[CompiledStateGraph]:
    await setup_checkpoints(database_url)
    async with open_checkpointer(database_url) as checkpointer:
        yield compile_triage_graph(checkpointer, interrupt_after=interrupt_after)
