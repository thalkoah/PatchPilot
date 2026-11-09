"""Triage workflow.

Every specialist node returns to `supervisor`. Deterministic rules choose the
next node. A model may recommend a route only when those rules already allow
it, and it cannot approve work or write files. A task with no repository is
finalized after triage. A task with a repo_id is investigated, then planned.
Incomplete evidence can investigate once more. A plan whose paths are all
forbidden can be replanned once. A plan that names a code change goes to
`request_plan_approval`, which calls `interrupt()` and does no I/O before that
call, because resume runs the node from the top. Approval continues to
`prepare_workspace`, then one baseline test of the unmodified worktree, then
`develop`, then `test`, then `review`. Policy may repair at most twice. A
patched suite that only repeats baseline failures can be accepted. Rejection,
a decline, and an escalation end at `finalize`.
When a GitHub target is configured, an accepted review pauses again for a
second approval. Nothing is pushed before that approval. Declining publication
finishes the local task without a pull request.

The investigate node reads the registered checkout. The plan node proposes
paths and does not write them. `prepare_workspace` clones that checkout into
the workspace volume. It does not read a path from the plan. `develop` applies
a patch only inside that private worktree. `test` copies that worktree to the
runner volume and waits for a result file. It does not start a test process.
A repair publishes a new runner job id so the suite runs again. Nothing in
this graph writes the registered checkout.

The worker compiles this graph with an AsyncPostgresSaver. The task UUID is the
LangGraph thread id. Checkpoint rows are not the business task record: the
worker still writes status, stage, summary, and result_json on `tasks`.

`durability="sync"` stores each finished node before the next one starts, so a
later resume does not run that node again.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, TypedDict
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from patchpilot.agents.developer import DeveloperAgent, DevelopOutcome
from patchpilot.agents.errors import StopForLease
from patchpilot.agents.investigator import InvestigatorAgent
from patchpilot.agents.limits import (
    DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS,
    DEFAULT_MAX_REPAIRS,
    DEFAULT_SUPERVISOR_DEADLINE_SECONDS,
    GRAPH_RECURSION_LIMIT,
    require_supervisor_deadline,
)
from patchpilot.agents.planner import PlannerAgent
from patchpilot.agents.review_policy import (
    PolicyAction,
    applied_diff_is_allowed,
    decide_review,
    failure_identities,
    has_new_failure,
)
from patchpilot.agents.reviewer import ReviewedCycle, ReviewerAgent
from patchpilot.agents.supervisor import (
    NODE_FOR_ROUTE,
    SupervisorDecision,
    decide_route,
    route_trace,
    with_model,
)
from patchpilot.artifacts import checksum, proposal_document, review_document
from patchpilot.db.models import APPROVAL_REASON_MAX_LENGTH, TaskStatus
from patchpilot.github.errors import GitHubError
from patchpilot.github.publish import GitHubBinding, publish_draft
from patchpilot.llm.budget import TaskBudget
from patchpilot.llm.errors import LLMError
from patchpilot.llm.investigation import InvestigationReport
from patchpilot.llm.patch import PatchProposal
from patchpilot.llm.plan import ChangePlan
from patchpilot.llm.provider import LLMProvider
from patchpilot.llm.review import FindingSeverity, ReviewResult
from patchpilot.llm.routing import StageModels
from patchpilot.llm.supervisor import SupervisorRoute
from patchpilot.llm.triage import TriageResult
from patchpilot.patching.apply import MAX_DIFF_BYTES, AppliedPatch
from patchpilot.patching.errors import PatchError
from patchpilot.runner.client import baseline_job_id, collect_test_report, test_job_id
from patchpilot.runner.errors import RunnerRejected, RunnerUnavailable
from patchpilot.runner.limits import DEFAULT_RUNNER_WAIT_SECONDS
from patchpilot.runner.protocol import TestReport, TestStatus
from patchpilot.source.tools import RepositoryTools
from patchpilot.workspace.errors import WorkspaceError
from patchpilot.workspace.git import prepare_isolated_workspace
from patchpilot.workspace.layout import (
    IsolatedWorkspace,
    require_workspace_root,
    workspace_from_state,
    worktree_relative,
)

logger = logging.getLogger(__name__)

STAGE_TRIAGE = "triage"
STAGE_INVESTIGATE = "investigate"
STAGE_PLAN = "plan"
STAGE_APPROVAL = "request_plan_approval"
STAGE_WORKSPACE = "prepare_workspace"
STAGE_DEVELOP = "develop"
STAGE_TEST = "test"
STAGE_BASELINE = "baseline"
STAGE_REVIEW = "review"
STAGE_PUBLISH_APPROVAL = "request_publish_approval"
STAGE_PUBLISH = "publish"
STAGE_SUPERVISOR = "supervisor"
STAGE_FINALIZE = "finalize"
GENERIC_FAILURE = "Triage failed before a valid result was produced."
INVESTIGATION_FAILURE = "Investigation failed before a valid report was produced."
PLAN_FAILURE = "Planning failed before a valid change plan was produced."
APPROVAL_FAILURE = "Approval resume did not continue the workflow."
WORKSPACE_FAILURE = "The isolated workspace could not be created."
PATCH_FAILURE = "The patch was rejected before it was applied."
SYNTAX_PATCH = "The patch is not valid Python."
MISMATCH_PATCH = "The edit did not match the file."
EMPTY_PATCH = "The repair did not change any approved file."
TEST_FAILURE = "The test result could not be read."
REVIEW_FAILURE = "The review could not be completed."
PUBLISH_FAILURE = "The draft pull request was not opened."
RUNNER_UNAVAILABLE = "The test runner did not return a result."
RUNNER_REJECTED = "The test runner rejected the workspace."
ARTIFACT_FAILURE = "The plan artifact could not be stored."
REPOSITORY_UNAVAILABLE = "The registered repository could not be opened."
REJECTED_SUMMARY = "The plan was rejected."
MAX_FAILURE_CHARS = 500
_FAILURE_STAGES = frozenset(
    {
        STAGE_TRIAGE,
        STAGE_INVESTIGATE,
        STAGE_PLAN,
        STAGE_APPROVAL,
        STAGE_WORKSPACE,
        STAGE_DEVELOP,
        STAGE_TEST,
        STAGE_REVIEW,
        STAGE_PUBLISH,
    }
)
_INTERRUPT = "__interrupt__"
# Persist the checkpoint before the next node. "async" can still be in flight
# when a node, or the process, ends.
CHECKPOINT_DURABILITY = "sync"

WorkflowStatus = Literal["running", "completed", "failed", "rejected"]


class TriageState(TypedDict):
    """Small values only. The model provider is run context, not state."""

    task_id: str
    title: str
    description: str
    repo_id: str | None
    triage: dict[str, Any] | None
    investigation: dict[str, Any] | None
    plan: dict[str, Any] | None
    approval: dict[str, Any] | None
    workspace: dict[str, Any] | None
    patch: dict[str, Any] | None
    baseline_tests: dict[str, Any] | None
    tests: dict[str, Any] | None
    review: dict[str, Any] | None
    publication: dict[str, Any] | None
    repair_count: int
    cycles: list[dict[str, Any]]
    policy: str | None
    phase: str
    route: str | None
    routes: list[dict[str, Any]]
    investigation_count: int
    plan_count: int
    supervisor_steps: int
    started_at: float | None
    result_summary: str | None
    error: str | None
    failed_stage: str | None
    status: WorkflowStatus


GraphState = TriageState | dict[str, Any]


class LeaseLost(Exception):
    """This worker no longer holds the lease, so the node must not checkpoint a success."""


@dataclass(frozen=True, slots=True)
class WorkflowContext:
    """Dependencies for one run. They are not written to a checkpoint."""

    provider: LLMProvider
    lease_lost: Callable[[], bool] | None = None
    repository_tools: RepositoryTools | None = None
    workspace_root: Path | None = None
    runner_root: Path | None = None
    runner_wait_seconds: float = DEFAULT_RUNNER_WAIT_SECONDS
    max_tool_calls: int = DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS
    max_repairs: int = DEFAULT_MAX_REPAIRS
    repository_error: str | None = None
    supervisor_model: bool = False
    supervisor_deadline_seconds: float = DEFAULT_SUPERVISOR_DEADLINE_SECONDS
    budget: TaskBudget | None = None
    models: StageModels | None = None
    github: GitHubBinding | None = None


CompiledGraph = CompiledStateGraph[TriageState, WorkflowContext, TriageState, TriageState]


@dataclass(frozen=True, slots=True)
class TriageOutcome:
    """What the worker stores after the graph returns."""

    status: TaskStatus
    stage: str
    result_summary: str
    result_json: dict[str, Any]
    plan: ChangePlan | None = None
    workspace: IsolatedWorkspace | None = None
    publication: dict[str, Any] | None = None
    patch: DevelopOutcome | None = None
    tests: TestReport | None = None
    review: ReviewResult | None = None
    policy: str | None = None
    repair_count: int = 0
    repairs: tuple[dict[str, Any], ...] = ()
    cycles: tuple[ReviewedCycle, ...] = ()
    routes: tuple[dict[str, Any], ...] = ()


def _provider(context: WorkflowContext, stage: str) -> LLMProvider:
    """The stage's model when routing is configured, otherwise the single provider."""
    if context.models is not None:
        return context.models.for_stage(stage)
    return context.provider


def triage_prompt(*, title: str, description: str) -> str:
    """The provider sees the report text and nothing from the database."""
    return f"Title: {title}\nDescription:\n{description}\n"


def thread_config(task_id: UUID) -> RunnableConfig:
    """Stable LangGraph thread. The id is the task UUID and nothing else.

    `recursion_limit` stops a routing loop. The supervisor step cap trips first
    on a normal task.
    """
    return {
        "configurable": {"thread_id": str(task_id)},
        "recursion_limit": GRAPH_RECURSION_LIMIT,
    }


