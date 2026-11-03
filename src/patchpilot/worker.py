"""Worker process. It claims queued tasks and triages them outside the API.

A task that names a registered repository is investigated after triage, then
planned. The tools open only that checkout, and only after the claim transaction
commits. The planner does not write that checkout. A completed plan is stored
as a versioned artifact.

The process owns the LangGraph checkpointer connection: it opens one after
checkpoint setup and closes it when the loop stops. A claim stores a lease on
`task_runs` and commits before the model call. A separate thread renews that
lease. When a lease expires, the next poll recovers the task from its
checkpoint or fails it once `WORKER_MAX_ATTEMPTS` is exhausted.
"""

import asyncio
import logging
import signal
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchpilot.agents.limits import DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS
from patchpilot.artifacts import ArtifactStoreError
from patchpilot.checkpoints import open_checkpointer, setup_checkpoints
from patchpilot.config import (
    DEFAULT_WORKER_LEASE_SECONDS,
    DEFAULT_WORKER_MAX_ATTEMPTS,
    Settings,
    get_settings,
)
from patchpilot.db.models import TaskStatus
from patchpilot.db.session import create_engine, create_session_factory
from patchpilot.leases import LeaseKeeper
from patchpilot.llm.factory import create_provider
from patchpilot.llm.provider import LLMProvider
from patchpilot.repositories.jobs import ClaimedTask, JobRepository
from patchpilot.repositories.repos import RepoRepository
from patchpilot.source.errors import SourceError
from patchpilot.source.tools import RepositoryTools, open_repository_tools
from patchpilot.workflow.graph import (
    ARTIFACT_FAILURE,
    GENERIC_FAILURE,
    REPOSITORY_UNAVAILABLE,
    STAGE_PLAN,
    STAGE_TRIAGE,
    CompiledStateGraph,
    LeaseLost,
    compile_triage_graph,
    failure_outcome,
    run_claimed_triage,
)

logger = logging.getLogger(__name__)

# One claim at a time. The lease, not a second claim, keeps a task exclusive.
WORKER_CONCURRENCY = 1


async def process_one(
    session_factory: async_sessionmaker[AsyncSession],
    provider: LLMProvider,
    graph: CompiledStateGraph | None = None,
    *,
    worker_id: str | None = None,
    lease_seconds: float = DEFAULT_WORKER_LEASE_SECONDS,
    max_attempts: int = DEFAULT_WORKER_MAX_ATTEMPTS,
    database_url: str | None = None,
    repository_root: Path | None = None,
    artifacts_root: Path | None = None,
    max_tool_calls: int = DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS,
) -> bool:
    """Claim and finish one task. Return False when there is nothing to do.

    Pass a graph compiled with the Postgres checkpointer when the run must be
    durable. The default graph does not write checkpoints. `database_url` lets
    a background thread renew the lease while a synchronous model call runs.
    """
    compiled = graph if graph is not None else compile_triage_graph()
    owner = worker_id or new_worker_id()
    async with session_factory() as session:
        item = await JobRepository(session).take_next(
            stage=STAGE_TRIAGE,
            worker_id=owner,
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
        )
    if item.exhausted_task_id is not None:
        logger.info("recovery exhausted task=%s", item.exhausted_task_id)
    if item.claimed is None:
        return item.acted
    if item.recovered:
        logger.info(
            "recovered task=%s attempt=%s",
            item.claimed.id,
            item.claimed.attempt,
        )
    await _finish(
        session_factory,
        provider,
        compiled,
        item.claimed,
        lease_seconds=lease_seconds,
        database_url=database_url,
        repository_root=repository_root,
        artifacts_root=artifacts_root,
        max_tool_calls=max_tool_calls,
    )
    return True


async def process_task(
    session_factory: async_sessionmaker[AsyncSession],
    provider: LLMProvider,
    task_id: UUID,
    graph: CompiledStateGraph | None = None,
    *,
    worker_id: str | None = None,
    lease_seconds: float = DEFAULT_WORKER_LEASE_SECONDS,
    database_url: str | None = None,
    repository_root: Path | None = None,
    artifacts_root: Path | None = None,
    max_tool_calls: int = DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS,
) -> bool:
    """Triage one task when it is still queued. Return False if another worker owns it."""
    compiled = graph if graph is not None else compile_triage_graph()
    owner = worker_id or new_worker_id()
    async with session_factory() as session:
        claimed = await JobRepository(session).claim_queued(
            task_id,
            stage=STAGE_TRIAGE,
            worker_id=owner,
            lease_seconds=lease_seconds,
        )
    if claimed is None:
        return False
    await _finish(
        session_factory,
        provider,
        compiled,
        claimed,
        lease_seconds=lease_seconds,
        database_url=database_url,
        repository_root=repository_root,
        artifacts_root=artifacts_root,
        max_tool_calls=max_tool_calls,
    )
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
    factory = session_factory
    database_url = None if session_factory is not None else settings.database_url
    repository_root = Path(settings.repository_root) if settings.repository_root else None
    artifacts_root = Path(settings.artifacts_root) if settings.artifacts_root else None
    if factory is None:
        engine = create_engine(settings)
        factory = create_session_factory(engine)
    try:
        if session_factory is None:
            await setup_checkpoints(settings.database_url)
            async with open_checkpointer(settings.database_url) as checkpointer:
                graph = compile_triage_graph(checkpointer)
                await _poll(
                    settings,
                    factory,
                    provider,
                    graph,
                    "postgres",
                    database_url=database_url,
                    repository_root=repository_root,
                    artifacts_root=artifacts_root,
                )
        else:
            await _poll(
                settings,
                factory,
                provider,
                compile_triage_graph(),
                "none",
                database_url=None,
                repository_root=repository_root,
                artifacts_root=artifacts_root,
            )
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


