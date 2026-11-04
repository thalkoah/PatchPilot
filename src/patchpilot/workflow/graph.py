"""Triage workflow.

A task with no repository follows `START -> triage -> finalize -> END`. A task
with a repo_id follows `START -> triage -> investigate -> plan`, then either
`finalize` or `request_plan_approval -> finalize`. The approval node runs only
when the plan names a code change. It calls `interrupt()` and does no I/O
before that call, because resume runs the node from the top.

The investigate node is the only place that reads a checkout. The plan node
proposes paths and does not write them. Neither node runs when triage failed,
and plan does not run when investigation failed. Nothing in this graph writes
the registered checkout.

The worker compiles this graph with an AsyncPostgresSaver. The task UUID is the
LangGraph thread id. Checkpoint rows are not the business task record: the
worker still writes status, stage, summary, and result_json on `tasks`.

`durability="sync"` stores each finished node before the next one starts, so a
later resume does not run that node again.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from patchpilot.agents.errors import StopForLease
from patchpilot.agents.investigator import InvestigatorAgent
from patchpilot.agents.limits import DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS
from patchpilot.agents.planner import PlannerAgent
from patchpilot.db.models import APPROVAL_REASON_MAX_LENGTH, TaskStatus
from patchpilot.llm.errors import LLMError
from patchpilot.llm.investigation import InvestigationReport
from patchpilot.llm.plan import ChangePlan
from patchpilot.llm.provider import LLMProvider
from patchpilot.llm.triage import TriageResult
from patchpilot.source.tools import RepositoryTools

logger = logging.getLogger(__name__)

STAGE_TRIAGE = "triage"
STAGE_INVESTIGATE = "investigate"
STAGE_PLAN = "plan"
STAGE_APPROVAL = "request_plan_approval"
STAGE_FINALIZE = "finalize"
GENERIC_FAILURE = "Triage failed before a valid result was produced."
INVESTIGATION_FAILURE = "Investigation failed before a valid report was produced."
PLAN_FAILURE = "Planning failed before a valid change plan was produced."
APPROVAL_FAILURE = "Approval resume did not continue the workflow."
ARTIFACT_FAILURE = "The plan artifact could not be stored."
REPOSITORY_UNAVAILABLE = "The registered repository could not be opened."
REJECTED_SUMMARY = "The plan was rejected."
MAX_FAILURE_CHARS = 500
_FAILURE_STAGES = frozenset({STAGE_TRIAGE, STAGE_INVESTIGATE, STAGE_PLAN, STAGE_APPROVAL})
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
    result_summary: str | None
    error: str | None
    failed_stage: str | None
    status: WorkflowStatus


class LeaseLost(Exception):
    """This worker no longer holds the lease, so the node must not checkpoint a success."""


@dataclass(frozen=True, slots=True)
class WorkflowContext:
    """Dependencies for one run. They are not written to a checkpoint."""

    provider: LLMProvider
    lease_lost: Callable[[], bool] | None = None
    repository_tools: RepositoryTools | None = None
    max_tool_calls: int = DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS
    repository_error: str | None = None


@dataclass(frozen=True, slots=True)
class TriageOutcome:
    """What the worker stores after the graph returns."""

    status: TaskStatus
    stage: str
    result_summary: str
    result_json: dict[str, Any]
    plan: ChangePlan | None = None


def triage_prompt(*, title: str, description: str) -> str:
    """The provider sees the report text and nothing from the database."""
    return f"Title: {title}\nDescription:\n{description}\n"


def thread_config(task_id: UUID) -> dict[str, Any]:
    """Stable LangGraph thread. The id is the task UUID and nothing else."""
    return {"configurable": {"thread_id": str(task_id)}}


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
        "result_summary": None,
        "error": None,
        "failed_stage": None,
        "status": "running",
    }


def compile_triage_graph(
    checkpointer: BaseCheckpointSaver | Literal[False] = False,
    *,
    interrupt_after: list[str] | None = None,
) -> CompiledStateGraph:
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
    builder.add_node("investigate", investigate_node)
    builder.add_node("plan", plan_node)
    builder.add_node("request_plan_approval", request_plan_approval)
    builder.add_node("finalize", finalize_node)
    builder.add_edge(START, "triage")
    builder.add_conditional_edges(
        "triage",
        route_after_triage,
        {"investigate": "investigate", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "investigate",
        route_after_investigate,
        {"plan": "plan", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "plan",
        route_after_plan,
        {"request_plan_approval": "request_plan_approval", "finalize": "finalize"},
    )
    builder.add_edge("request_plan_approval", "finalize")
    builder.add_edge("finalize", END)
    options: dict[str, Any] = {}
    if interrupt_after:
        options["interrupt_after"] = interrupt_after
    return builder.compile(checkpointer=checkpointer, **options)


async def run_triage(
    graph: CompiledStateGraph,
    *,
    task_id: UUID,
    title: str,
    description: str,
    provider: LLMProvider,
    lease_lost: Callable[[], bool] | None = None,
    repo_id: UUID | None = None,
    repository_tools: RepositoryTools | None = None,
    max_tool_calls: int = DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS,
    repository_error: str | None = None,
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
            max_tool_calls=max_tool_calls,
            repository_error=repository_error,
        )
    except LeaseLost:
        raise
    except Exception as exc:
        logger.error("triage graph failed: %s", type(exc).__name__)
        return failure_outcome(GENERIC_FAILURE)
    return outcome_from_state(final)


async def run_claimed_triage(
    graph: CompiledStateGraph,
    *,
    task_id: UUID,
    title: str,
    description: str,
    provider: LLMProvider,
    lease_lost: Callable[[], bool] | None = None,
    repo_id: UUID | None = None,
    repository_tools: RepositoryTools | None = None,
    max_tool_calls: int = DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS,
    repository_error: str | None = None,
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
                    max_tool_calls=max_tool_calls,
                    repository_error=repository_error,
                )
            except LeaseLost:
                raise
            except Exception as exc:
                logger.error("approval resume failed: %s", type(exc).__name__)
                return failure_outcome(GENERIC_FAILURE)
            outcome = outcome_from_state(final)
            if outcome.status == TaskStatus.WAITING_APPROVAL:
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
                    max_tool_calls=max_tool_calls,
                    repository_error=repository_error,
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
        max_tool_calls=max_tool_calls,
        repository_error=repository_error,
    )


async def invoke_triage(
    graph: CompiledStateGraph,
    state: TriageState | None,
    *,
    task_id: UUID,
    provider: LLMProvider,
    resume: bool = False,
    resume_decision: dict[str, Any] | None = None,
    lease_lost: Callable[[], bool] | None = None,
    repository_tools: RepositoryTools | None = None,
    max_tool_calls: int = DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS,
    repository_error: str | None = None,
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
            max_tool_calls=max_tool_calls,
            repository_error=repository_error,
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


async def read_thread_state(graph: CompiledStateGraph, task_id: UUID) -> dict[str, Any]:
    """Load the latest checkpoint values for this task. Empty when none exist."""
    snapshot = await graph.aget_state(thread_config(task_id))
    values = snapshot.values
    if not isinstance(values, dict) or not values:
        return {}
    return dict(values)


def route_after_triage(state: TriageState) -> str:
    """Investigate only after a successful triage of a task that names a repository."""
    if state.get("status") == "failed" or state.get("error") or not state.get("repo_id"):
        return "finalize"
    return "investigate"


def route_after_investigate(state: TriageState) -> str:
    """Plan only after a stored investigation. A failed read does not call the planner."""
    if state.get("status") == "failed" or state.get("error") or not state.get("investigation"):
        return "finalize"
    return "plan"


def route_after_plan(state: TriageState) -> str:
    """Ask for approval only when the plan names a code change."""
    if state.get("status") == "failed" or state.get("error"):
        return "finalize"
    if not _plan_requests_changes(state.get("plan")):
        return "finalize"
    return "request_plan_approval"


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
        "status": "rejected" if parsed["decision"] == "reject" else "completed",
    }


def triage_node(state: TriageState, runtime: Runtime[WorkflowContext]) -> dict[str, Any]:
    prompt = triage_prompt(title=state["title"], description=state["description"])
    try:
        result = runtime.context.provider.generate_structured(prompt, TriageResult)
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
            context.provider,
            context.repository_tools,
            max_tool_calls=context.max_tool_calls,
            lease_lost=context.lease_lost,
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
            context.provider,
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
    summary = checked.summary
    if plan is not None:
        summary = str(plan["plan"]["problem_statement"])
    return {
        "status": "completed",
        "error": None,
        "failed_stage": None,
        "result_summary": summary,
        "triage": checked.model_dump(mode="json"),
        "investigation": investigation,
        "plan": plan,
        "approval": approval,
    }


def failure_outcome(message: str, *, stage: str = STAGE_TRIAGE) -> TriageOutcome:
    text = _message(message)
    return TriageOutcome(
        status=TaskStatus.FAILED,
        stage=stage if stage in _FAILURE_STAGES else STAGE_TRIAGE,
        result_summary=text,
        result_json={"error": text},
    )


def outcome_from_state(state: TriageState) -> TriageOutcome:
    if _INTERRUPT in state:
        return _pause_or_fail(state)
    if state.get("status") == "rejected":
        try:
            return _rejected_outcome(state)
        except (ValidationError, ValueError):
            return failure_outcome(GENERIC_FAILURE)
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
    if plan is None or investigation is None:
        return TriageOutcome(
            status=TaskStatus.COMPLETED,
            stage=STAGE_FINALIZE,
            result_summary=checked.summary,
            result_json=checked.model_dump(mode="json"),
        )
    body, accepted = _planned_body(checked, investigation, plan, approval)
    return TriageOutcome(
        status=TaskStatus.COMPLETED,
        stage=STAGE_FINALIZE,
        result_summary=accepted.problem_statement,
        result_json=body,
        plan=accepted,
    )


def failure_text(exc: LLMError) -> str:
    return _message(str(exc))


def _pause_or_fail(state: dict[str, Any]) -> TriageOutcome:
    try:
        return _paused_outcome(state)
    except (ValidationError, ValueError):
        logger.error("approval pause rejected the stored plan")
        return failure_outcome(PLAN_FAILURE, stage=STAGE_PLAN)


def _paused_outcome(state: dict[str, Any]) -> TriageOutcome:
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


def _rejected_outcome(state: dict[str, Any]) -> TriageOutcome:
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
    }


def _planned_body(
    triage: TriageResult,
    investigation: dict[str, Any],
    plan: dict[str, Any],
    approval: dict[str, Any] | None,
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


def _parse_resume(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
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
    return {"decision": decision, "approval_id": approval_id, "reason": reason}


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


def _failed_update(message: str, *, stage: str = STAGE_TRIAGE) -> dict[str, Any]:
    text = _message(message)
    return {
        "status": "failed",
        "error": text,
        "result_summary": text,
        "failed_stage": stage if stage in _FAILURE_STAGES else STAGE_TRIAGE,
        "triage": None,
    }


def _message(value: object) -> str:
    if not isinstance(value, str):
        return GENERIC_FAILURE
    text = " ".join(value.split())
    if not text:
        return GENERIC_FAILURE
    return text[:MAX_FAILURE_CHARS]
