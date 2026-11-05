"""Run the curated cases through the worker and score the stored tasks.

The fake provider never opens a socket. Ollama is built only when the caller
selects it. Cloud providers are not constructed.
"""

import asyncio
import json
import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import psycopg
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from patchpilot.benchmark.cases import BenchmarkCase, cases_for
from patchpilot.benchmark.prepare import (
    PreparedCheckouts,
    PrepareError,
    git_state,
    prepare_checkouts,
)
from patchpilot.benchmark.report import build_document
from patchpilot.benchmark.score import CaseResult, Observation, aggregate, score_case
from patchpilot.checkpoints import open_checkpointer, setup_checkpoints
from patchpilot.db.models import ApprovalStatus, Task, TaskStatus
from patchpilot.db.session import sqlalchemy_url
from patchpilot.llm.budget import (
    DEFAULT_MAX_MODEL_CALLS,
    DEFAULT_MAX_RUNTIME_SECONDS,
    DEFAULT_MAX_TASK_TOOL_CALLS,
    DEFAULT_MAX_TOKENS,
)
from patchpilot.llm.capabilities import MODEL_STAGES, ModelCapability
from patchpilot.llm.errors import LLMTimeoutError
from patchpilot.llm.fake import FakeProvider
from patchpilot.llm.local_gate import LocalGate
from patchpilot.llm.routing import StageChoice, StageRouter, UsageControl, build_router
from patchpilot.llm.triage import TriageResult
from patchpilot.llm.usage_report import ReportedUsage
from patchpilot.repositories.approvals import ApprovalRepository
from patchpilot.repositories.events import EventRepository
from patchpilot.repositories.model_calls import ModelCallRepository
from patchpilot.repositories.repos import RepoRepository
from patchpilot.repositories.tasks import TaskRepository
from patchpilot.runner.env import runner_process_env
from patchpilot.source.registry import register_checkout
from patchpilot.worker import process_one
from patchpilot.workflow.graph import compile_triage_graph

logger = logging.getLogger(__name__)

FAKE_MODEL = "benchmark-fake"
_FAKE_USAGE = ReportedUsage(prompt_tokens=20, output_tokens=10)
_APPROVE_REASON = "The plan matches the reported defect."
_REJECT_REASON = "Do not change the repository."
_CAPS = frozenset({ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOL_CALLING})


class BenchmarkError(Exception):
    """The harness could not start. The message does not include a database URL."""


@dataclass
class BenchmarkRun:
    document: dict[str, object]
    ok: bool


class _Recorded:
    """Shared across stage wrappers so a later stage can see the triage label."""

    def __init__(self) -> None:
        self.script = ""
        self.classification: str | None = None


class RecordingProvider:
    """Remember the validated triage label and, for fake, steer one fixture."""

    def __init__(self, inner: object, recorded: _Recorded) -> None:
        self._inner = inner
        self._recorded = recorded

    @property
    def script(self) -> str:
        return self._recorded.script

    @script.setter
    def script(self, value: str) -> None:
        self._recorded.script = value

    @property
    def classification(self) -> str | None:
        return self._recorded.classification

    @classification.setter
    def classification(self, value: str | None) -> None:
        self._recorded.classification = value

    def generate_structured(
        self,
        prompt: str,
        schema: type[object],
        options: object = None,
    ) -> object:
        script = self._recorded.script
        text = prompt if script == "" else f"{prompt}\nBenchmark script: {script}\n"
        result = self._inner.generate_structured(text, schema, options)  # type: ignore[attr-defined]
        if isinstance(result, TriageResult):
            self._recorded.classification = result.classification.value
        return result

    def take_usage(self) -> ReportedUsage | None:
        method = getattr(self._inner, "take_usage", None)
        if not callable(method):
            return None
        usage = method()
        return usage if isinstance(usage, ReportedUsage) else None

    def close(self) -> None:
        self._inner.close()  # type: ignore[attr-defined]