def new_worker_id() -> str:
    """One id for this process. It is not a secret and it is not reused."""
    return uuid4().hex


def _stop_on_sigterm(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


async def _poll(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    provider: LLMProvider,
    graph: CompiledStateGraph,
    checkpointer_name: str,
    *,
    database_url: str | None,
    repository_root: Path | None,
    artifacts_root: Path | None,
) -> None:
    worker_id = new_worker_id()
    logger.info(
        "worker started provider=%s concurrency=%s checkpointer=%s worker_id=%s",
        settings.llm_provider,
        WORKER_CONCURRENCY,
        checkpointer_name,
        worker_id,
    )
    while True:
        try:
            worked = await process_one(
                session_factory,
                provider,
                graph,
                worker_id=worker_id,
                lease_seconds=settings.worker_lease_seconds,
                max_attempts=settings.worker_max_attempts,
                database_url=database_url,
                repository_root=repository_root,
                artifacts_root=artifacts_root,
                max_tool_calls=settings.investigator_max_tool_calls,
            )
        except Exception as exc:
            logger.error("worker iteration failed: %s", type(exc).__name__)
            await asyncio.sleep(settings.worker_idle_sleep_seconds)
            continue
        if not worked:
            await asyncio.sleep(settings.worker_idle_sleep_seconds)


async def _finish(
    session_factory: async_sessionmaker[AsyncSession],
    provider: LLMProvider,
    graph: CompiledStateGraph,
    claimed: ClaimedTask,
    *,
    lease_seconds: float,
    database_url: str | None,
    repository_root: Path | None,
    artifacts_root: Path | None,
    max_tool_calls: int,
) -> None:
    logger.info("claimed task=%s", claimed.id)
    tools, repository_error = await _open_tools(session_factory, claimed, repository_root)
    keeper = None
    if database_url is not None:
        keeper = LeaseKeeper(
            database_url,
            run_id=claimed.run_id,
            worker_id=claimed.worker_id,
            lease_seconds=lease_seconds,
        )
        keeper.start()
    try:
        try:
            outcome = await run_claimed_triage(
                graph,
                task_id=claimed.id,
                title=claimed.title,
                description=claimed.description,
                provider=provider,
                lease_lost=None if keeper is None else keeper.lost,
                repo_id=claimed.repo_id,
                repository_tools=tools,
                max_tool_calls=max_tool_calls,
                repository_error=repository_error,
            )
        except LeaseLost:
            logger.warning("lease lost task=%s", claimed.id)
            return
        except Exception as exc:
            logger.error("triage workflow failed: %s", type(exc).__name__)
            outcome = failure_outcome(GENERIC_FAILURE)
    finally:
        lost = False
        if keeper is not None:
            lost = not keeper.stop()
    if lost:
        logger.warning("lease lost task=%s", claimed.id)
        return
    plan = outcome.plan if outcome.status == TaskStatus.COMPLETED else None
    try:
        async with session_factory() as session:
            saved = await JobRepository(session).finish_run(
                claimed,
                status=outcome.status,
                stage=outcome.stage,
                result_summary=outcome.result_summary,
                result_json=outcome.result_json,
                plan=plan,
                artifacts_root=artifacts_root,
            )
    except ArtifactStoreError:
        logger.error("plan artifact failed task=%s", claimed.id)
        outcome = failure_outcome(ARTIFACT_FAILURE, stage=STAGE_PLAN)
        async with session_factory() as session:
            saved = await JobRepository(session).finish_run(
                claimed,
                status=outcome.status,
                stage=outcome.stage,
                result_summary=outcome.result_summary,
                result_json=outcome.result_json,
            )
    if not saved:
        logger.warning("discarded result task=%s run=%s", claimed.id, claimed.run_id)
        return
    logger.info(
        "finished task=%s status=%s stage=%s",
        claimed.id,
        outcome.status.value,
        outcome.stage,
    )


async def _open_tools(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedTask,
    repository_root: Path | None,
) -> tuple[RepositoryTools | None, str | None]:
    """Open the claimed checkout. A task with no repository does not touch the disk."""
    if claimed.repo_id is None:
        return None, None
    if repository_root is None:
        return None, REPOSITORY_UNAVAILABLE
    async with session_factory() as session:
        row = await RepoRepository(session).get(claimed.repo_id)
    if row is None or not row.enabled:
        return None, REPOSITORY_UNAVAILABLE
    try:
        return open_repository_tools(repository_root, row.canonical_path), None
    except SourceError:
        return None, REPOSITORY_UNAVAILABLE


if __name__ == "__main__":
    main()
