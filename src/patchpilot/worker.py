"""Worker process. It claims queued tasks and triages them outside the API.

A task that names a registered repository is investigated after triage, then
planned. The tools open only that checkout, and only after the claim transaction
commits. The planner does not write that checkout. A plan that names a code
change pauses on a LangGraph interrupt: the worker stores a pending approval,
sets `waiting_approval`, and releases the lease. A decision queues the same
task again. The next claim resumes that thread with `Command(resume=...)`.
Approval clones the checkout into `WORKSPACE_ROOT` and records that association.
The developer then applies a patch in that private worktree. The test stage
copies that worktree to `RUNNER_ROOT` and waits for a result file. Pytest runs
in the separate runner process, not in this one. The reviewer reads that diff
and the test report and cannot edit the worktree. Policy may send the patch
back through develop and test at most twice. A failure that still cannot be
accepted is stored as `failed` and needs a person. Rejection and a declined
plan do not reach either stage. A declined plan is stored as a versioned
artifact and completes without asking.

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
from patchpilot.artifacts import ArtifactStoreError, TestReportStoreError
from patchpilot.checkpoints import open_checkpointer, setup_checkpoints
from patchpilot.config import (
    DEFAULT_WORKER_LEASE_SECONDS,
    DEFAULT_WORKER_MAX_ATTEMPTS,
    Settings,
    get_settings,
)
from patchpilot.db.models import APPROVAL_KIND_PUBLISH, TaskStatus
from patchpilot.db.session import create_engine, create_session_factory
from patchpilot.github.names import parse_allowed_repos
from patchpilot.github.publish import GitHubBinding, GitHubOptions, binding_for
from patchpilot.leases import LeaseKeeper
from patchpilot.llm.budget import TaskBudget
from patchpilot.llm.local_gate import LocalGate
from patchpilot.llm.meter import ModelCallDraft
from patchpilot.llm.pricing import load_pricing
from patchpilot.llm.provider import LLMProvider
from patchpilot.llm.routing import StageModels, UsageControl, build_router
from patchpilot.repositories.approvals import ApprovalRepository
from patchpilot.repositories.jobs import ClaimedTask, JobRepository
from patchpilot.repositories.model_calls import ModelCallRepository
from patchpilot.repositories.repos import RepoRepository
from patchpilot.runner.limits import DEFAULT_RUNNER_WAIT_SECONDS
from patchpilot.source.errors import SourceError
from patchpilot.source.tools import RepositoryTools, open_repository_tools
from patchpilot.workflow.graph import (
    ARTIFACT_FAILURE,
    GENERIC_FAILURE,
    PATCH_FAILURE,
    PLAN_FAILURE,
    REPOSITORY_UNAVAILABLE,
    STAGE_DEVELOP,
    STAGE_PLAN,
    STAGE_PUBLISH_APPROVAL,
    STAGE_TEST,
    STAGE_TRIAGE,
    STAGE_WORKSPACE,
    TEST_FAILURE,
    WORKSPACE_FAILURE,
    CompiledStateGraph,
    LeaseLost,
    TriageOutcome,
    compile_triage_graph,
    failure_outcome,
    run_claimed_triage,
)
from patchpilot.workspace.errors import WorkspaceError, WorkspaceStoreError
from patchpilot.workspace.git import remove_isolated_workspace

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
    workspace_root: Path | None = None,
    runner_root: Path | None = None,
    runner_wait_seconds: float = DEFAULT_RUNNER_WAIT_SECONDS,
    max_tool_calls: int = DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS,
    supervisor_model: bool = False,
    usage: UsageControl | None = None,
    github: GitHubOptions | None = None,
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
        workspace_root=workspace_root,
        runner_root=runner_root,
        runner_wait_seconds=runner_wait_seconds,
        max_tool_calls=max_tool_calls,
        supervisor_model=supervisor_model,
        usage=usage,
        github=github,
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
    workspace_root: Path | None = None,
    runner_root: Path | None = None,
    runner_wait_seconds: float = DEFAULT_RUNNER_WAIT_SECONDS,
    max_tool_calls: int = DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS,
    supervisor_model: bool = False,
    usage: UsageControl | None = None,
    github: GitHubOptions | None = None,
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
        workspace_root=workspace_root,
        runner_root=runner_root,
        runner_wait_seconds=runner_wait_seconds,
        max_tool_calls=max_tool_calls,
        supervisor_model=supervisor_model,
        usage=usage,
        github=github,
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
    router = None
    usage = None
    if provider is None:
        router = build_router(settings)
        provider = router.fallback()
        usage = UsageControl(
            router=router,
            pricing=load_pricing(settings.llm_pricing_path),
            max_model_calls=settings.task_max_model_calls,
            max_tool_calls=settings.task_max_tool_calls,
            max_tokens=settings.task_max_tokens,
            max_runtime_seconds=settings.task_max_runtime_seconds,
            max_parallelism=settings.task_max_parallelism,
            max_repairs=settings.task_max_repairs,
            database_url=settings.database_url,
            gate=LocalGate(),
        )
    engine = None
    factory = session_factory
    database_url = None if session_factory is not None else settings.database_url
    repository_root = Path(settings.repository_root) if settings.repository_root else None
    artifacts_root = Path(settings.artifacts_root) if settings.artifacts_root else None
    workspace_root = Path(settings.workspace_root) if settings.workspace_root else None
    runner_root = Path(settings.runner_root) if settings.runner_root else None
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
                    workspace_root=workspace_root,
                    runner_root=runner_root,
                    usage=usage,
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
                workspace_root=workspace_root,
                runner_root=runner_root,
                usage=usage,
            )
    finally:
        if router is not None:
            router.close()
        elif owns_provider and provider is not None:
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
    workspace_root: Path | None,
    runner_root: Path | None,
    usage: UsageControl | None = None,
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
                workspace_root=workspace_root,
                runner_root=runner_root,
                runner_wait_seconds=settings.runner_wait_seconds,
                max_tool_calls=settings.investigator_max_tool_calls,
                supervisor_model=settings.supervisor_model,
                usage=usage,
                github=github_options(settings),
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
    workspace_root: Path | None,
    runner_root: Path | None,
    runner_wait_seconds: float,
    max_tool_calls: int,
    supervisor_model: bool = False,
    usage: UsageControl | None = None,
    github: GitHubOptions | None = None,
) -> None:
    logger.info("claimed task=%s", claimed.id)
    budget, models = await _bind_models(session_factory, claimed.id, usage)
    tools, repository_error = await _open_tools(session_factory, claimed, repository_root)
    github_binding = await _github_binding(session_factory, claimed, github)
    resume_decision = await _load_resume_decision(session_factory, claimed.id)
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
                workspace_root=workspace_root,
                runner_root=runner_root,
                runner_wait_seconds=runner_wait_seconds,
                max_tool_calls=max_tool_calls,
                repository_error=repository_error,
                supervisor_model=supervisor_model,
                resume_decision=resume_decision,
                budget=budget,
                models=models,
                github=github_binding,
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
        if models is not None:
            await _store_model_calls(session_factory, models.take_records(), claimed.id)
    if lost:
        logger.warning("lease lost task=%s", claimed.id)
        return
    if outcome.status == TaskStatus.WAITING_APPROVAL:
        await _pause(
            session_factory,
            claimed,
            outcome,
            artifacts_root=artifacts_root,
        )
        return
    reviewed = bool(outcome.cycles)
    kept = outcome.status in {TaskStatus.COMPLETED, TaskStatus.REJECTED} or reviewed
    plan = outcome.plan if kept else None
    workspace = outcome.workspace if outcome.status == TaskStatus.COMPLETED else None
    patch = outcome.patch if outcome.status == TaskStatus.COMPLETED and not reviewed else None
    tests = outcome.tests if outcome.status == TaskStatus.COMPLETED and not reviewed else None
    try:
        async with session_factory() as session:
            saved = await JobRepository(session).finish_run(
                claimed,
                status=outcome.status,
                stage=outcome.stage,
                result_summary=outcome.result_summary,
                result_json=outcome.result_json,
                plan=plan,
                workspace=workspace,
                patch=patch,
                tests=tests,
                cycles=outcome.cycles,
                artifacts_root=artifacts_root,
                routes=outcome.routes,
            )
    except TestReportStoreError:
        logger.error("test report failed task=%s", claimed.id)
        _discard_workspace(workspace_root, claimed.id)
        outcome = failure_outcome(TEST_FAILURE, stage=STAGE_TEST)
        async with session_factory() as session:
            saved = await JobRepository(session).finish_run(
                claimed,
                status=outcome.status,
                stage=outcome.stage,
                result_summary=outcome.result_summary,
                result_json=outcome.result_json,
            )
    except ArtifactStoreError:
        logger.error("artifact store failed task=%s", claimed.id)
        _discard_workspace(workspace_root, claimed.id)
        if patch is not None:
            outcome = failure_outcome(PATCH_FAILURE, stage=STAGE_DEVELOP)
        else:
            outcome = failure_outcome(ARTIFACT_FAILURE, stage=STAGE_PLAN)
        async with session_factory() as session:
            saved = await JobRepository(session).finish_run(
                claimed,
                status=outcome.status,
                stage=outcome.stage,
                result_summary=outcome.result_summary,
                result_json=outcome.result_json,
            )
    except WorkspaceStoreError:
        logger.error("workspace row failed task=%s", claimed.id)
        _discard_workspace(workspace_root, claimed.id)
        outcome = failure_outcome(WORKSPACE_FAILURE, stage=STAGE_WORKSPACE)
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
    if outcome.status == TaskStatus.FAILED:
        _discard_workspace(workspace_root, claimed.id)
    logger.info(
        "finished task=%s status=%s stage=%s",
        claimed.id,
        outcome.status.value,
        outcome.stage,
    )


async def _pause(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedTask,
    outcome: TriageOutcome,
    *,
    artifacts_root: Path | None,
) -> None:
    """Persist the plan and release the lease. A lost lease leaves the task running."""
    if outcome.plan is None:
        outcome = failure_outcome(PLAN_FAILURE, stage=STAGE_PLAN)
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
    try:
        async with session_factory() as session:
            saved = await JobRepository(session).pause_for_approval(
                claimed,
                stage=outcome.stage,
                result_summary=outcome.result_summary,
                result_json=outcome.result_json,
                plan=outcome.plan,
                artifacts_root=artifacts_root,
                routes=outcome.routes,
                kind=(APPROVAL_KIND_PUBLISH if outcome.stage == STAGE_PUBLISH_APPROVAL else "plan"),
                patch=outcome.patch,
                tests=outcome.tests,
                cycles=outcome.cycles,
                workspace=outcome.workspace,
            )
    except ArtifactStoreError:
        logger.error("plan artifact failed task=%s", claimed.id)
        failed = failure_outcome(ARTIFACT_FAILURE, stage=STAGE_PLAN)
        async with session_factory() as session:
            saved = await JobRepository(session).finish_run(
                claimed,
                status=failed.status,
                stage=failed.stage,
                result_summary=failed.result_summary,
                result_json=failed.result_json,
            )
    if not saved:
        logger.warning("discarded result task=%s run=%s", claimed.id, claimed.run_id)
        return
    logger.info("waiting for approval task=%s", claimed.id)


async def _bind_models(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: UUID,
    usage: UsageControl | None,
) -> tuple[TaskBudget | None, StageModels | None]:
    """Attach this task's budget. A missing control keeps the single provider."""
    if usage is None:
        return None, None
    async with session_factory() as session:
        seed = await ModelCallRepository(session).seed(task_id)
    budget = TaskBudget.from_seed(
        seed,
        max_model_calls=usage.max_model_calls,
        max_tool_calls=usage.max_tool_calls,
        max_tokens=usage.max_tokens,
        max_runtime_seconds=usage.max_runtime_seconds,
        max_parallelism=usage.max_parallelism,
        max_repairs=usage.max_repairs,
    )
    models = usage.router.bind(
        task_id=task_id,
        budget=budget,
        pricing=usage.pricing,
        gate=usage.gate,
        advisory=True,
        database_url=usage.database_url,
    )
    return budget, models


