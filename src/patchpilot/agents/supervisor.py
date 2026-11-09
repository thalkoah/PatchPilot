"""Deterministic supervisor. A model may recommend only a route policy already allows.

The supervisor does not read the checkout, apply a patch, approve a plan, or
start tests. It names the next graph node and the reason for that choice.
Cycle counts, the step cap, and the deadline are enforced here. A recommendation
outside the allowed set is ignored.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeGuard

from pydantic import ValidationError

from patchpilot.agents.limits import (
    DEFAULT_MAX_REPAIRS,
    DEFAULT_SUPERVISOR_DEADLINE_SECONDS,
    MAX_INVESTIGATIONS,
    MAX_PLANS,
    MAX_SUPERVISOR_STEPS,
    require_supervisor_deadline,
)
from patchpilot.agents.review_policy import PolicyAction
from patchpilot.llm.errors import LLMError
from patchpilot.llm.investigation import InvestigationReport
from patchpilot.llm.limits import MAX_PROMPT_CHARS
from patchpilot.llm.plan import ChangePlan
from patchpilot.llm.provider import LLMProvider
from patchpilot.llm.supervisor import SupervisorRecommendation, SupervisorRoute

_REASON_CHARS = 300
ABSENT_REPO = "The task has no repository, so investigation is skipped."
INVESTIGATE_REASON = "A registered repository is attached."
INVESTIGATE_MORE_REASON = "The investigation cited no line."
NO_PYTHON_REASON = "The investigation cited no Python file."
PLAN_REASON = "The investigation is finished."
REPLAN_REASON = "The plan named no permissible path."
PROCEED_REASON = "The plan names a code change."
DECLINE_REASON = "The plan declines to change code."
REJECTED_REASON = "The plan was rejected."
NOT_APPROVED_REASON = "The plan was not approved."
APPROVED_REASON = "The plan was approved."
WORKSPACE_REASON = "The private worktree is ready."
BASELINE_REASON = "The unmodified worktree has not been tested."
BASELINE_READY_REASON = "The baseline test report is stored."
BASELINE_MISSING = "The baseline test report is missing."
PATCH_REASON = "A patch was applied in the private worktree."
TEST_REASON = "A test report is stored."
REPAIR_REASON = "Policy requested a repair."
ACCEPT_REASON = "Policy accepted the patch."
PUBLISH_READY_REASON = "Policy accepted the patch and publication is configured."
PUBLICATION_APPROVED_REASON = "Publication was approved."
PUBLICATION_DECLINED_REASON = "Publication was declined."
PUBLISHED_REASON = "A draft pull request was opened."
ESCALATE_REASON = "Policy did not accept the patch."
STAGE_FAILED = "The stage failed."
STEP_LIMIT_REASON = "The supervisor step limit was reached."
DEADLINE_REASON = "The supervisor deadline was reached."
COUNTER_REASON = "The supervisor counters are not valid."
UNRECOGNIZED_REASON = "The supervisor did not recognize the stage."
INVESTIGATION_MISSING = "Investigation did not produce a report."
TRIAGE_MISSING = "Triage did not produce a result."
PLAN_MISSING = "Planning did not produce a plan."
WORKSPACE_MISSING = "The workspace was not created."
PATCH_MISSING = "No patch was applied."
TEST_MISSING = "The test report is missing."
MODEL_SELECTED = "The supervisor model selected this allowed route."
MODEL_REJECTED = "which policy rejected."
MODEL_OUTPUT_REJECTED = "The supervisor model output was rejected."

NODE_FOR_ROUTE = {
    SupervisorRoute.INVESTIGATE: "investigate",
    SupervisorRoute.INVESTIGATE_MORE: "investigate",
    SupervisorRoute.PLAN: "plan",
    SupervisorRoute.REPLAN: "plan",
    SupervisorRoute.PROCEED_TO_APPROVAL: "request_plan_approval",
    SupervisorRoute.PREPARE_WORKSPACE: "prepare_workspace",
    SupervisorRoute.DEVELOP: "develop",
    SupervisorRoute.TEST: "test",
    SupervisorRoute.REVIEW: "review",
    SupervisorRoute.REPAIR: "develop",
    SupervisorRoute.REQUEST_PUBLISH_APPROVAL: "request_publish_approval",
    SupervisorRoute.PUBLISH: "publish",
    SupervisorRoute.FINALIZE: "finalize",
    SupervisorRoute.ESCALATE: "finalize",
}


@dataclass(frozen=True, slots=True)
class SupervisorDecision:
    """The authoritative next route, why it was chosen, and what else was safe."""

    route: SupervisorRoute
    reason: str
    allowed: frozenset[SupervisorRoute]


def decide_route(
    state: Mapping[str, Any],
    *,
    now: float | None = None,
    deadline_seconds: float = DEFAULT_SUPERVISOR_DEADLINE_SECONDS,
    publish_available: bool = False,
) -> SupervisorDecision:
    """Choose the next route from the stage that just finished."""
    try:
        deadline = require_supervisor_deadline(deadline_seconds)
    except ValueError:
        deadline = DEFAULT_SUPERVISOR_DEADLINE_SECONDS
    steps = _count(state, "supervisor_steps")
    if steps is None:
        return _one(SupervisorRoute.ESCALATE, COUNTER_REASON)
    if steps >= MAX_SUPERVISOR_STEPS:
        return _one(SupervisorRoute.ESCALATE, STEP_LIMIT_REASON)
    if _past_deadline(state, now=now, deadline_seconds=deadline):
        return _one(SupervisorRoute.ESCALATE, DEADLINE_REASON)
    status = state.get("status")
    if status == "rejected":
        return _one(SupervisorRoute.FINALIZE, REJECTED_REASON)
    phase = _phase(state)
    error = state.get("error")
    if status == "failed" or error:
        reason = _error_text(error)
        if phase == "review" or _has_cycles(state):
            return _one(SupervisorRoute.ESCALATE, reason or ESCALATE_REASON)
        return _one(SupervisorRoute.FINALIZE, reason or STAGE_FAILED)
    if phase == "triage":
        if not state.get("repo_id"):
            return _one(SupervisorRoute.FINALIZE, ABSENT_REPO)
        if not _mapping(state.get("triage")):
            return _one(SupervisorRoute.FINALIZE, TRIAGE_MISSING)
        return _one(SupervisorRoute.INVESTIGATE, INVESTIGATE_REASON)
    if phase == "investigate":
        return _after_investigation(state)
    if phase == "plan":
        return _after_plan(state)
    if phase == "request_plan_approval":
        if state.get("status") == "running" and _approved(state):
            return _one(SupervisorRoute.PREPARE_WORKSPACE, APPROVED_REASON)
        return _one(SupervisorRoute.FINALIZE, NOT_APPROVED_REASON)
    if phase == "prepare_workspace":
        if _mapping(state.get("workspace")):
            if not _mapping(state.get("baseline_tests")):
                return _one(SupervisorRoute.TEST, BASELINE_REASON)
            return _one(SupervisorRoute.DEVELOP, WORKSPACE_REASON)
        return _one(SupervisorRoute.FINALIZE, WORKSPACE_MISSING)
    if phase == "baseline":
        if _mapping(state.get("baseline_tests")):
            return _one(SupervisorRoute.DEVELOP, BASELINE_READY_REASON)
        return _one(SupervisorRoute.FINALIZE, BASELINE_MISSING)
    if phase == "develop":
        if _mapping(state.get("patch")):
            return _one(SupervisorRoute.TEST, PATCH_REASON)
        return _one(SupervisorRoute.FINALIZE, PATCH_MISSING)
    if phase == "test":
        if _mapping(state.get("tests")):
            return _one(SupervisorRoute.REVIEW, TEST_REASON)
        return _one(SupervisorRoute.FINALIZE, TEST_MISSING)
    if phase == "review":
        return _after_review(state, publish_available=publish_available)
    if phase == "request_publish_approval":
        return _after_publish_approval(state)
    if phase == "publish":
        if _publication_opened(state):
            return _one(SupervisorRoute.FINALIZE, PUBLISHED_REASON)
        return _one(SupervisorRoute.ESCALATE, STAGE_FAILED)
    return _one(SupervisorRoute.ESCALATE, UNRECOGNIZED_REASON)


def apply_recommendation(
    decision: SupervisorDecision,
    recommended: SupervisorRoute,
) -> SupervisorDecision:
    """Keep the recommended route only when policy listed it."""
    if recommended == decision.route:
        return decision
    if recommended in decision.allowed:
        return SupervisorDecision(
            route=recommended,
            reason=_clip(f"{decision.reason} {MODEL_SELECTED}"),
            allowed=decision.allowed,
        )
    return SupervisorDecision(
        route=decision.route,
        reason=_clip(
            f"{decision.reason} The supervisor model recommended "
            f"{recommended.value}, {MODEL_REJECTED}"
        ),
        allowed=decision.allowed,
    )


def with_model(
    decision: SupervisorDecision,
    *,
    provider: LLMProvider,
    state: Mapping[str, Any],
) -> SupervisorDecision:
    """Ask for a recommendation. Invalid output cannot change the route."""
    try:
        recommended = provider.generate_structured(
            supervisor_prompt(state, decision.allowed),
            SupervisorRecommendation,
        )
    except LLMError:
        return _note_rejected(decision)
    except Exception:
        return _note_rejected(decision)
    if not isinstance(recommended, SupervisorRecommendation):
        return _note_rejected(decision)
    return apply_recommendation(decision, recommended.route)


def supervisor_prompt(state: Mapping[str, Any], allowed: frozenset[SupervisorRoute]) -> str:
    """The model sees the phase and the allowed names, not a diff or a file."""
    names = ", ".join(sorted(route.value for route in allowed))
    prompt = (
        "Choose one allowed route. You cannot approve work, write files, or change policy.\n"
        f"Phase: {_phase(state)}\n"
        f"Allowed: {names}\n"
        f"Title: {_short(state.get('title'), 200)}\n"
        f"Description:\n{_short(state.get('description'), 500)}\n"
    )
    return prompt[:MAX_PROMPT_CHARS]


def route_trace(state: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Copy the decisions stored on the graph. Drop anything that is not a route."""
    return tuple(route_records(state.get("routes")))


