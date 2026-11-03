"""Triage workflow: START -> triage -> finalize -> END.

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
from pydantic import ValidationError

from patchpilot.db.models import TaskStatus
from patchpilot.llm.errors import LLMError
from patchpilot.llm.provider import LLMProvider
from patchpilot.llm.triage import TriageResult

logger = logging.getLogger(__name__)

STAGE_TRIAGE = "triage"
STAGE_FINALIZE = "finalize"
GENERIC_FAILURE = "Triage failed before a valid result was produced."
MAX_FAILURE_CHARS = 500
# Persist the checkpoint before the next node. "async" can still be in flight
# when a node, or the process, ends.
CHECKPOINT_DURABILITY = "sync"

WorkflowStatus = Literal["running", "completed", "failed"]


class TriageState(TypedDict):
    """Small values only. The model provider is run context, not state."""

    task_id: str
    title: str
    description: str
    triage: dict[str, Any] | None
    result_summary: str | None
    error: str | None
    status: WorkflowStatus


class LeaseLost(Exception):
    """This worker no longer holds the lease, so the node must not checkpoint a success."""


@dataclass(frozen=True, slots=True)
class WorkflowContext:
    """Dependencies for one run. They are not written to a checkpoint."""

    provider: LLMProvider
    lease_lost: Callable[[], bool] | None = None


@dataclass(frozen=True, slots=True)
class TriageOutcome:
    """What the worker stores after the graph returns."""

    status: TaskStatus
    stage: str
    result_summary: str
    result_json: dict[str, Any]


def triage_prompt(*, title: str, description: str) -> str:
    """The provider sees the report text and nothing from the database."""
    return f"Title: {title}\nDescription:\n{description}\n"


def thread_config(task_id: UUID) -> dict[str, Any]:
    """Stable LangGraph thread. The id is the task UUID and nothing else."""
    return {"configurable": {"thread_id": str(task_id)}}


def initial_triage_state(*, task_id: UUID, title: str, description: str) -> TriageState:
    return {
        "task_id": str(task_id),
        "title": title,
        "description": description,
        "triage": None,
        "result_summary": None,
        "error": None,
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
    builder.add_node("finalize", finalize_node)
    builder.add_edge(START, "triage")
    builder.add_edge("triage", "finalize")
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
) -> TriageOutcome:
    """Run one triage. Provider failures become a failed outcome instead of raw text."""
    state = initial_triage_state(task_id=task_id, title=title, description=description)
    try:
        final = await invoke_triage(
            graph,
            state,
            task_id=task_id,
            provider=provider,
            lease_lost=lease_lost,
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
) -> TriageOutcome:
    """Resume a checkpointed thread, or start one when no checkpoint exists.

    A finished thread returns its stored outcome and does not call the model again.
    """
    if graph.checkpointer:
        existing = await read_thread_state(graph, task_id)
        if existing:
            try:
                final = await invoke_triage(
                    graph,
                    None,
                    task_id=task_id,
                    provider=provider,
                    resume=True,
                    lease_lost=lease_lost,
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
    )


async def invoke_triage(
    graph: CompiledStateGraph,
    state: TriageState | None,
    *,
    task_id: UUID,
    provider: LLMProvider,
    resume: bool = False,
    lease_lost: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run or resume one thread. `resume=True` passes no new input."""
    options: dict[str, Any] = {}
    if graph.checkpointer:
        options["durability"] = CHECKPOINT_DURABILITY
    result = await graph.ainvoke(
        None if resume else state,
        thread_config(task_id),
        context=WorkflowContext(provider=provider, lease_lost=lease_lost),
        **options,
    )
    if not isinstance(result, dict):
        raise TypeError(type(result).__name__)
    return result


async def read_thread_state(graph: CompiledStateGraph, task_id: UUID) -> dict[str, Any]:
    """Load the latest checkpoint values for this task. Empty when none exist."""
    snapshot = await graph.aget_state(thread_config(task_id))
    values = snapshot.values
    if not isinstance(values, dict) or not values:
        return {}
    return dict(values)


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


def finalize_node(state: TriageState) -> dict[str, Any]:
    """Re-validate before the summary becomes user-facing state."""
    if state.get("status") == "failed" or state.get("error"):
        message = _message(state.get("error"))
        return _failed_update(message)
    try:
        checked = TriageResult.model_validate(state.get("triage"))
    except ValidationError:
        logger.error("finalize rejected the triage payload")
        return _failed_update(GENERIC_FAILURE)
    return {
        "status": "completed",
        "error": None,
        "result_summary": checked.summary,
        "triage": checked.model_dump(mode="json"),
    }


def failure_outcome(message: str) -> TriageOutcome:
    text = _message(message)
    return TriageOutcome(
        status=TaskStatus.FAILED,
        stage=STAGE_TRIAGE,
        result_summary=text,
        result_json={"error": text},
    )


def outcome_from_state(state: TriageState) -> TriageOutcome:
    if state.get("status") != "completed" or state.get("error"):
        return failure_outcome(_message(state.get("error") or state.get("result_summary")))
    try:
        checked = TriageResult.model_validate(state.get("triage"))
    except ValidationError:
        return failure_outcome(GENERIC_FAILURE)
    return TriageOutcome(
        status=TaskStatus.COMPLETED,
        stage=STAGE_FINALIZE,
        result_summary=checked.summary,
        result_json=checked.model_dump(mode="json"),
    )


def failure_text(exc: LLMError) -> str:
    return _message(str(exc))


def _lease_is_lost(runtime: Runtime[WorkflowContext]) -> bool:
    probe = runtime.context.lease_lost
    if probe is None:
        return False
    return bool(probe())


def _failed_update(message: str) -> dict[str, Any]:
    text = _message(message)
    return {
        "status": "failed",
        "error": text,
        "result_summary": text,
        "triage": None,
    }


def _message(value: object) -> str:
    if not isinstance(value, str):
        return GENERIC_FAILURE
    text = " ".join(value.split())
    if not text:
        return GENERIC_FAILURE
    return text[:MAX_FAILURE_CHARS]