async def _store_model_calls(
    session_factory: async_sessionmaker[AsyncSession],
    records: list[ModelCallDraft],
    task_id: UUID,
) -> None:
    if not records:
        return
    try:
        async with session_factory() as session:
            await ModelCallRepository(session).insert_many(records)
    except Exception:
        logger.error("model call record failed task=%s", task_id)


def github_options(settings: Settings) -> GitHubOptions | None:
    """Publication settings for the worker. A missing token leaves publication off."""
    if not settings.github_enabled or settings.github_token is None:
        return None
    return GitHubOptions(
        token=settings.github_token,
        allowed=parse_allowed_repos(settings.github_allowed_repos),
    )


async def _github_binding(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedTask,
    options: GitHubOptions | None,
) -> GitHubBinding | None:
    """The allowlisted repository for this task, or None when publication is off."""
    if options is None or claimed.repo_id is None:
        return None
    async with session_factory() as session:
        row = await RepoRepository(session).get(claimed.repo_id)
    if row is None or not row.enabled:
        return None
    return binding_for(row.registration, options)


async def _load_resume_decision(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: UUID,
) -> dict[str, object] | None:
    async with session_factory() as session:
        return await ApprovalRepository(session).resume_value(task_id)


def _discard_workspace(workspace_root: Path | None, task_id: UUID) -> None:
    """Remove a clone when the task will not keep the association."""
    if workspace_root is None:
        return
    try:
        remove_isolated_workspace(workspace_root, task_id)
    except WorkspaceError:
        logger.error("workspace cleanup failed task=%s", task_id)


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