def initial_triage_state(
    *,
    task_id: UUID,
    title: str,
    description: str,
    repo_id: UUID | None = None,
) -> TriageState:
    return {
        "task_id": str(task_id),
        "title": title,
        "description": description,
        "repo_id": None if repo_id is None else str(repo_id),
        "triage": None,
        "investigation": None,
        "plan": None,
        "approval": None,
        "workspace": None,
        "patch": None,
        "baseline_tests": None,
        "tests": None,
        "review": None,
        "publication": None,
        "repair_count": 0,
        "cycles": [],
        "policy": None,
        "phase": "",
        "route": None,
        "routes": [],
        "investigation_count": 0,
        "plan_count": 0,
        "supervisor_steps": 0,
        "started_at": None,
        "result_summary": None,
        "error": None,
        "failed_stage": None,
        "status": "running",
    }


def compile_triage_graph(
    checkpointer: BaseCheckpointSaver | Literal[False] = False,
    *,
    interrupt_after: list[str] | None = None,
) -> CompiledGraph:
    """Compile the triage graph.

    The default saver is off so a graph unit test can run without PostgreSQL.
    Pass `checkpointer=False` explicitly when that is the intent. The worker
    passes the Postgres saver instead.
    """
    builder: StateGraph[TriageState, WorkflowContext, TriageState, TriageState] = StateGraph(
        TriageState,
        context_schema=WorkflowContext,
    )
    builder.add_node("triage", triage_node)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("investigate", investigate_node)
    builder.add_node("plan", plan_node)
    builder.add_node("request_plan_approval", request_plan_approval)
    builder.add_node("prepare_workspace", prepare_workspace_node)
    builder.add_node("develop", develop_node)
    builder.add_node("test", test_node)
    builder.add_node("review", review_node)
    builder.add_node("request_publish_approval", request_publish_approval)
    builder.add_node("publish", publish_node)
    builder.add_node("finalize", finalize_node)
    builder.add_edge(START, "triage")
    for name in (
        "triage",
        "investigate",
        "plan",
        "request_plan_approval",
        "prepare_workspace",
        "develop",
        "test",
        "review",
        "request_publish_approval",
        "publish",
    ):
        builder.add_edge(name, "supervisor")
    builder.add_conditional_edges(
        "supervisor",
        route_from_supervisor,
        {
            "investigate": "investigate",
            "plan": "plan",
            "request_plan_approval": "request_plan_approval",
            "prepare_workspace": "prepare_workspace",
            "develop": "develop",
            "test": "test",
            "review": "review",
            "request_publish_approval": "request_publish_approval",
            "publish": "publish",
            "finalize": "finalize",
        },
    )
    builder.add_edge("finalize", END)
    options: dict[str, Any] = {}
    if interrupt_after:
        options["interrupt_after"] = interrupt_after
    return builder.compile(checkpointer=checkpointer, **options)


async def run_triage(
    graph: CompiledGraph,
    *,
    task_id: UUID,
    title: str,
    description: str,
    provider: LLMProvider,
    lease_lost: Callable[[], bool] | None = None,
    repo_id: UUID | None = None,
    repository_tools: RepositoryTools | None = None,
    workspace_root: Path | None = None,
    runner_root: Path | None = None,
    runner_wait_seconds: float = DEFAULT_RUNNER_WAIT_SECONDS,
    max_tool_calls: int = DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS,
    repository_error: str | None = None,
    supervisor_model: bool = False,
    supervisor_deadline_seconds: float = DEFAULT_SUPERVISOR_DEADLINE_SECONDS,
    budget: TaskBudget | None = None,
    models: StageModels | None = None,
    github: GitHubBinding | None = None,
) -> TriageOutcome:
    """Run one triage. Provider failures become a failed outcome instead of raw text."""
    state = initial_triage_state(
        task_id=task_id,
        title=title,
        description=description,
        repo_id=repo_id,
    )
    try:
        final = await invoke_triage(
            graph,
            state,
            task_id=task_id,
            provider=provider,
            lease_lost=lease_lost,
            repository_tools=repository_tools,
            workspace_root=workspace_root,
            runner_root=runner_root,
            runner_wait_seconds=runner_wait_seconds,
            max_tool_calls=max_tool_calls,
            repository_error=repository_error,
            supervisor_model=supervisor_model,
            supervisor_deadline_seconds=supervisor_deadline_seconds,
            budget=budget,
            models=models,
            github=github,
        )
    except LeaseLost:
        raise
    except Exception as exc:
        logger.error("triage graph failed: %s", type(exc).__name__)
        return failure_outcome(GENERIC_FAILURE)
    return outcome_from_state(final)


async def run_claimed_triage(
    graph: CompiledGraph,
    *,
    task_id: UUID,
    title: str,
    description: str,
    provider: LLMProvider,
    lease_lost: Callable[[], bool] | None = None,
    repo_id: UUID | None = None,
    repository_tools: RepositoryTools | None = None,
    workspace_root: Path | None = None,
    runner_root: Path | None = None,
    runner_wait_seconds: float = DEFAULT_RUNNER_WAIT_SECONDS,
    max_tool_calls: int = DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS,
    repository_error: str | None = None,
    supervisor_model: bool = False,
    supervisor_deadline_seconds: float = DEFAULT_SUPERVISOR_DEADLINE_SECONDS,
    budget: TaskBudget | None = None,
    models: StageModels | None = None,
    github: GitHubBinding | None = None,
    resume_decision: dict[str, Any] | None = None,
) -> TriageOutcome:
    """Resume a checkpointed thread, or start one when no checkpoint exists.

    A thread paused on `interrupt()` stays paused until `resume_decision` is
    passed to `Command(resume=...)`. A finished thread returns its stored
    outcome and does not call the model again.
    """
    if graph.checkpointer:
        snapshot = await graph.aget_state(thread_config(task_id))
        if snapshot.interrupts:
            if resume_decision is None:
                values = snapshot.values if isinstance(snapshot.values, dict) else {}
                return _pause_or_fail(dict(values))
            try:
                final = await invoke_triage(
                    graph,
                    None,
                    task_id=task_id,
                    provider=provider,
                    resume_decision=resume_decision,
                    lease_lost=lease_lost,
                    repository_tools=repository_tools,
                    workspace_root=workspace_root,
                    runner_root=runner_root,
                    runner_wait_seconds=runner_wait_seconds,
                    max_tool_calls=max_tool_calls,
                    repository_error=repository_error,
                    supervisor_model=supervisor_model,
                    supervisor_deadline_seconds=supervisor_deadline_seconds,
                    budget=budget,
                    models=models,
                    github=github,
                )
            except LeaseLost:
                raise
            except Exception as exc:
                logger.error("approval resume failed: %s", type(exc).__name__)
                return failure_outcome(GENERIC_FAILURE)
            outcome = outcome_from_state(final)
            resumed_kind = "plan"
            if isinstance(resume_decision, dict) and resume_decision.get("kind") == "publish":
                resumed_kind = "publish"
            if outcome.status == TaskStatus.WAITING_APPROVAL:
                if outcome.stage == STAGE_PUBLISH_APPROVAL and resumed_kind != "publish":
                    return outcome
                logger.error("approval resume paused again task=%s", task_id)
                return failure_outcome(APPROVAL_FAILURE, stage=STAGE_APPROVAL)
            return outcome
        if isinstance(snapshot.values, dict) and snapshot.values:
            try:
                final = await invoke_triage(
                    graph,
                    None,
                    task_id=task_id,
                    provider=provider,
                    resume=True,
                    lease_lost=lease_lost,
                    repository_tools=repository_tools,
                    workspace_root=workspace_root,
                    runner_root=runner_root,
                    runner_wait_seconds=runner_wait_seconds,
                    max_tool_calls=max_tool_calls,
                    repository_error=repository_error,
                    supervisor_model=supervisor_model,
                    supervisor_deadline_seconds=supervisor_deadline_seconds,
                    budget=budget,
                    models=models,
                    github=github,
                )
            except LeaseLost:
                raise
            except Exception as exc:
                logger.error("triage resume failed: %s", type(exc).__name__)
                return failure_outcome(GENERIC_FAILURE)
            return outcome_from_state(final)
    return await run_triage(
        graph,
        task_id=task_id,
        title=title,
        description=description,
        provider=provider,
        lease_lost=lease_lost,
        repo_id=repo_id,
        repository_tools=repository_tools,
        workspace_root=workspace_root,
        runner_root=runner_root,
        runner_wait_seconds=runner_wait_seconds,
        max_tool_calls=max_tool_calls,
        repository_error=repository_error,
        supervisor_model=supervisor_model,
        supervisor_deadline_seconds=supervisor_deadline_seconds,
        budget=budget,
        models=models,
        github=github,
    )


async def invoke_triage(
    graph: CompiledGraph,
    state: TriageState | None,
    *,
    task_id: UUID,
    provider: LLMProvider,
    resume: bool = False,
    resume_decision: dict[str, Any] | None = None,
    lease_lost: Callable[[], bool] | None = None,
    repository_tools: RepositoryTools | None = None,
    workspace_root: Path | None = None,
    runner_root: Path | None = None,
    runner_wait_seconds: float = DEFAULT_RUNNER_WAIT_SECONDS,
    max_tool_calls: int = DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS,
    repository_error: str | None = None,
    supervisor_model: bool = False,
    supervisor_deadline_seconds: float = DEFAULT_SUPERVISOR_DEADLINE_SECONDS,
    budget: TaskBudget | None = None,
    models: StageModels | None = None,
    github: GitHubBinding | None = None,
) -> dict[str, Any]:
    """Run or resume one thread.

    `resume=True` passes no new input. `resume_decision` answers an `interrupt()`
    with `Command(resume=...)` on the same thread id.
    """
    options: dict[str, Any] = {}
    if graph.checkpointer:
        options["durability"] = CHECKPOINT_DURABILITY
    if resume_decision is not None:
        graph_input: Any = Command(resume=resume_decision)
    elif resume:
        graph_input = None
    else:
        graph_input = state
    result = await graph.ainvoke(
        graph_input,
        thread_config(task_id),
        context=WorkflowContext(
            provider=provider,
            lease_lost=lease_lost,
            repository_tools=repository_tools,
            workspace_root=workspace_root,
            runner_root=runner_root,
            runner_wait_seconds=runner_wait_seconds,
            max_tool_calls=max_tool_calls,
            repository_error=repository_error,
            supervisor_model=supervisor_model,
            supervisor_deadline_seconds=supervisor_deadline_seconds,
            budget=budget,
            models=models,
            github=github,
        ),
        **options,
    )
    if not isinstance(result, dict):
        raise TypeError(type(result).__name__)
    if _INTERRUPT in result:
        return result
    if graph.checkpointer:
        snapshot = await graph.aget_state(thread_config(task_id))
        if snapshot.interrupts:
            values = dict(snapshot.values) if isinstance(snapshot.values, dict) else {}
            return {**values, _INTERRUPT: snapshot.interrupts}
    return result