def route_records(value: object) -> tuple[dict[str, Any], ...]:
    """Return the valid decisions in order. Invalid items are omitted."""
    if not isinstance(value, (list, tuple)):
        return ()
    records: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        raw_route = item.get("route")
        if not isinstance(raw_route, str):
            continue
        try:
            route = SupervisorRoute(raw_route)
        except ValueError:
            continue
        reason = item.get("reason")
        phase = item.get("phase")
        if not isinstance(reason, str) or not reason.strip():
            continue
        records.append(
            {
                "route": route.value,
                "reason": _clip(reason),
                "phase": phase if isinstance(phase, str) else "",
            }
        )
    return tuple(records)


def _after_investigation(state: Mapping[str, Any]) -> SupervisorDecision:
    count = _count(state, "investigation_count")
    report = _investigation_report(state)
    if count is None or report is None:
        return _one(SupervisorRoute.FINALIZE, INVESTIGATION_MISSING)
    if _python_citations(report) == 0 and count < MAX_INVESTIGATIONS:
        reason = INVESTIGATE_MORE_REASON if not report.evidence else NO_PYTHON_REASON
        return _one(SupervisorRoute.INVESTIGATE_MORE, reason)
    return _one(SupervisorRoute.PLAN, PLAN_REASON)


def _after_plan(state: Mapping[str, Any]) -> SupervisorDecision:
    viewed = _plan_view(state)
    count = _count(state, "plan_count")
    if viewed is None or count is None:
        return _one(SupervisorRoute.FINALIZE, PLAN_MISSING)
    requests_changes, rejected, evidence = viewed
    if requests_changes:
        return _one(SupervisorRoute.PROCEED_TO_APPROVAL, PROCEED_REASON)
    if rejected > 0 and evidence > 0 and count < MAX_PLANS:
        return SupervisorDecision(
            route=SupervisorRoute.REPLAN,
            reason=REPLAN_REASON,
            allowed=frozenset({SupervisorRoute.REPLAN, SupervisorRoute.FINALIZE}),
        )
    return _one(SupervisorRoute.FINALIZE, DECLINE_REASON)


