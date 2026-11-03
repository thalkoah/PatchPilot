"""Worker process. It claims queued tasks and triages them outside the API."""

import asyncio
import logging
import signal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchpilot.config import Settings, get_settings
from patchpilot.db.session import create_engine, create_session_factory
from patchpilot.llm.factory import create_provider
from patchpilot.llm.provider import LLMProvider
from patchpilot.repositories.tasks import ClaimedTask, TaskRepository
from patchpilot.workflow.graph import (
    GENERIC_FAILURE,
    STAGE_TRIAGE,
    CompiledStateGraph,
    compile_triage_graph,
    failure_outcome,
    run_triage,
)

logger = logging.getLogger(__name__)

# One claim at a time. Leases and a larger pool are a later task.
WORKER_CONCURRENCY = 1


async def process_one(
    session_factory: async_sessionmaker[AsyncSession],
    provider: LLMProvider,
    graph: CompiledStateGraph | None = None,
) -> bool:
    """Claim and finish the oldest queued task. Return False when the queue is empty."""
    compiled = graph if graph is not None else compile_triage_graph()
    async with session_factory() as session:
        claimed = await TaskRepository(session).claim_next_queued(stage=STAGE_TRIAGE)
    if claimed is None:
        return False
    await _finish(session_factory, provider, compiled, claimed)
    return True


async def process_task(
    session_factory: async_sessionmaker[AsyncSession],
    provider: LLMProvider,
    task_id: UUID,
    graph: CompiledStateGraph | None = None,
) -> bool:
    """Triage one task when it is still queued. Return False if another worker owns it."""
    compiled = graph if graph is not None else compile_triage_graph()
    async with session_factory() as session:
        claimed = await TaskRepository(session).claim_queued(task_id, stage=STAGE_TRIAGE)
    if claimed is None:
        return False
    await _finish(session_factory, provider, compiled, claimed)
    return True


async def serve(
    settings: Settings,
    *,
    provider: LLMProvider | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Poll until the process is interrupted. Model calls happen inside process_one."""
    owns_provider = provider is None
    if provider is None:
        provider = create_provider(settings)
    engine = None
    if session_factory is None:
        engine = create_engine(settings)
        session_factory = create_session_factory(engine)
    graph = compile_triage_graph()
    logger.info(
        "worker started provider=%s concurrency=%s",
        settings.llm_provider,
        WORKER_CONCURRENCY,
    )
    try:
        while True:
            try:
                worked = await process_one(session_factory, provider, graph)
            except Exception as exc:
                logger.error("worker iteration failed: %s", type(exc).__name__)
                await asyncio.sleep(settings.worker_idle_sleep_seconds)
                continue
            if not worked:
                await asyncio.sleep(settings.worker_idle_sleep_seconds)
    finally:
        if owns_provider:
            provider.close()
        if engine is not None:
            await engine.dispose()


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Python is PID 1 in the container, so the default SIGTERM is ignored.
    signal.signal(signal.SIGTERM, _stop_on_sigterm)
    try:
        asyncio.run(serve(settings))
    except KeyboardInterrupt:
        logger.info("worker stopped")


def _stop_on_sigterm(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


async def _finish(
    session_factory: async_sessionmaker[AsyncSession],
    provider: LLMProvider,
    graph: CompiledStateGraph,
    claimed: ClaimedTask,
) -> None:
    logger.info("claimed task=%s", claimed.id)
    try:
        outcome = run_triage(
            graph,
            task_id=claimed.id,
            title=claimed.title,
            description=claimed.description,
            provider=provider,
        )
    except Exception as exc:
        logger.error("triage workflow failed: %s", type(exc).__name__)
        outcome = failure_outcome(GENERIC_FAILURE)
    async with session_factory() as session:
        await TaskRepository(session).save_triage_result(
            claimed.id,
            status=outcome.status,
            stage=outcome.stage,
            result_summary=outcome.result_summary,
            result_json=outcome.result_json,
        )
    logger.info(
        "finished task=%s status=%s stage=%s",
        claimed.id,
        outcome.status.value,
        outcome.stage,
    )


if __name__ == "__main__":
    main()