async def read_thread_state(graph: CompiledGraph, task_id: UUID) -> dict[str, Any]:
    """Load the latest checkpoint values for this task. Empty when none exist."""
    snapshot = await graph.aget_state(thread_config(task_id))
    values = snapshot.values
    if not isinstance(values, dict) or not values:
        return {}
    return dict(values)


def route_from_supervisor(state: TriageState) -> str:
    """Follow the route the supervisor just stored. An unknown route stops."""
    raw_route = state.get("route")
    if not isinstance(raw_route, str):
        return "finalize"
    try:
        chosen = SupervisorRoute(raw_route)
    except ValueError:
        return "finalize"
    return NODE_FOR_ROUTE[chosen]


def route_after_approval(state: TriageState) -> str:
    """Create a workspace only after an approval. Rejection stores no clone."""
    view = dict(state)
    view["phase"] = STAGE_APPROVAL
    return NODE_FOR_ROUTE[decide_route(view).route]


def supervisor_node(state: TriageState, runtime: Runtime[WorkflowContext]) -> dict[str, Any]:
    """Record why the next node was chosen. This node does not touch the checkout."""
    if _lease_is_lost(runtime):
        raise LeaseLost()
    now = time.time()
    try:
        deadline = require_supervisor_deadline(runtime.context.supervisor_deadline_seconds)
    except ValueError:
        deadline = DEFAULT_SUPERVISOR_DEADLINE_SECONDS
    decision = decide_route(
        state,
        now=now,
        deadline_seconds=deadline,
        publish_available=runtime.context.github is not None,
    )
    if runtime.context.supervisor_model:
        decision = with_model(
            decision,
            provider=_provider(runtime.context, STAGE_SUPERVISOR),
            state=state,
        )
        if _lease_is_lost(runtime):
            raise LeaseLost()
    return _supervisor_update(state, decision, now=now)


def _supervisor_update(
    state: TriageState,
    decision: SupervisorDecision,
    *,
    now: float,
) -> dict[str, Any]:
    steps = state.get("supervisor_steps")
    if isinstance(steps, bool) or not isinstance(steps, int) or steps < 0:
        steps = 0
    raw_routes = state.get("routes")
    routes = list(raw_routes) if isinstance(raw_routes, list) else []
    phase = state.get("phase")
    routes.append(
        {
            "route": decision.route.value,
            "reason": decision.reason,
            "phase": phase if isinstance(phase, str) and phase else "triage",
        }
    )
    update: dict[str, Any] = {
        "routes": routes,
        "route": decision.route.value,
        "supervisor_steps": steps + 1,
    }
    started = state.get("started_at")
    if isinstance(started, bool) or not isinstance(started, (int, float)):
        update["started_at"] = now
    if decision.route in {SupervisorRoute.INVESTIGATE, SupervisorRoute.INVESTIGATE_MORE}:
        update["investigation_count"] = _next_count(state.get("investigation_count"))
    if decision.route in {SupervisorRoute.PLAN, SupervisorRoute.REPLAN}:
        update["plan_count"] = _next_count(state.get("plan_count"))
    if decision.route is SupervisorRoute.ESCALATE and state.get("status") not in {
        "failed",
        "rejected",
    }:
        failed = phase if isinstance(phase, str) and phase in _FAILURE_STAGES else STAGE_TRIAGE
        update["status"] = "failed"
        update["error"] = decision.reason
        update["result_summary"] = decision.reason
        update["failed_stage"] = failed
        update["policy"] = PolicyAction.FAIL.value
    return update


def _next_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 1
    return value + 1


def request_plan_approval(state: TriageState) -> dict[str, Any]:
    """Pause until a person decides. Resume runs this function from the top.

    There is no database write, file write, or model call before `interrupt()`.
    Those would run again when the worker resumes the same thread.
    """
    try:
        prompt = _approval_prompt(state)
    except (ValidationError, ValueError):
        logger.error("approval rejected the stored plan")
        return _failed_update(PLAN_FAILURE, stage=STAGE_PLAN)
    parsed = _parse_resume(interrupt(prompt))
    if parsed is None:
        return _failed_update(APPROVAL_FAILURE, stage=STAGE_APPROVAL)
    return {
        "approval": parsed,
        "error": None,
        "failed_stage": None,
        "status": "rejected" if parsed["decision"] == "reject" else "running",
        "phase": STAGE_APPROVAL,
    }


def prepare_workspace_node(state: TriageState, runtime: Runtime[WorkflowContext]) -> dict[str, Any]:
    """Clone the opened checkout. The plan's paths are not used as destinations."""
    if _lease_is_lost(runtime):
        raise LeaseLost()
    context = runtime.context
    if (
        context.repository_error
        or context.repository_tools is None
        or context.workspace_root is None
    ):
        logger.error("workspace failed: unavailable")
        return _failed_update(WORKSPACE_FAILURE, stage=STAGE_WORKSPACE)
    try:
        approval = _checked_approval(state.get("approval"))
        task_id = UUID(str(state.get("task_id")))
    except (TypeError, ValueError):
        logger.error("workspace rejected the approval payload")
        return _failed_update(APPROVAL_FAILURE, stage=STAGE_APPROVAL)
    if approval is None or approval["decision"] != "approve":
        return _failed_update(APPROVAL_FAILURE, stage=STAGE_APPROVAL)
    try:
        created = prepare_isolated_workspace(
            source=context.repository_tools.root,
            workspace_root=context.workspace_root,
            task_id=task_id,
        )
    except WorkspaceError as exc:
        logger.error("workspace failed: %s", exc.code)
        return _failed_update(WORKSPACE_FAILURE, stage=STAGE_WORKSPACE)
    if _lease_is_lost(runtime):
        raise LeaseLost()
    return {
        "workspace": created.as_state(),
        "error": None,
        "failed_stage": None,
        "status": "completed",
        "phase": STAGE_WORKSPACE,
    }


def develop_node(state: TriageState, runtime: Runtime[WorkflowContext]) -> dict[str, Any]:
    """Propose a patch and apply it in the private worktree. The source stays read-only."""
    if _lease_is_lost(runtime):
        raise LeaseLost()
    context = runtime.context
    if context.repository_error or context.workspace_root is None:
        logger.error("patch rejected: unavailable")
        return _failed_update(PATCH_FAILURE, stage=STAGE_DEVELOP)
    try:
        plan_state = _checked_plan(state.get("plan"))
        investigation = _checked_investigation(state.get("investigation"))
        approval = _checked_approval(state.get("approval"))
        task_id = UUID(str(state.get("task_id")))
        workspace = workspace_from_state(state.get("workspace"), task_id=task_id)
        plan = None if plan_state is None else ChangePlan.model_validate(plan_state["plan"])
        report = None
        if investigation is not None:
            report = InvestigationReport.model_validate(investigation["report"])
    except (TypeError, ValidationError, ValueError):
        logger.error("patch rejected the stored plan")
        return _failed_update(PATCH_FAILURE, stage=STAGE_DEVELOP)
    if (
        plan is None
        or report is None
        or workspace is None
        or approval is None
        or approval["decision"] != "approve"
        or not plan.can_proceed
    ):
        return _failed_update(PATCH_FAILURE, stage=STAGE_DEVELOP)
    try:
        worktree = require_workspace_root(context.workspace_root) / worktree_relative(task_id)
    except Exception:
        logger.error("patch rejected: unavailable")
        return _failed_update(PATCH_FAILURE, stage=STAGE_DEVELOP)
    if worktree.is_symlink() or not worktree.is_dir():
        logger.error("patch rejected: unavailable")
        return _failed_update(PATCH_FAILURE, stage=STAGE_DEVELOP)
    try:
        try:
            repair_count = _repair_count(state)
            prior_review = _stored_review(state.get("review"))
            prior_tests = _checked_tests(state.get("tests")) if repair_count > 0 else None
        except ValueError:
            logger.error("patch rejected the review state")
            return _failed_update(PATCH_FAILURE, stage=STAGE_DEVELOP)
        if context.budget is not None:
            context.budget.allow_repair(repair_count)
        outcome = DeveloperAgent(
            _provider(context, STAGE_DEVELOP),
            worktree,
            lease_lost=context.lease_lost,
        ).run(
            title=state["title"],
            description=state["description"],
            plan=plan,
            report=report,
            review=prior_review,
            repair_count=repair_count,
            tests=prior_tests,
        )
    except StopForLease:
        raise LeaseLost() from None
    except PatchError as exc:
        logger.error("patch rejected: %s", exc.code)
        if exc.code == "empty":
            message = EMPTY_PATCH
        elif exc.code == "syntax":
            message = SYNTAX_PATCH
        elif exc.code == "mismatch":
            message = MISMATCH_PATCH
        else:
            message = PATCH_FAILURE
        return _failed_update(message, stage=STAGE_DEVELOP)
    except LLMError as exc:
        return _failed_update(failure_text(exc), stage=STAGE_DEVELOP)
    except Exception as exc:
        logger.error("develop node failed: %s", type(exc).__name__)
        return _failed_update(PATCH_FAILURE, stage=STAGE_DEVELOP)
    if _lease_is_lost(runtime):
        raise LeaseLost()
    return {
        "patch": outcome.as_state(),
        "error": None,
        "failed_stage": None,
        "status": "completed",
        "phase": STAGE_DEVELOP,
    }