def _after_review(
    state: Mapping[str, Any],
    *,
    publish_available: bool = False,
) -> SupervisorDecision:
    count = _count(state, "repair_count")
    if (
        state.get("policy") == PolicyAction.REPAIR.value
        and state.get("status") == "running"
        and count is not None
        and 0 < count <= DEFAULT_MAX_REPAIRS
    ):
        return SupervisorDecision(
            route=SupervisorRoute.REPAIR,
            reason=REPAIR_REASON,
            allowed=frozenset({SupervisorRoute.REPAIR, SupervisorRoute.ESCALATE}),
        )
    if (
        state.get("policy") == PolicyAction.ACCEPT.value
        and state.get("status") == "completed"
        and not state.get("error")
    ):
        if publish_available:
            return _one(SupervisorRoute.REQUEST_PUBLISH_APPROVAL, PUBLISH_READY_REASON)
        return _one(SupervisorRoute.FINALIZE, ACCEPT_REASON)
    return _one(SupervisorRoute.ESCALATE, ESCALATE_REASON)


def _after_publish_approval(state: Mapping[str, Any]) -> SupervisorDecision:
    decision = _publication_decision(state)
    if decision == "approve" and state.get("status") == "running":
        return _one(SupervisorRoute.PUBLISH, PUBLICATION_APPROVED_REASON)
    if decision == "reject" and state.get("status") == "completed":
        return _one(SupervisorRoute.FINALIZE, PUBLICATION_DECLINED_REASON)
    return _one(SupervisorRoute.ESCALATE, STAGE_FAILED)