class LeaseStall:
    """Block the first model call until the open lease is expired, then continue.

    `generate_structured` is synchronous, so the worker loop cannot expire its
    own lease. The caller expires the row. This object waits until that UPDATE
    is visible, gives the renewal thread time to notice, and then calls the model.
    The graph drops the result when the renewal thread has lost the lease.
    """

    def __init__(self, inner: object, database_url: str) -> None:
        self._inner = inner
        self._database_url = database_url
        self._stalled = False
        self.started = threading.Event()

    def generate_structured(
        self,
        prompt: str,
        schema: type[object],
        options: object = None,
    ) -> object:
        if not self._stalled:
            self._stalled = True
            self.started.set()
            if not self._wait_until_expired():
                raise LLMTimeoutError("benchmark lease was not expired")
            time.sleep(1.0)
        return self._inner.generate_structured(prompt, schema, options)  # type: ignore[attr-defined]

    def take_usage(self) -> ReportedUsage | None:
        method = getattr(self._inner, "take_usage", None)
        if not callable(method):
            return None
        usage = method()
        return usage if isinstance(usage, ReportedUsage) else None

    def close(self) -> None:
        return None

    def _wait_until_expired(self) -> bool:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _lease_expired(self._database_url):
                return True
            time.sleep(0.05)
        return False


async def run_benchmark(
    *,
    provider: str,
    database_url: str,
    work_root: Path,
    ollama_settings: object | None = None,
) -> BenchmarkRun:
    """Execute the dataset. `work_root` holds copies, artifacts, and the runner directory."""
    if provider not in {"fake", "ollama"}:
        raise BenchmarkError("provider must be fake or ollama")
    _require_database_url(database_url)
    selected = cases_for(provider)
    checkouts = prepare_checkouts(work_root / "repositories")
    artifacts = work_root / "artifacts"
    workspaces = work_root / "workspaces"
    runner_root = work_root / "runner"
    artifacts.mkdir()
    workspaces.mkdir()
    engine = _engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    router, recording = _open_router(provider, ollama_settings)
    usage = _usage(router, database_url)
    runner = _start_runner(runner_root)
    results: list[CaseResult] = []
    try:
        await setup_checkpoints(database_url)
        repo_ids = await _register(factory, checkouts.root)
        async with open_checkpointer(database_url) as checkpointer:
            graph = compile_triage_graph(checkpointer)
            for case in cases_for("fake"):
                if case not in selected:
                    results.append(
                        score_case(
                            case,
                            None,
                            require_quality=False,
                            skip_reason="fixture-only fault, skipped for a live model",
                        )
                    )
                    continue
                recording.script = "" if provider == "ollama" else case.fake_script
                recording.classification = None
                results.append(
                    await _run_case(
                        case,
                        provider=provider,
                        database_url=database_url,
                        factory=factory,
                        graph=graph,
                        router=router,
                        recording=recording,
                        usage=usage,
                        checkouts=checkouts,
                        repo_ids=repo_ids,
                        artifacts_root=artifacts,
                        workspace_root=workspaces,
                        runner_root=runner_root,
                    )
                )
    finally:
        _stop_runner(runner)
        router.close()
        await engine.dispose()
    scored = tuple(results)
    summary = aggregate(scored)
    model = FAKE_MODEL if provider == "fake" else _ollama_model(router)
    document = build_document(provider=provider, model=model, results=scored, summary=summary)
    return BenchmarkRun(document=document, ok=summary["checks_failed"] == 0)