def test_node(state: TriageState, runtime: Runtime[WorkflowContext]) -> dict[str, Any]:
    """Wait for the runner. This process does not start pytest."""
    if _lease_is_lost(runtime):
        raise LeaseLost()
    context = runtime.context
    if context.runner_root is None or context.workspace_root is None:
        logger.error("tests rejected: unavailable")
        return _failed_update(RUNNER_UNAVAILABLE, stage=STAGE_TEST)
    try:
        task_id = UUID(str(state.get("task_id")))
        workspace = workspace_from_state(state.get("workspace"), task_id=task_id)
        repair_count = _repair_count(state)
    except (TypeError, ValueError, RunnerRejected):
        logger.error("tests rejected the workspace")
        return _failed_update(RUNNER_REJECTED, stage=STAGE_TEST)
    try:
        patch_ready = _checked_patch(state.get("patch")) is not None
    except ValueError:
        return _failed_update(TEST_FAILURE, stage=STAGE_TEST)
    if workspace is None:
        return _failed_update(TEST_FAILURE, stage=STAGE_TEST)
    try:
        if patch_ready:
            job_id = test_job_id(task_id, repair_count)
        else:
            job_id = baseline_job_id(task_id)
    except (TypeError, ValueError, RunnerRejected):
        logger.error("tests rejected the workspace")
        return _failed_update(RUNNER_REJECTED, stage=STAGE_TEST)
    try:
        worktree = require_workspace_root(context.workspace_root) / worktree_relative(task_id)
    except Exception:
        logger.error("tests rejected: unavailable")
        return _failed_update(RUNNER_REJECTED, stage=STAGE_TEST)
    if worktree.is_symlink() or not worktree.is_dir():
        logger.error("tests rejected: unavailable")
        return _failed_update(RUNNER_REJECTED, stage=STAGE_TEST)
    try:
        report = collect_test_report(
            worktree=worktree,
            runner_root=context.runner_root,
            job_id=job_id,
            wait_seconds=context.runner_wait_seconds,
            lease_lost=context.lease_lost,
        )
    except StopForLease:
        raise LeaseLost() from None
    except RunnerUnavailable:
        logger.error("tests rejected: unavailable")
        return _failed_update(RUNNER_UNAVAILABLE, stage=STAGE_TEST)
    except RunnerRejected:
        logger.error("tests rejected: refused")
        return _failed_update(RUNNER_REJECTED, stage=STAGE_TEST)
    except Exception as exc:
        logger.error("test node failed: %s", type(exc).__name__)
        return _failed_update(TEST_FAILURE, stage=STAGE_TEST)
    if report.job_id != job_id or report.status == TestStatus.INVALID_REQUEST:
        return _failed_update(RUNNER_REJECTED, stage=STAGE_TEST)
    if _lease_is_lost(runtime):
        raise LeaseLost()
    stored = report.model_dump(mode="json")
    if not patch_ready:
        return {
            "baseline_tests": stored,
            "error": None,
            "failed_stage": None,
            "status": "completed",
            "phase": STAGE_BASELINE,
        }
    return {
        "tests": stored,
        "error": None,
        "failed_stage": None,
        "status": "completed",
        "phase": STAGE_TEST,
    }


def review_node(state: TriageState, runtime: Runtime[WorkflowContext]) -> dict[str, Any]:
    """Ask for a verdict. Policy, not the model, chooses accept, repair, or failure."""
    if _lease_is_lost(runtime):
        raise LeaseLost()
    context = runtime.context
    try:
        budget = context.max_repairs if context.budget is None else context.budget.max_repairs
        if isinstance(budget, bool) or not isinstance(budget, int) or not 0 <= budget <= 2:
            raise ValueError("repair budget")
        prepared = _review_inputs(state)
    except (TypeError, ValidationError, ValueError):
        logger.error("review rejected the stored patch")
        return _failed_update(REVIEW_FAILURE, stage=STAGE_REVIEW)
    if prepared is None:
        return _failed_update(REVIEW_FAILURE, stage=STAGE_REVIEW)
    plan, investigation, patch, tests, repair_count = prepared
    if not applied_diff_is_allowed(patch["diff"], plan):
        logger.error("review rejected the diff")
        return _failed_update(
            "The patch violates the edit policy. A person needs to intervene.",
            stage=STAGE_REVIEW,
        )
    if len(_cycle_list(state)) > budget:
        return _failed_update(
            "Repair stopped after 2 attempts. A person needs to intervene.",
            stage=STAGE_REVIEW,
        )
    try:
        baseline_line, no_new_failures, baseline_output = _baseline_context(state, tests)
    except ValueError:
        logger.error("review rejected the baseline report")
        return _failed_update(REVIEW_FAILURE, stage=STAGE_REVIEW)
    try:
        verdict = ReviewerAgent(
            _provider(context, STAGE_REVIEW),
            lease_lost=context.lease_lost,
        ).run(
            title=state["title"],
            description=state["description"],
            plan=plan,
            report=investigation,
            diff=patch["diff"],
            tests=tests,
            repair_count=repair_count,
            baseline_failures=baseline_line,
        )
    except StopForLease:
        raise LeaseLost() from None
    except LLMError as exc:
        return _failed_update(failure_text(exc), stage=STAGE_REVIEW)
    except Exception as exc:
        logger.error("review node failed: %s", type(exc).__name__)
        return _failed_update(REVIEW_FAILURE, stage=STAGE_REVIEW)
    if _lease_is_lost(runtime):
        raise LeaseLost()
    try:
        decision = decide_review(
            review=verdict,
            tests_passed=tests.status is TestStatus.PASSED,
            patch_allowed=True,
            repair_count=repair_count,
            max_repairs=budget,
            no_new_failures=no_new_failures,
            baseline_output=baseline_output,
            diff=patch["diff"],
        )
    except ValueError:
        logger.error("review policy rejected the verdict")
        return _failed_update(REVIEW_FAILURE, stage=STAGE_REVIEW)
    return _review_stop(
        state,
        review=verdict,
        decision_action=decision.action,
        reason=decision.reason,
    )


def triage_node(state: TriageState, runtime: Runtime[WorkflowContext]) -> dict[str, Any]:
    prompt = triage_prompt(title=state["title"], description=state["description"])
    try:
        result = _provider(runtime.context, STAGE_TRIAGE).generate_structured(prompt, TriageResult)
    except LLMError as exc:
        return _failed_update(failure_text(exc))
    except Exception as exc:
        logger.error("triage node failed: %s", type(exc).__name__)
        return _failed_update(GENERIC_FAILURE)
    if _lease_is_lost(runtime):
        raise LeaseLost()
    return {
        "triage": result.model_dump(mode="json"),
        "error": None,
        "status": "completed",
        "phase": STAGE_TRIAGE,
    }


def investigate_node(state: TriageState, runtime: Runtime[WorkflowContext]) -> dict[str, Any]:
    """Read the registered checkout and keep only citations that were actually read."""
    context = runtime.context
    if context.repository_error or context.repository_tools is None:
        return _failed_update(
            context.repository_error or REPOSITORY_UNAVAILABLE,
            stage=STAGE_INVESTIGATE,
        )
    try:
        triage = TriageResult.model_validate(state.get("triage"))
    except ValidationError:
        logger.error("investigate rejected the triage payload")
        return _failed_update(GENERIC_FAILURE, stage=STAGE_TRIAGE)
    try:
        outcome = InvestigatorAgent(
            _provider(context, STAGE_INVESTIGATE),
            context.repository_tools,
            max_tool_calls=context.max_tool_calls,
            lease_lost=context.lease_lost,
            charge_tool=None if context.budget is None else context.budget.charge_tool,
        ).run(title=state["title"], description=state["description"], triage=triage)
    except StopForLease:
        raise LeaseLost() from None
    except LLMError as exc:
        return _failed_update(failure_text(exc), stage=STAGE_INVESTIGATE)
    except Exception as exc:
        logger.error("investigate node failed: %s", type(exc).__name__)
        return _failed_update(INVESTIGATION_FAILURE, stage=STAGE_INVESTIGATE)
    if _lease_is_lost(runtime):
        raise LeaseLost()
    return {
        "investigation": outcome.as_state(),
        "error": None,
        "failed_stage": None,
        "status": "completed",
        "phase": STAGE_INVESTIGATE,
    }


def plan_node(state: TriageState, runtime: Runtime[WorkflowContext]) -> dict[str, Any]:
    """Propose a change from the investigation. This node does not write files."""
    context = runtime.context
    if context.repository_error or context.repository_tools is None:
        return _failed_update(
            context.repository_error or REPOSITORY_UNAVAILABLE,
            stage=STAGE_PLAN,
        )
    raw = state.get("investigation")
    if not isinstance(raw, dict):
        logger.error("plan rejected the investigation payload")
        return _failed_update(INVESTIGATION_FAILURE, stage=STAGE_INVESTIGATE)
    try:
        triage = TriageResult.model_validate(state.get("triage"))
        report = InvestigationReport.model_validate(raw.get("report"))
    except ValidationError:
        logger.error("plan rejected the stored payload")
        return _failed_update(INVESTIGATION_FAILURE, stage=STAGE_INVESTIGATE)
    try:
        outcome = PlannerAgent(
            _provider(context, STAGE_PLAN),
            context.repository_tools.root,
            lease_lost=context.lease_lost,
        ).run(
            title=state["title"],
            description=state["description"],
            triage=triage,
            report=report,
        )
    except StopForLease:
        raise LeaseLost() from None
    except LLMError as exc:
        return _failed_update(failure_text(exc), stage=STAGE_PLAN)
    except Exception as exc:
        logger.error("plan node failed: %s", type(exc).__name__)
        return _failed_update(PLAN_FAILURE, stage=STAGE_PLAN)
    if _lease_is_lost(runtime):
        raise LeaseLost()
    return {
        "plan": outcome.as_state(),
        "error": None,
        "failed_stage": None,
        "status": "completed",
        "phase": STAGE_PLAN,
    }


