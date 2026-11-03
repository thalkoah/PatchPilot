"""Triage workflow: START -> triage -> finalize -> END.

The compiled graph has no checkpointer. PostgreSQL stores the business result.
Durable graph checkpoints arrive in a later task.
"""

import logging
from dataclasses import dataclass
from typing import Any, Literal, TypedDict
from uuid import UUID

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


@dataclass(frozen=True, slots=True)
class WorkflowContext:
    """Dependencies for one run. They are not written to a checkpoint."""

    provider: LLMProvider


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


def compile_triage_graph() -> CompiledStateGraph:
    """Build the one-shot triage graph. `checkpointer=False` disables inheritance."""
    builder: StateGraph[TriageState, WorkflowContext, TriageState, TriageState] = StateGraph(
        TriageState,
        context_schema=WorkflowContext,
    )
    builder.add_node("triage", triage_node)
    builder.add_node("finalize", finalize_node)
    builder.add_edge(START, "triage")
    builder.add_edge("triage", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=False)


def run_triage(
    graph: CompiledStateGraph,
    *,
    task_id: UUID,
    title: str,
    description: str,
    provider: LLMProvider,
) -> TriageOutcome:
    """Run one triage. Provider failures become a failed outcome instead of raw text."""
    state: TriageState = {
        "task_id": str(task_id),
        "title": title,
        "description": description,
        "triage": None,
        "result_summary": None,
        "error": None,
        "status": "running",
    }
    try:
        final = graph.invoke(state, context=WorkflowContext(provider=provider))
    except Exception as exc:
        logger.error("triage graph failed: %s", type(exc).__name__)
        return failure_outcome(GENERIC_FAILURE)
    return outcome_from_state(final)


def triage_node(state: TriageState, runtime: Runtime[WorkflowContext]) -> dict[str, Any]:
    prompt = triage_prompt(title=state["title"], description=state["description"])
    try:
        result = runtime.context.provider.generate_structured(prompt, TriageResult)
    except LLMError as exc:
        return _failed_update(failure_text(exc))
    except Exception as exc:
        logger.error("triage node failed: %s", type(exc).__name__)
        return _failed_update(GENERIC_FAILURE)
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