async def _run_case(
    case: BenchmarkCase,
    *,
    provider: str,
    database_url: str,
    factory: async_sessionmaker[AsyncSession],
    graph: object,
    router: StageRouter,
    recording: RecordingProvider,
    usage: UsageControl,
    checkouts: PreparedCheckouts,
    repo_ids: dict[str, UUID],
    artifacts_root: Path,
    workspace_root: Path,
    runner_root: Path,
) -> CaseResult:
    started = time.perf_counter()
    try:
        task_id = await _create(factory, case, repo_ids)
        if case.recovery:
            await _crash_and_recover(
                database_url=database_url,
                router=router,
                recording=recording,
                usage=usage,
                task_id=task_id,
                checkouts=checkouts,
                artifacts_root=artifacts_root,
                workspace_root=workspace_root,
                runner_root=runner_root,
            )
        else:
            claimed = await _process(
                factory,
                recording,
                graph,
                usage,
                worker_id=f"bench-{case.id}",
                lease_seconds=30,
                database_url=database_url,
                checkouts=checkouts,
                artifacts_root=artifacts_root,
                workspace_root=workspace_root,
                runner_root=runner_root,
            )
            if not claimed:
                raise BenchmarkError("task was not claimed")
            if case.decision is not None and await _status(factory, task_id) == "waiting_approval":
                await _decide(factory, task_id, case.decision)
                resumed = await _process(
                    factory,
                    recording,
                    graph,
                    usage,
                    worker_id=f"bench-{case.id}-resume",
                    lease_seconds=30,
                    database_url=database_url,
                    checkouts=checkouts,
                    artifacts_root=artifacts_root,
                    workspace_root=workspace_root,
                    runner_root=runner_root,
                )
                if not resumed:
                    raise BenchmarkError("approved task was not claimed")
        observation = await _load(
            factory,
            task_id,
            classification=recording.classification,
            latency_ms=_elapsed(started),
            checkout=_checkout_path(case, checkouts),
            source_unchanged=_unchanged(checkouts),
        )
    except Exception as exc:
        logger.error("benchmark case failed case=%s error=%s", case.id, type(exc).__name__)
        observation = _failed_observation(
            type(exc).__name__,
            latency_ms=_elapsed(started),
            checkout=_checkout_path(case, checkouts),
            source_unchanged=_unchanged(checkouts),
        )
    result = score_case(case, observation, require_quality=provider == "fake")
    logger.info("benchmark case=%s status=%s", case.id, result.status)
    return result