def request_publish_approval(
    state: TriageState,
    runtime: Runtime[WorkflowContext],
) -> dict[str, Any]:
    """Pause before any push. Resume runs this function from the top.

    There is no Git command and no HTTP request before `interrupt()`.
    """
    if runtime.context.github is None:
        logger.error("publication rejected: not configured")
        return _failed_update(PUBLISH_FAILURE, stage=STAGE_PUBLISH)
    try:
        prompt = _publish_prompt(state, runtime.context.github)
    except (ValidationError, ValueError):
        logger.error("publication rejected the stored review")
        return _failed_update(PUBLISH_FAILURE, stage=STAGE_PUBLISH)
    parsed = _parse_resume(interrupt(prompt), kind="publish")
    if parsed is None:
        return _failed_update(PUBLISH_FAILURE, stage=STAGE_PUBLISH)
    recorded = {
        **parsed,
        "owner": runtime.context.github.repo.owner,
        "name": runtime.context.github.repo.name,
    }
    if parsed["decision"] == "reject":
        return {
            "publication": recorded,
            "error": None,
            "failed_stage": None,
            "status": "completed",
            "phase": STAGE_PUBLISH_APPROVAL,
        }
    return {
        "publication": recorded,
        "error": None,
        "failed_stage": None,
        "status": "running",
        "phase": STAGE_PUBLISH_APPROVAL,
    }


def publish_node(state: TriageState, runtime: Runtime[WorkflowContext]) -> dict[str, Any]:
    """Push the task branch and open one draft. The token stays in the context."""
    if _lease_is_lost(runtime):
        raise LeaseLost()
    binding = runtime.context.github
    try:
        decision = _checked_publication(state.get("publication"))
        task_id = UUID(str(state.get("task_id")))
        workspace = workspace_from_state(state.get("workspace"), task_id=task_id)
    except (TypeError, ValueError):
        logger.error("publication rejected the stored decision")
        return _failed_update(PUBLISH_FAILURE, stage=STAGE_PUBLISH)
    if (
        binding is None
        or workspace is None
        or decision is None
        or decision["decision"] != "approve"
        or runtime.context.workspace_root is None
    ):
        return _failed_update(PUBLISH_FAILURE, stage=STAGE_PUBLISH)
    if binding.repo.owner != decision["owner"] or binding.repo.name != decision["name"]:
        logger.error("publication rejected: repository mismatch")
        return _failed_update(PUBLISH_FAILURE, stage=STAGE_PUBLISH)
    try:
        worktree = require_workspace_root(runtime.context.workspace_root) / worktree_relative(
            task_id
        )
    except Exception:
        logger.error("publication rejected: unavailable")
        return _failed_update(PUBLISH_FAILURE, stage=STAGE_PUBLISH)
    if worktree.is_symlink() or not worktree.is_dir():
        return _failed_update(PUBLISH_FAILURE, stage=STAGE_PUBLISH)
    try:
        opened = publish_draft(
            task_id=task_id,
            worktree=worktree,
            repo=binding.repo,
            token=binding.token,
            issue_title=str(state.get("title") or ""),
            description=str(state.get("description") or ""),
            reasoning=_plan_reasoning(state),
            tests_passed=_tests_passed(state),
            tests_summary=_tests_summary(state),
            transport=binding.transport,
            local_remote=binding.local_remote,
        )
    except GitHubError as exc:
        logger.error("publication failed: %s", exc.code)
        return _failed_update(PUBLISH_FAILURE, stage=STAGE_PUBLISH)
    except Exception:
        logger.error("publication failed: rejected")
        return _failed_update(PUBLISH_FAILURE, stage=STAGE_PUBLISH)
    if _lease_is_lost(runtime):
        raise LeaseLost()
    return {
        "publication": {
            "decision": "approve",
            "kind": "publish",
            "approval_id": decision["approval_id"],
            "owner": opened.owner,
            "name": opened.name,
            "branch": opened.branch,
            "base": opened.base,
            "number": opened.number,
            "url": opened.url,
            "draft": True,
            "head_sha": opened.head_sha,
        },
        "error": None,
        "failed_stage": None,
        "status": "completed",
        "phase": STAGE_PUBLISH,
    }


def finalize_node(state: TriageState) -> dict[str, Any]:
    """Re-validate before the summary becomes user-facing state."""
    if state.get("status") == "rejected":
        try:
            return _rejected_update(state)
        except (ValidationError, ValueError):
            logger.error("finalize rejected the approval payload")
            return _failed_update(GENERIC_FAILURE)
    if state.get("status") == "failed" or state.get("error"):
        try:
            reviewed = bool(_cycle_list(state))
        except ValueError:
            return _failed_update(REVIEW_FAILURE, stage=STAGE_REVIEW)
        if reviewed:
            reason = _message(state.get("error") or state.get("result_summary"))
            stage = state.get("failed_stage")
            if stage == STAGE_PUBLISH:
                return {
                    "status": "failed",
                    "error": reason,
                    "result_summary": reason,
                    "failed_stage": STAGE_PUBLISH,
                    "phase": STAGE_PUBLISH,
                }
            if stage in {STAGE_DEVELOP, STAGE_TEST}:
                return {
                    "status": "failed",
                    "error": reason,
                    "result_summary": reason,
                    "failed_stage": stage,
                    "phase": stage,
                    "policy": PolicyAction.FAIL.value,
                }
            return {
                "status": "failed",
                "error": reason,
                "result_summary": reason,
                "failed_stage": STAGE_REVIEW,
                "policy": PolicyAction.FAIL.value,
            }
        message = _message(state.get("error"))
        stage = state.get("failed_stage") or STAGE_TRIAGE
        return _failed_update(message, stage=stage if isinstance(stage, str) else STAGE_TRIAGE)
    try:
        checked = TriageResult.model_validate(state.get("triage"))
        investigation = _checked_investigation(state.get("investigation"))
        plan = _checked_plan(state.get("plan"))
        approval = _checked_approval(state.get("approval"))
    except (ValidationError, ValueError):
        logger.error("finalize rejected the stored payload")
        return _failed_update(GENERIC_FAILURE)
    if investigation is not None and plan is None:
        return _failed_update(PLAN_FAILURE, stage=STAGE_PLAN)
    if plan is not None and investigation is None:
        return _failed_update(GENERIC_FAILURE)
    if _plan_requests_changes(plan) and (approval is None or approval["decision"] != "approve"):
        return _failed_update(APPROVAL_FAILURE, stage=STAGE_APPROVAL)
    try:
        workspace = _accepted_workspace(state, plan)
    except ValueError:
        logger.error("finalize rejected the workspace payload")
        return _failed_update(WORKSPACE_FAILURE, stage=STAGE_WORKSPACE)
    try:
        patch = _accepted_patch(state, plan, workspace)
    except ValueError:
        logger.error("finalize rejected the patch payload")
        return _failed_update(PATCH_FAILURE, stage=STAGE_DEVELOP)
    try:
        _accepted_tests(state, patch)
    except ValueError:
        logger.error("finalize rejected the test report")
        return _failed_update(TEST_FAILURE, stage=STAGE_TEST)
    if patch is not None:
        try:
            judged, _repairs, _cycles = _judge_cycles(state)
        except (ValidationError, ValueError):
            logger.error("finalize rejected the review")
            return _failed_update(REVIEW_FAILURE, stage=STAGE_REVIEW)
        if judged.action is not PolicyAction.ACCEPT:
            reason = _message(judged.reason)
            return {
                "status": "failed",
                "error": reason,
                "result_summary": reason,
                "failed_stage": STAGE_REVIEW,
                "policy": PolicyAction.FAIL.value,
            }
    summary = checked.summary
    if plan is not None:
        summary = str(plan["plan"]["problem_statement"])
    if patch is not None:
        summary = _completed_summary(
            str(patch["summary"]),
            _unchanged_failure_identities(state),
        )
    return {
        "status": "completed",
        "error": None,
        "failed_stage": None,
        "result_summary": summary,
        "triage": checked.model_dump(mode="json"),
        "investigation": investigation,
        "plan": plan,
        "approval": approval,
        "workspace": None if workspace is None else workspace.as_state(),
    }


def failure_outcome(message: str, *, stage: str = STAGE_TRIAGE) -> TriageOutcome:
    text = _message(message)
    return TriageOutcome(
        status=TaskStatus.FAILED,
        stage=stage if stage in _FAILURE_STAGES else STAGE_TRIAGE,
        result_summary=text,
        result_json={"error": text},
    )


def outcome_from_state(state: GraphState) -> TriageOutcome:
    """Build the task record and keep the supervisor trace off `result_json`."""
    return replace(_outcome_from_state(state), routes=route_trace(state))