def _publication_decision(state: Mapping[str, Any]) -> str:
    publication = state.get("publication")
    if not isinstance(publication, dict):
        return ""
    decision = publication.get("decision")
    if decision not in {"approve", "reject"}:
        return ""
    if publication.get("kind") not in {None, "publish"}:
        return ""
    return str(decision)


def _publication_opened(state: Mapping[str, Any]) -> bool:
    publication = state.get("publication")
    if not isinstance(publication, dict) or publication.get("decision") != "approve":
        return False
    number = publication.get("number")
    return isinstance(number, int) and not isinstance(number, bool) and number > 0


def _past_deadline(
    state: Mapping[str, Any],
    *,
    now: float | None,
    deadline_seconds: float,
) -> bool:
    started = state.get("started_at")
    if isinstance(started, bool) or not isinstance(started, (int, float)):
        return False
    current = now
    if current is None:
        return False
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        return False
    return float(current) - float(started) > deadline_seconds


def _one(route: SupervisorRoute, reason: str) -> SupervisorDecision:
    return SupervisorDecision(route=route, reason=reason, allowed=frozenset({route}))


def _note_rejected(decision: SupervisorDecision) -> SupervisorDecision:
    return SupervisorDecision(
        route=decision.route,
        reason=_clip(f"{decision.reason} {MODEL_OUTPUT_REJECTED}"),
        allowed=decision.allowed,
    )


def _approved(state: Mapping[str, Any]) -> bool:
    approval = state.get("approval")
    return isinstance(approval, dict) and approval.get("decision") == "approve"


def _investigation_report(state: Mapping[str, Any]) -> InvestigationReport | None:
    raw = state.get("investigation")
    if not _mapping(raw):
        return None
    try:
        return InvestigationReport.model_validate(raw.get("report"))
    except ValidationError:
        return None


def _python_citations(report: InvestigationReport) -> int:
    """Citations of a `.py` file. Documentation and HTML do not let planning start."""
    return sum(1 for item in report.evidence if item.relative_path.casefold().endswith(".py"))


def _evidence_count(state: Mapping[str, Any]) -> int | None:
    report = _investigation_report(state)
    if report is None:
        return None
    return len(report.evidence)


def _plan_view(state: Mapping[str, Any]) -> tuple[bool, int, int] | None:
    raw = state.get("plan")
    if not _mapping(raw):
        return None
    try:
        plan = ChangePlan.model_validate(raw.get("plan"))
    except ValidationError:
        return None
    rejected = raw.get("rejected_paths")
    if isinstance(rejected, bool) or not isinstance(rejected, int) or rejected < 0:
        return None
    evidence = _evidence_count(state)
    if evidence is None:
        evidence = 0
    requests = plan.can_proceed and len(plan.modifications) > 0
    return requests, rejected, evidence


def _has_cycles(state: Mapping[str, Any]) -> bool:
    cycles = state.get("cycles")
    return isinstance(cycles, list) and len(cycles) > 0


def _phase(state: Mapping[str, Any]) -> str:
    phase = state.get("phase")
    if not isinstance(phase, str) or not phase.strip():
        return ""
    return phase


def _count(state: Mapping[str, Any], key: str) -> int | None:
    value = state.get(key)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _mapping(value: object) -> TypeGuard[Mapping[str, Any]]:
    return isinstance(value, dict)


def _error_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return _clip(value)


def _short(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _clip(value: str) -> str:
    text = " ".join(value.split())
    if not text:
        return STAGE_FAILED
    return text[:_REASON_CHARS]