async def _crash_and_recover(
    *,
    database_url: str,
    router: StageRouter,
    recording: RecordingProvider,
    usage: UsageControl,
    task_id: UUID,
    checkouts: PreparedCheckouts,
    artifacts_root: Path,
    workspace_root: Path,
    runner_root: Path,
) -> None:
    choice = router.choices["triage"]
    key = (choice.provider, choice.model)
    original = router._providers[key]
    stall = LeaseStall(original, database_url)
    router._providers[key] = stall
    errors: list[BaseException] = []

    def _crash() -> None:
        try:
            asyncio.run(
                _crash_once(
                    database_url=database_url,
                    recording=recording,
                    usage=usage,
                    checkouts=checkouts,
                    artifacts_root=artifacts_root,
                    workspace_root=workspace_root,
                    runner_root=runner_root,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=_crash, name="benchmark-crash")
    thread.start()
    try:
        started = await asyncio.to_thread(stall.started.wait, 20)
        if not started:
            raise BenchmarkError("recovery case did not reach the model call")
        await asyncio.to_thread(_expire_leases, database_url)
        await asyncio.to_thread(thread.join, 40)
    finally:
        router._providers[key] = original
    if thread.is_alive():
        raise BenchmarkError("recovery case did not release the first worker")
    if errors:
        raise BenchmarkError(type(errors[0]).__name__)
    engine = _engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with open_checkpointer(database_url) as checkpointer:
            graph = compile_triage_graph(checkpointer)
            recovered = await _process(
                factory,
                recording,
                graph,
                usage,
                worker_id="benchmark-recover",
                lease_seconds=30,
                database_url=database_url,
                checkouts=checkouts,
                artifacts_root=artifacts_root,
                workspace_root=workspace_root,
                runner_root=runner_root,
            )
    finally:
        await engine.dispose()
    if not recovered:
        raise BenchmarkError("expired task was not recovered")


async def _crash_once(
    *,
    database_url: str,
    recording: RecordingProvider,
    usage: UsageControl,
    checkouts: PreparedCheckouts,
    artifacts_root: Path,
    workspace_root: Path,
    runner_root: Path,
) -> None:
    engine = _engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with open_checkpointer(database_url) as checkpointer:
            graph = compile_triage_graph(checkpointer)
            await _process(
                factory,
                recording,
                graph,
                usage,
                worker_id="benchmark-crash",
                lease_seconds=2,
                database_url=database_url,
                checkouts=checkouts,
                artifacts_root=artifacts_root,
                workspace_root=workspace_root,
                runner_root=runner_root,
            )
    finally:
        await engine.dispose()


async def _process(
    factory: async_sessionmaker[AsyncSession],
    provider: RecordingProvider,
    graph: object,
    usage: UsageControl,
    *,
    worker_id: str,
    lease_seconds: float,
    database_url: str,
    checkouts: PreparedCheckouts,
    artifacts_root: Path,
    workspace_root: Path,
    runner_root: Path,
) -> bool:
    return await process_one(
        factory,
        provider,  # type: ignore[arg-type]
        graph,  # type: ignore[arg-type]
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        database_url=database_url,
        repository_root=checkouts.root,
        artifacts_root=artifacts_root,
        workspace_root=workspace_root,
        runner_root=runner_root,
        runner_wait_seconds=45,
        usage=usage,
    )


async def _create(
    factory: async_sessionmaker[AsyncSession],
    case: BenchmarkCase,
    repo_ids: dict[str, UUID],
) -> UUID:
    repo_id = None if case.checkout == "none" else repo_ids[case.checkout]
    async with factory() as session:
        created = await TaskRepository(session).create(
            title=case.title,
            description=case.description,
            repo_id=repo_id,
        )
        return created.id


async def _decide(
    factory: async_sessionmaker[AsyncSession],
    task_id: UUID,
    decision: str,
) -> None:
    async with factory() as session:
        rows = await ApprovalRepository(session).list_for_task(task_id)
        pending_id = next(
            (row.id for row in rows if row.status == ApprovalStatus.PENDING),
            None,
        )
    if pending_id is None:
        raise BenchmarkError("approval row was not stored")
    reason = _APPROVE_REASON if decision == "approve" else _REJECT_REASON
    async with factory() as session:
        await ApprovalRepository(session).decide(task_id, pending_id, decision, reason)


async def _status(factory: async_sessionmaker[AsyncSession], task_id: UUID) -> str:
    async with factory() as session:
        task = await session.get(Task, task_id)
        if task is None:
            raise BenchmarkError("task was not stored")
        status = task.status
        return status.value if isinstance(status, TaskStatus) else str(status)


async def _load(
    factory: async_sessionmaker[AsyncSession],
    task_id: UUID,
    *,
    classification: str | None,
    latency_ms: int,
    checkout: Path | None,
    source_unchanged: bool,
) -> Observation:
    async with factory() as session:
        task = await session.get(Task, task_id)
        if task is None:
            raise BenchmarkError("task was not stored")
        events, _truncated = await EventRepository(session).list_for_task(task_id)
        calls = await ModelCallRepository(session).list_for_task(task_id)
        status = task.status.value if isinstance(task.status, TaskStatus) else str(task.status)
        stage = task.stage
        result = task.result_json if isinstance(task.result_json, dict) else {}
        summary = task.result_summary or ""
        event_types = tuple(event.event_type for event in events)
        payload_text = json.dumps([event.payload for event in events], default=str)
        outcomes = tuple(call.outcome for call in calls)
        stages = tuple(call.stage for call in calls)
        prompt, output, complete = _tokens(calls)
    return Observation(
        status=status,
        stage=stage,
        result=result,
        result_summary=summary,
        event_types=event_types,
        payload_text=payload_text,
        model_outcomes=outcomes,
        model_stages=stages,
        prompt_tokens=prompt,
        output_tokens=output,
        tokens_complete=complete,
        latency_ms=latency_ms,
        observed_classification=classification,
        source_unchanged=source_unchanged,
        checkout=checkout,
    )


async def _register(
    factory: async_sessionmaker[AsyncSession],
    repository_root: Path,
) -> dict[str, UUID]:
    async with factory() as session:
        repository = RepoRepository(session)
        shared = await register_checkout(
            repository,
            repository_root=repository_root,
            relative_path="demo_service",
            display_name="Demo service",
        )
        injected = await register_checkout(
            repository,
            repository_root=repository_root,
            relative_path="demo_injected",
            display_name="Demo service with a hostile comment",
        )
        return {"shared": shared.id, "injected": injected.id}


def _open_router(provider: str, settings: object | None) -> tuple[StageRouter, RecordingProvider]:
    recorded = _Recorded()
    if provider == "fake":
        recording = RecordingProvider(FakeProvider(usage=_FAKE_USAGE), recorded)
        choices = {
            stage: StageChoice(
                provider="fake",
                model=FAKE_MODEL,
                capabilities=_CAPS,
                local=False,
            )
            for stage in MODEL_STAGES
        }
        return StageRouter(choices, {("fake", FAKE_MODEL): recording}), recording
    if settings is None:
        raise BenchmarkError("ollama settings are required")
    forced = settings.model_copy(update={"llm_provider": "ollama", "llm_stage_models": ""})  # type: ignore[attr-defined]
    router = build_router(forced)
    recording: RecordingProvider | None = None
    for key, inner in list(router._providers.items()):
        wrapped = RecordingProvider(inner, recorded)
        router._providers[key] = wrapped
        if recording is None:
            recording = wrapped
    if recording is None:
        raise BenchmarkError("ollama provider was not built")
    return router, recording


def _usage(router: StageRouter, database_url: str) -> UsageControl:
    from patchpilot.agents.limits import DEFAULT_MAX_REPAIRS

    return UsageControl(
        router=router,
        pricing=None,
        max_model_calls=DEFAULT_MAX_MODEL_CALLS,
        max_tool_calls=DEFAULT_MAX_TASK_TOOL_CALLS,
        max_tokens=DEFAULT_MAX_TOKENS,
        max_runtime_seconds=DEFAULT_MAX_RUNTIME_SECONDS,
        max_parallelism=1,
        max_repairs=DEFAULT_MAX_REPAIRS,
        database_url=database_url,
        gate=LocalGate(),
    )


def _engine(database_url: str) -> AsyncEngine:
    return create_async_engine(sqlalchemy_url(database_url), poolclass=NullPool)


def _start_runner(root: Path) -> subprocess.Popen[bytes]:
    root.mkdir(parents=True, exist_ok=True)
    env = runner_process_env(root=root, timeout_seconds=20, poll_seconds=0.05)
    return subprocess.Popen(
        [sys.executable, "-m", "patchpilot.runner"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stop_runner(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _expire_leases(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(
            """
            UPDATE task_runs
            SET lease_until = now() - interval '1 second'
            WHERE ended_at IS NULL
            """
        )


def _lease_expired(database_url: str) -> bool:
    with psycopg.connect(database_url, autocommit=True) as connection:
        row = connection.execute(
            """
            SELECT lease_until < now()
            FROM task_runs
            WHERE ended_at IS NULL
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
    return row is not None and bool(row[0])


def _tokens(calls: list[object]) -> tuple[int | None, int | None, bool]:
    prompt_values: list[int] = []
    output_values: list[int] = []
    complete = True
    for call in calls:
        prompt = getattr(call, "prompt_tokens", None)
        output = getattr(call, "output_tokens", None)
        if not isinstance(prompt, int) or not isinstance(output, int):
            complete = False
        if isinstance(prompt, int):
            prompt_values.append(prompt)
        if isinstance(output, int):
            output_values.append(output)
    prompt_total = None if not prompt_values else sum(prompt_values)
    output_total = None if not output_values else sum(output_values)
    return prompt_total, output_total, complete and bool(calls)


def _checkout_path(case: BenchmarkCase, checkouts: PreparedCheckouts) -> Path | None:
    if case.checkout == "shared":
        return checkouts.shared
    if case.checkout == "injected":
        return checkouts.injected
    return None


def _unchanged(checkouts: PreparedCheckouts) -> bool:
    try:
        return (
            git_state(checkouts.shared) == checkouts.shared_state
            and git_state(checkouts.injected) == checkouts.injected_state
        )
    except PrepareError:
        return False


def _failed_observation(
    message: str,
    *,
    latency_ms: int,
    checkout: Path | None,
    source_unchanged: bool,
) -> Observation:
    return Observation(
        status="error",
        stage=None,
        result={"error": message},
        result_summary=message,
        event_types=(),
        payload_text="",
        model_outcomes=(),
        model_stages=(),
        prompt_tokens=None,
        output_tokens=None,
        tokens_complete=False,
        latency_ms=latency_ms,
        observed_classification=None,
        source_unchanged=source_unchanged,
        checkout=checkout,
    )


def _elapsed(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _ollama_model(router: StageRouter) -> str:
    return router.choices["triage"].model


def _require_database_url(database_url: str) -> None:
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise BenchmarkError("database_url must be a postgresql URL")