def _outcome_from_state(state: GraphState) -> TriageOutcome:
    if _INTERRUPT in state:
        return _pause_or_fail(state)
    if state.get("status") == "rejected":
        try:
            return _rejected_outcome(state)
        except (ValidationError, ValueError):
            return failure_outcome(GENERIC_FAILURE)
    publish_failed = state.get("failed_stage") == STAGE_PUBLISH or (
        state.get("phase") == STAGE_PUBLISH
        and (state.get("status") == "failed" or state.get("error"))
    )
    if publish_failed:
        return failure_outcome(
            _message(state.get("error") or state.get("result_summary")),
            stage=STAGE_PUBLISH,
        )
    try:
        reviewed = bool(_cycle_list(state))
    except ValueError:
        return failure_outcome(REVIEW_FAILURE, stage=STAGE_REVIEW)
    if reviewed:
        try:
            return _attach_publication(_reviewed_outcome(state), state)
        except (ValidationError, ValueError):
            logger.error("outcome rejected the review")
            return failure_outcome(REVIEW_FAILURE, stage=STAGE_REVIEW)
    if state.get("status") != "completed" or state.get("error"):
        stage = state.get("failed_stage")
        return failure_outcome(
            _message(state.get("error") or state.get("result_summary")),
            stage=stage if isinstance(stage, str) else STAGE_TRIAGE,
        )
    try:
        checked = TriageResult.model_validate(state.get("triage"))
        investigation = _checked_investigation(state.get("investigation"))
        plan = _checked_plan(state.get("plan"))
        approval = _checked_approval(state.get("approval"))
    except (ValidationError, ValueError):
        return failure_outcome(GENERIC_FAILURE)
    if investigation is not None and plan is None:
        return failure_outcome(PLAN_FAILURE, stage=STAGE_PLAN)
    if plan is not None and investigation is None:
        return failure_outcome(GENERIC_FAILURE)
    if _plan_requests_changes(plan) and (approval is None or approval["decision"] != "approve"):
        return failure_outcome(APPROVAL_FAILURE, stage=STAGE_APPROVAL)
    try:
        workspace = _accepted_workspace(state, plan)
    except ValueError:
        return failure_outcome(WORKSPACE_FAILURE, stage=STAGE_WORKSPACE)
    try:
        patch = _accepted_patch(state, plan, workspace)
    except ValueError:
        return failure_outcome(PATCH_FAILURE, stage=STAGE_DEVELOP)
    try:
        tests = _accepted_tests(state, patch)
    except ValueError:
        return failure_outcome(TEST_FAILURE, stage=STAGE_TEST)
    if plan is None or investigation is None:
        return TriageOutcome(
            status=TaskStatus.COMPLETED,
            stage=STAGE_FINALIZE,
            result_summary=checked.summary,
            result_json=checked.model_dump(mode="json"),
        )
    body, accepted = _planned_body(checked, investigation, plan, approval, workspace)
    stored = _store_patch(body, patch)
    stored_tests = _store_tests(body, tests)
    summary = accepted.problem_statement
    if patch is not None:
        summary = patch["summary"]
    return TriageOutcome(
        status=TaskStatus.COMPLETED,
        stage=STAGE_FINALIZE,
        result_summary=summary,
        result_json=body,
        plan=accepted,
        workspace=workspace,
        patch=stored,
        tests=stored_tests,
    )


def failure_text(exc: LLMError) -> str:
    return _message(str(exc))


def _pause_or_fail(state: GraphState) -> TriageOutcome:
    try:
        if _interrupt_kind(state) == "publish":
            outcome = _publish_pause_outcome(state)
        else:
            outcome = _paused_outcome(state)
    except (ValidationError, ValueError):
        logger.error("approval pause rejected the stored plan")
        stage = STAGE_PUBLISH if _interrupt_kind(state) == "publish" else STAGE_PLAN
        message = PUBLISH_FAILURE if stage == STAGE_PUBLISH else PLAN_FAILURE
        outcome = failure_outcome(message, stage=stage)
    return replace(outcome, routes=route_trace(state))


def _publish_pause_outcome(state: GraphState) -> TriageOutcome:
    """The review is stored, and the publication interrupt is still unanswered."""
    outcome = _reviewed_outcome(state)
    if outcome.status != TaskStatus.COMPLETED:
        raise ValueError("publish pause without an accepted review")
    return replace(
        outcome,
        status=TaskStatus.WAITING_APPROVAL,
        stage=STAGE_PUBLISH_APPROVAL,
    )


def _paused_outcome(state: GraphState) -> TriageOutcome:
    checked = TriageResult.model_validate(state.get("triage"))
    investigation = _checked_investigation(state.get("investigation"))
    plan = _checked_plan(state.get("plan"))
    if investigation is None or plan is None or not _plan_requests_changes(plan):
        raise ValueError("pause without a code change")
    body, accepted = _planned_body(checked, investigation, plan, None)
    return TriageOutcome(
        status=TaskStatus.WAITING_APPROVAL,
        stage=STAGE_APPROVAL,
        result_summary=accepted.problem_statement,
        result_json=body,
        plan=accepted,
    )


def _rejected_outcome(state: GraphState) -> TriageOutcome:
    checked = TriageResult.model_validate(state.get("triage"))
    investigation = _checked_investigation(state.get("investigation"))
    plan = _checked_plan(state.get("plan"))
    approval = _checked_approval(state.get("approval"))
    if (
        investigation is None
        or plan is None
        or approval is None
        or approval["decision"] != "reject"
    ):
        raise ValueError("rejection is missing its plan")
    body, accepted = _planned_body(checked, investigation, plan, approval)
    return TriageOutcome(
        status=TaskStatus.REJECTED,
        stage=STAGE_FINALIZE,
        result_summary=_rejection_summary(approval.get("reason")),
        result_json=body,
        plan=accepted,
    )


def _rejected_update(state: TriageState) -> dict[str, Any]:
    outcome = _rejected_outcome(state)
    return {
        "status": "rejected",
        "error": None,
        "failed_stage": None,
        "result_summary": outcome.result_summary,
        "triage": outcome.result_json["triage"],
        "investigation": outcome.result_json["investigation"],
        "plan": _checked_plan(state.get("plan")),
        "approval": _checked_approval(state.get("approval")),
        "workspace": None,
    }


def _reviewed_outcome(state: GraphState) -> TriageOutcome:
    """Build the task record from the review cycles. Policy is applied again here."""
    checked = TriageResult.model_validate(state.get("triage"))
    investigation = _checked_investigation(state.get("investigation"))
    plan = _checked_plan(state.get("plan"))
    approval = _checked_approval(state.get("approval"))
    if investigation is None or plan is None:
        raise ValueError("review is missing its plan")
    if _plan_requests_changes(plan) and (approval is None or approval["decision"] != "approve"):
        raise ValueError("review without approval")
    workspace = _accepted_workspace(state, plan)
    decision, repairs, cycles = _judge_cycles(state)
    body, accepted = _planned_body(checked, investigation, plan, approval, workspace)
    last = cycles[-1]
    stored = _store_patch(
        body,
        {
            "summary": last.applied.summary,
            "paths": list(last.applied.paths),
            "diff": last.applied.diff.decode("utf-8"),
            "proposal": last.proposal,
        },
    )
    stored_tests = _store_tests(body, last.tests)
    _store_review(body, last, repair_count=len(repairs))
    body["repairs"] = [{"index": item["index"], "reason": item["reason"]} for item in repairs]
    identities = _unchanged_failure_identities(state, last.tests)
    accepted_finish = (
        decision.action is PolicyAction.ACCEPT
        and state.get("status") == "completed"
        and not state.get("error")
    )
    if accepted_finish:
        if identities:
            body["baseline_failures"] = list(identities)
        return TriageOutcome(
            status=TaskStatus.COMPLETED,
            stage=STAGE_FINALIZE,
            result_summary=_completed_summary(last.applied.summary, identities),
            result_json=body,
            plan=accepted,
            workspace=workspace,
            patch=stored,
            tests=stored_tests,
            review=last.review,
            policy=PolicyAction.ACCEPT.value,
            repair_count=len(repairs),
            repairs=repairs,
            cycles=cycles,
        )
    if decision.action is PolicyAction.REPAIR:
        stage = state.get("failed_stage")
        if stage not in {STAGE_DEVELOP, STAGE_TEST}:
            raise ValueError("review stopped while a repair was still allowed")
        reason = _message(state.get("error") or state.get("result_summary"))
    else:
        stage = STAGE_REVIEW
        reason = decision.reason if decision.action is PolicyAction.FAIL else REVIEW_FAILURE
        reason = _message(reason)
    body["error"] = reason
    return TriageOutcome(
        status=TaskStatus.FAILED,
        stage=stage,
        result_summary=reason,
        result_json=body,
        plan=accepted,
        patch=stored,
        tests=stored_tests,
        review=last.review,
        policy=PolicyAction.FAIL.value,
        repair_count=len(repairs),
        repairs=repairs,
        cycles=cycles,
    )


def _judge_cycles(
    state: GraphState,
) -> tuple[Any, tuple[dict[str, Any], ...], tuple[ReviewedCycle, ...]]:
    plan_state = _checked_plan(state.get("plan"))
    if plan_state is None:
        raise ValueError("review is missing its plan")
    plan = ChangePlan.model_validate(plan_state["plan"])
    raw_cycles = _cycle_list(state)
    if not raw_cycles or len(raw_cycles) > DEFAULT_MAX_REPAIRS + 1:
        raise ValueError("review cycles are missing")
    task_raw = state.get("task_id")
    if not isinstance(task_raw, str):
        raise ValueError("task id missing")
    task_id = UUID(task_raw)
    built: list[ReviewedCycle] = []
    repairs: list[dict[str, Any]] = []
    final = None
    for index, item in enumerate(raw_cycles):
        proposal, applied, tests, review = _cycle_parts(item, plan, task_id, index)
        diff_text = applied.diff.decode("utf-8")
        _line, no_new_failures, baseline_output = _baseline_context(state, tests)
        decision = decide_review(
            review=review,
            tests_passed=tests.status is TestStatus.PASSED,
            patch_allowed=applied_diff_is_allowed(diff_text, plan),
            repair_count=index,
            no_new_failures=no_new_failures,
            baseline_output=baseline_output,
            diff=diff_text,
        )
        cycle = ReviewedCycle(
            proposal=proposal,
            applied=applied,
            tests=tests,
            review=review,
            policy=decision.action.value,
            reason=decision.reason,
            repair_index=index,
        )
        built.append(cycle)
        if index < len(raw_cycles) - 1:
            if decision.action is not PolicyAction.REPAIR:
                raise ValueError("a repair cycle did not request a repair")
            repairs.append({"index": index + 1, "reason": decision.reason})
        else:
            final = decision
    if final is None:
        raise ValueError("review stopped while a repair was still allowed")
    if final.action is PolicyAction.REPAIR:
        if not _interrupted_repair(state):
            raise ValueError("review stopped while a repair was still allowed")
        repairs.append({"index": len(built), "reason": final.reason})
    current = _checked_patch(state.get("patch"))
    if current is None or current["diff"] != built[-1].applied.diff.decode("utf-8"):
        raise ValueError("the stored patch does not match the review")
    return final, tuple(repairs), tuple(built)


def _cycle_parts(
    item: object,
    plan: ChangePlan,
    task_id: UUID,
    index: int,
) -> tuple[Any, AppliedPatch, TestReport, ReviewResult]:
    if not isinstance(item, dict):
        raise ValueError("review cycle is not an object")
    proposal = PatchProposal.model_validate(item.get("proposal"))
    diff = item.get("diff")
    paths = item.get("paths")
    if not isinstance(diff, str) or not diff.startswith("diff --git ") or "\x00" in diff:
        raise ValueError("review cycle diff is missing")
    encoded = diff.encode()
    if len(encoded) > MAX_DIFF_BYTES:
        raise ValueError("review cycle diff is too large")
    if (
        not isinstance(paths, list)
        or not paths
        or not all(isinstance(path, str) and path for path in paths)
    ):
        raise ValueError("review cycle paths are missing")
    planned = {modification.relative_path for modification in plan.modifications}
    if any(path not in planned for path in paths):
        raise ValueError("review cycle path is outside the plan")
    if item.get("summary") != proposal.summary:
        raise ValueError("review cycle summary does not match the proposal")
    tests = TestReport.model_validate(item.get("tests"))
    try:
        expected = test_job_id(task_id, index)
    except RunnerRejected:
        raise ValueError("review cycle job is not valid") from None
    if tests.job_id != expected or tests.status is TestStatus.INVALID_REQUEST:
        raise ValueError("review cycle job does not match the task")
    review = ReviewResult.model_validate(item.get("review"))
    applied = AppliedPatch(summary=proposal.summary, paths=tuple(paths), diff=encoded)
    return proposal, applied, tests, review


def _review_stop(
    state: TriageState,
    *,
    review: ReviewResult,
    decision_action: PolicyAction,
    reason: str,
) -> dict[str, Any]:
    patch = _checked_patch(state.get("patch"))
    tests = _checked_tests(state.get("tests"))
    repair_count = _repair_count(state)
    if patch is None or tests is None:
        return _failed_update(REVIEW_FAILURE, stage=STAGE_REVIEW)
    cycle = {
        "summary": patch["summary"],
        "paths": list(patch["paths"]),
        "diff": patch["diff"],
        "proposal": patch["proposal"].model_dump(mode="json"),
        "tests": tests.model_dump(mode="json"),
        "review": review.model_dump(mode="json"),
        "policy": decision_action.value,
        "reason": reason,
        "repair_index": repair_count,
    }
    update: dict[str, Any] = {
        "review": review.model_dump(mode="json"),
        "policy": decision_action.value,
        "cycles": [*_cycle_list(state), cycle],
        "repair_count": repair_count,
        "phase": STAGE_REVIEW,
    }
    if decision_action is PolicyAction.REPAIR:
        update["status"] = "running"
        update["repair_count"] = repair_count + 1
        update["error"] = None
        update["failed_stage"] = None
        return update
    if decision_action is PolicyAction.ACCEPT:
        update["status"] = "completed"
        update["error"] = None
        update["failed_stage"] = None
        return update
    update["status"] = "failed"
    update["error"] = reason
    update["result_summary"] = reason
    update["failed_stage"] = STAGE_REVIEW
    return update


def _review_inputs(
    state: TriageState,
) -> tuple[ChangePlan, InvestigationReport, dict[str, Any], TestReport, int] | None:
    plan_state = _checked_plan(state.get("plan"))
    investigation = _checked_investigation(state.get("investigation"))
    if plan_state is None or investigation is None:
        return None
    plan = ChangePlan.model_validate(plan_state["plan"])
    report = InvestigationReport.model_validate(investigation["report"])
    patch = _checked_patch(state.get("patch"))
    tests = _checked_tests(state.get("tests"))
    if patch is None or tests is None or not plan.can_proceed:
        return None
    return plan, report, patch, tests, _repair_count(state)


def _store_review(body: dict[str, Any], cycle: ReviewedCycle, *, repair_count: int) -> None:
    """Record the verdict checksum. The review body stays in the artifact file."""
    payload = review_document(cycle.review, policy=cycle.policy, repair_count=repair_count)
    body["review"] = {
        "action": cycle.review.action.value,
        "policy": cycle.policy,
        "repair_count": repair_count,
        "critical": any(
            item.severity is FindingSeverity.CRITICAL for item in cycle.review.findings
        ),
        "checksum": checksum(payload),
    }


def _cycle_list(state: GraphState) -> list[dict[str, Any]]:
    raw = state.get("cycles")
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ValueError("review cycles are not a list")
    return list(raw)


def _repair_count(state: GraphState) -> int:
    value = state.get("repair_count")
    if value is None:
        return 0
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= DEFAULT_MAX_REPAIRS
    ):
        raise ValueError("repair count is not valid")
    return value


def _stored_review(value: object) -> ReviewResult | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("review payload is not an object")
    return ReviewResult.model_validate(value)


def _accepted_patch(
    state: GraphState,
    plan: dict[str, Any] | None,
    workspace: IsolatedWorkspace | None,
) -> dict[str, Any] | None:
    """Return the applied patch, or None when this task did not develop."""
    checked = _checked_patch(state.get("patch"))
    if workspace is None:
        if checked is not None:
            raise ValueError("patch without a workspace")
        return None
    if checked is None or plan is None:
        raise ValueError("approved plan has no patch")
    accepted = ChangePlan.model_validate(plan["plan"])
    planned = {item.relative_path for item in accepted.modifications}
    paths = checked["paths"]
    if not paths or any(path not in planned for path in paths):
        raise ValueError("patch path is outside the plan")
    return checked


def _checked_patch(value: object) -> dict[str, Any] | None:
    """Re-validate a stored patch. None means this task did not develop."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("patch payload is not an object")
    try:
        proposal = PatchProposal.model_validate(value.get("proposal"))
    except ValidationError:
        raise ValueError("patch proposal is not valid") from None
    diff = value.get("diff")
    paths = value.get("paths")
    if not isinstance(diff, str) or not diff.startswith("diff --git ") or "\x00" in diff:
        raise ValueError("patch diff is missing")
    if len(diff.encode()) > MAX_DIFF_BYTES:
        raise ValueError("patch diff is too large")
    if (
        not isinstance(paths, list)
        or not paths
        or not all(isinstance(item, str) and item for item in paths)
    ):
        raise ValueError("patch paths are missing")
    if value.get("summary") != proposal.summary:
        raise ValueError("patch summary does not match the proposal")
    return {
        "summary": proposal.summary,
        "paths": list(paths),
        "diff": diff,
        "proposal": proposal,
    }


def _accepted_tests(state: GraphState, patch: dict[str, Any] | None) -> TestReport | None:
    """Return the runner report, or None when this task did not develop."""
    report = _checked_tests(state.get("tests"))
    if patch is None:
        if report is not None:
            raise ValueError("tests without a patch")
        return None
    if report is None:
        raise ValueError("patch has no test report")
    task_raw = state.get("task_id")
    if not isinstance(task_raw, str):
        raise ValueError("test job does not match the task")
    try:
        expected = test_job_id(UUID(task_raw), _repair_count(state))
    except (TypeError, ValueError, RunnerRejected):
        raise ValueError("test job does not match the task") from None
    if report.job_id != expected:
        raise ValueError("test job does not match the task")
    if report.status == TestStatus.INVALID_REQUEST:
        raise ValueError("test runner rejected the job")
    return report


def _checked_tests(value: object) -> TestReport | None:
    """Re-validate a stored report. None means this task did not ask the runner."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("test report is not an object")
    try:
        return TestReport.model_validate(value)
    except ValidationError:
        raise ValueError("test report is not valid") from None


def _unchanged_failure_identities(
    state: GraphState,
    tests: TestReport | None = None,
) -> tuple[str, ...]:
    """Pytest identities in the patched report that the baseline already had."""
    report = tests
    if report is None:
        try:
            report = _checked_tests(state.get("tests"))
        except ValueError:
            return ()
    if report is None or report.status is TestStatus.PASSED or report.timed_out:
        return ()
    try:
        baseline = _checked_tests(state.get("baseline_tests"))
    except ValueError:
        return ()
    if baseline is None:
        return ()
    current = failure_identities(report.stdout)
    known = set(failure_identities(baseline.stdout))
    if not current or any(item not in known for item in current):
        return ()
    return current


def _completed_summary(summary: str, identities: tuple[str, ...]) -> str:
    """Name a red suite that matches the unmodified worktree."""
    if not identities:
        return summary
    note = "Unchanged failure: " + ", ".join(identities) + "."
    return _message(f"{summary} {note}")


def _store_tests(body: dict[str, Any], report: TestReport | None) -> TestReport | None:
    """Record the outcome on the task. Captured output stays in the artifact."""
    if report is None:
        return None
    body["tests"] = {
        "passed": report.status == TestStatus.PASSED,
        "status": report.status.value,
        "exit_code": report.exit_code,
        "timed_out": report.timed_out,
        "duration_ms": report.duration_ms,
        "output_truncated": report.output_truncated,
    }
    return report


def _store_patch(body: dict[str, Any], patch: dict[str, Any] | None) -> DevelopOutcome | None:
    """Record checksums on the task. The diff body stays out of result_json."""
    if patch is None:
        return None
    diff = patch["diff"].encode()
    proposal = patch["proposal"]
    body["patch"] = {
        "summary": patch["summary"],
        "paths": list(patch["paths"]),
        "checksum": checksum(diff),
        "proposal_checksum": checksum(proposal_document(proposal)),
    }
    return DevelopOutcome(
        proposal=proposal,
        applied=AppliedPatch(summary=patch["summary"], paths=tuple(patch["paths"]), diff=diff),
    )


def _accepted_workspace(
    state: GraphState,
    plan: dict[str, Any] | None,
) -> IsolatedWorkspace | None:
    """Return the derived workspace, or None when the plan names no edit."""
    raw = state.get("workspace")
    if not _plan_requests_changes(plan):
        if raw is not None:
            raise ValueError("workspace without a code change")
        return None
    task_raw = state.get("task_id")
    if not isinstance(task_raw, str):
        raise ValueError("task id missing")
    workspace = workspace_from_state(raw, task_id=UUID(task_raw))
    if workspace is None:
        raise ValueError("approved plan has no workspace")
    return workspace


def _planned_body(
    triage: TriageResult,
    investigation: dict[str, Any],
    plan: dict[str, Any],
    approval: dict[str, Any] | None,
    workspace: IsolatedWorkspace | None = None,
) -> tuple[dict[str, Any], ChangePlan]:
    accepted = ChangePlan.model_validate(plan["plan"])
    body: dict[str, Any] = {
        "triage": triage.model_dump(mode="json"),
        "investigation": investigation,
        "plan": {
            "can_proceed": accepted.can_proceed,
            "confidence": accepted.confidence,
            "rejected_paths": plan["rejected_paths"],
            "paths": [item.relative_path for item in accepted.modifications],
        },
    }
    if approval is not None:
        recorded: dict[str, Any] = {
            "decision": approval["decision"],
            "approval_id": approval["approval_id"],
        }
        if approval.get("reason"):
            recorded["reason"] = approval["reason"]
        body["approval"] = recorded
    if workspace is not None:
        body["workspace"] = {"base_sha": workspace.base_sha, "branch": workspace.branch}
    return body, accepted


def _plan_requests_changes(value: object) -> bool:
    """True when a stored plan still asks for a later edit."""
    try:
        checked = value if _is_checked_plan(value) else _checked_plan(value)
    except (ValidationError, ValueError):
        return False
    if not isinstance(checked, dict):
        return False
    try:
        plan = ChangePlan.model_validate(checked.get("plan"))
    except ValidationError:
        return False
    return plan.can_proceed and len(plan.modifications) > 0


def _is_checked_plan(value: object) -> bool:
    return isinstance(value, dict) and isinstance(value.get("plan"), dict)


def _publish_prompt(state: TriageState, binding: GitHubBinding) -> dict[str, Any]:
    """What the human sees. The token is not included."""
    task_id = state.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task id missing")
    if _checked_approval(state.get("approval")) is None:
        raise ValueError("publication without plan approval")
    if not isinstance(state.get("patch"), dict) or not _tests_passed(state):
        raise ValueError("publication without a tested patch")
    return {
        "kind": "publish",
        "task_id": task_id,
        "owner": binding.repo.owner,
        "name": binding.repo.name,
        "branch": f"patchpilot/{task_id}",
    }


def _plan_reasoning(state: TriageState) -> str:
    plan = _checked_plan(state.get("plan"))
    if plan is None:
        return ""
    statement = plan["plan"].get("problem_statement")
    if not isinstance(statement, str):
        return ""
    return statement


def _tests_passed(state: TriageState) -> bool:
    """True when the patched suite passed, or it added no baseline failure."""
    tests = state.get("tests")
    if not isinstance(tests, dict):
        return False
    if tests.get("status") == "passed":
        return True
    try:
        report = _checked_tests(tests)
    except ValueError:
        return False
    if report is None:
        return False
    try:
        _line, no_new_failures, _output = _baseline_context(state, report)
    except ValueError:
        return False
    return no_new_failures


def _baseline_context(state: GraphState, tests: TestReport) -> tuple[str, bool, str]:
    """The prompt line, whether the patch added no failure, and the baseline text."""
    baseline = _checked_tests(state.get("baseline_tests"))
    if baseline is None:
        return "none", False, ""
    identities = failure_identities(baseline.stdout)
    line = ", ".join(identities) if identities else "none"
    output = f"{baseline.stdout}\n{baseline.stderr}"
    introduced = has_new_failure(
        baseline_stdout=baseline.stdout,
        patched_stdout=tests.stdout,
        patched_passed=tests.status is TestStatus.PASSED,
        patched_timed_out=tests.timed_out,
        patched_truncated=tests.output_truncated,
    )
    return line, not introduced, output


def _tests_summary(state: TriageState) -> str:
    tests = state.get("tests")
    if isinstance(tests, dict) and isinstance(tests.get("status"), str):
        return tests["status"]
    return ""


def _checked_publication(value: object) -> dict[str, Any] | None:
    """Re-validate a publication decision. The repository comes from the binding."""
    parsed = _parse_resume(value, kind="publish")
    if parsed is None or not isinstance(value, dict):
        return None
    owner = value.get("owner")
    name = value.get("name")
    if not isinstance(owner, str) or not isinstance(name, str) or not owner or not name:
        raise ValueError("publication repository is not valid")
    return {**parsed, "owner": owner, "name": name}


def _attach_publication(outcome: TriageOutcome, state: GraphState) -> TriageOutcome:
    publication = state.get("publication")
    if publication is None:
        return outcome
    record = _public_publication(publication)
    body = dict(outcome.result_json)
    body["publication"] = record
    return replace(outcome, result_json=body, publication=record)


def _public_publication(value: object) -> dict[str, Any]:
    """The stored record. Keys that could carry a secret are dropped."""
    checked = _checked_publication(value)
    if checked is None or not isinstance(value, dict):
        raise ValueError("publication decision is not valid")
    record: dict[str, Any] = {
        "decision": checked["decision"],
        "kind": "publish",
        "approval_id": checked["approval_id"],
        "owner": checked["owner"],
        "name": checked["name"],
    }
    if checked.get("reason"):
        record["reason"] = checked["reason"]
    if checked["decision"] != "approve":
        return record
    number = value.get("number")
    url = value.get("url")
    branch = value.get("branch")
    base = value.get("base")
    head_sha = value.get("head_sha")
    if value.get("draft") is not True:
        raise ValueError("publication is not a draft")
    if isinstance(number, bool) or not isinstance(number, int):
        raise ValueError("publication number is not valid")
    if not all(isinstance(item, str) and item for item in (url, branch, base, head_sha)):
        raise ValueError("publication address is not valid")
    record.update(
        {
            "number": number,
            "url": url,
            "branch": branch,
            "base": base,
            "head_sha": head_sha,
            "draft": True,
        }
    )
    return record


def _interrupt_kind(state: GraphState) -> str:
    raw = state.get(_INTERRUPT)
    if not isinstance(raw, (list, tuple)) or not raw:
        return "plan"
    item = raw[0]
    value = item.get("value") if isinstance(item, dict) else getattr(item, "value", None)
    if isinstance(value, dict) and value.get("kind") == "publish":
        return "publish"
    return "plan"


def _approval_prompt(state: TriageState) -> dict[str, Any]:
    plan = _checked_plan(state.get("plan"))
    if plan is None or not _plan_requests_changes(plan):
        raise ValueError("plan does not request a change")
    accepted = ChangePlan.model_validate(plan["plan"])
    task_id = state.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task id missing")
    return {
        "task_id": task_id,
        "paths": [item.relative_path for item in accepted.modifications],
    }


def _parse_resume(value: object, *, kind: str = "plan") -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if value.get("kind", "plan") != kind:
        return None
    decision = value.get("decision")
    if decision not in {"approve", "reject"}:
        return None
    raw_id = value.get("approval_id")
    if not isinstance(raw_id, str):
        return None
    try:
        approval_id = str(UUID(raw_id))
    except ValueError:
        return None
    reason = value.get("reason")
    if reason is not None:
        if not isinstance(reason, str):
            return None
        reason = " ".join(reason.split())
        if not reason:
            reason = None
        elif len(reason) > APPROVAL_REASON_MAX_LENGTH:
            return None
    return {
        "decision": decision,
        "approval_id": approval_id,
        "reason": reason,
        "kind": kind,
    }


def _checked_approval(value: object) -> dict[str, Any] | None:
    """Re-validate a stored decision. None means this task has no decision yet."""
    if value is None:
        return None
    parsed = _parse_resume(value)
    if parsed is None:
        raise ValueError("approval decision is not valid")
    return parsed


def _rejection_summary(reason: object) -> str:
    if not isinstance(reason, str):
        return REJECTED_SUMMARY
    text = " ".join(reason.split())
    if not text:
        return REJECTED_SUMMARY
    return text[:MAX_FAILURE_CHARS]


def _lease_is_lost(runtime: Runtime[WorkflowContext]) -> bool:
    probe = runtime.context.lease_lost
    if probe is None:
        return False
    return bool(probe())


def _checked_investigation(value: object) -> dict[str, Any] | None:
    """Re-validate a stored report. None means this task did not investigate."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("investigation payload is not an object")
    report = InvestigationReport.model_validate(value.get("report"))
    tool_calls = value.get("tool_calls")
    rejected = value.get("rejected_citations")
    if isinstance(tool_calls, bool) or not isinstance(tool_calls, int) or tool_calls < 0:
        raise ValueError("investigation tool count is not a count")
    if isinstance(rejected, bool) or not isinstance(rejected, int) or rejected < 0:
        raise ValueError("investigation rejection count is not a count")
    return {
        "report": report.model_dump(mode="json"),
        "tool_calls": tool_calls,
        "rejected_citations": rejected,
    }


def _checked_plan(value: object) -> dict[str, Any] | None:
    """Re-validate a stored plan. None means this task did not plan."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("plan payload is not an object")
    plan = ChangePlan.model_validate(value.get("plan"))
    rejected = value.get("rejected_paths")
    if isinstance(rejected, bool) or not isinstance(rejected, int) or rejected < 0:
        raise ValueError("plan rejection count is not a count")
    return {"plan": plan.model_dump(mode="json"), "rejected_paths": rejected}


def _interrupted_repair(state: GraphState) -> bool:
    """True when develop or test failed after the last review still asked for a repair."""
    if state.get("status") != "failed":
        return False
    return state.get("failed_stage") in {STAGE_DEVELOP, STAGE_TEST}


def _failed_update(message: str, *, stage: str = STAGE_TRIAGE) -> dict[str, Any]:
    text = _message(message)
    failed = stage if stage in _FAILURE_STAGES else STAGE_TRIAGE
    update: dict[str, Any] = {
        "status": "failed",
        "error": text,
        "result_summary": text,
        "failed_stage": failed,
        "phase": failed,
    }
    if failed == STAGE_TRIAGE:
        update["triage"] = None
    return update


def _message(value: object) -> str:
    if not isinstance(value, str):
        return GENERIC_FAILURE
    text = " ".join(value.split())
    if not text:
        return GENERIC_FAILURE
    return text[:MAX_FAILURE_CHARS]
