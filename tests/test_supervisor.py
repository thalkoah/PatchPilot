"""Supervisor routing. Rules choose the node. A model cannot widen that choice."""

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from patchpilot.agents.limits import (
    GRAPH_RECURSION_LIMIT,
    MAX_SUPERVISOR_STEPS,
    require_supervisor_deadline,
)
from patchpilot.agents.review_policy import PolicyAction
from patchpilot.agents.supervisor import (
    ABSENT_REPO,
    ACCEPT_REASON,
    DEADLINE_REASON,
    DECLINE_REASON,
    INVESTIGATE_MORE_REASON,
    INVESTIGATE_REASON,
    MODEL_OUTPUT_REJECTED,
    MODEL_REJECTED,
    MODEL_SELECTED,
    PLAN_REASON,
    PROCEED_REASON,
    REJECTED_REASON,
    REPAIR_REASON,
    REPLAN_REASON,
    STEP_LIMIT_REASON,
    apply_recommendation,
    decide_route,
    with_model,
)
from patchpilot.db.models import TaskStatus
from patchpilot.llm.errors import LLMResponseError
from patchpilot.llm.fake import FakeProvider
from patchpilot.llm.investigation import EvidenceCitation, InvestigationReport
from patchpilot.llm.plan import ChangePlan, ModificationKind, ProposedModification
from patchpilot.llm.supervisor import SupervisorRecommendation, SupervisorRoute
from patchpilot.source.tools import RepositoryTools
from patchpilot.workflow.graph import (
    compile_triage_graph,
    route_from_supervisor,
    run_triage,
    thread_config,
)

ROOT = Path(__file__).resolve().parents[1]
DEMO_CHECKOUT = ROOT / "examples" / "demo_service"
DEMO_TITLE = "Unknown customer tier crashes discount endpoint"


def test_routes_cover_the_outcomes_the_graph_must_distinguish():
    absent = decide_route({"phase": "triage", "status": "completed", "triage": {"ok": True}})
    investigate = decide_route(
        {"phase": "triage", "status": "completed", "repo_id": "repo", "triage": {"ok": True}}
    )
    more = decide_route(
        {
            "phase": "investigate",
            "status": "completed",
            "investigation_count": 1,
            "investigation": _investigation(0),
        }
    )
    plan = decide_route(
        {
            "phase": "investigate",
            "status": "completed",
            "investigation_count": 2,
            "investigation": _investigation(0),
        }
    )
    replan = decide_route(
        {
            "phase": "plan",
            "status": "completed",
            "plan_count": 1,
            "plan": _plan(proceed=False, rejected=2),
            "investigation": _investigation(1),
        }
    )
    proceed = decide_route(
        {"phase": "plan", "status": "completed", "plan_count": 1, "plan": _plan(proceed=True)}
    )
    rejected = decide_route({"status": "rejected", "phase": "request_plan_approval"})
    repair = decide_route(
        {
            "phase": "review",
            "status": "running",
            "policy": PolicyAction.REPAIR.value,
            "repair_count": 1,
        }
    )
    escalate = decide_route(
        {
            "phase": "review",
            "status": "failed",
            "error": "The reviewer asked for a person to decide.",
            "policy": PolicyAction.FAIL.value,
        }
    )
    finalize = decide_route(
        {
            "phase": "review",
            "status": "completed",
            "policy": PolicyAction.ACCEPT.value,
        }
    )

    assert absent.route is SupervisorRoute.FINALIZE
    assert ABSENT_REPO in absent.reason
    assert investigate.route is SupervisorRoute.INVESTIGATE
    assert investigate.reason == INVESTIGATE_REASON
    assert more.route is SupervisorRoute.INVESTIGATE_MORE
    assert more.reason == INVESTIGATE_MORE_REASON
    assert plan.route is SupervisorRoute.PLAN
    assert plan.reason == PLAN_REASON
    assert replan.route is SupervisorRoute.REPLAN
    assert replan.reason == REPLAN_REASON
    assert SupervisorRoute.FINALIZE in replan.allowed
    assert proceed.route is SupervisorRoute.PROCEED_TO_APPROVAL
    assert proceed.reason == PROCEED_REASON
    assert rejected.route is SupervisorRoute.FINALIZE
    assert rejected.reason == REJECTED_REASON
    assert repair.route is SupervisorRoute.REPAIR
    assert repair.reason == REPAIR_REASON
    assert SupervisorRoute.ESCALATE in repair.allowed
    assert escalate.route is SupervisorRoute.ESCALATE
    assert "person" in escalate.reason
    assert finalize.route is SupervisorRoute.FINALIZE
    assert finalize.reason == ACCEPT_REASON


def test_a_declined_plan_finalizes_and_a_second_invalid_plan_does_too():
    declined = decide_route(
        {
            "phase": "plan",
            "status": "completed",
            "plan_count": 1,
            "plan": _plan(proceed=False, rejected=0),
            "investigation": _investigation(1),
        }
    )
    exhausted = decide_route(
        {
            "phase": "plan",
            "status": "completed",
            "plan_count": 2,
            "plan": _plan(proceed=False, rejected=2),
            "investigation": _investigation(1),
        }
    )

    assert declined.route is SupervisorRoute.FINALIZE
    assert declined.reason == DECLINE_REASON
    assert exhausted.route is SupervisorRoute.FINALIZE
    assert exhausted.reason == DECLINE_REASON


def test_step_and_deadline_limits_escalate_and_cannot_be_raised():
    limited = decide_route(
        {"supervisor_steps": MAX_SUPERVISOR_STEPS, "phase": "triage", "repo_id": "repo"}
    )
    expired = decide_route(
        {
            "supervisor_steps": 1,
            "phase": "triage",
            "status": "completed",
            "repo_id": "repo",
            "triage": {"ok": True},
            "started_at": 0,
        },
        now=10_000,
        deadline_seconds=1,
    )

    assert limited.route is SupervisorRoute.ESCALATE
    assert limited.reason == STEP_LIMIT_REASON
    assert limited.allowed == frozenset({SupervisorRoute.ESCALATE})
    assert expired.route is SupervisorRoute.ESCALATE
    assert expired.reason == DEADLINE_REASON
    assert GRAPH_RECURSION_LIMIT == 40
    assert MAX_SUPERVISOR_STEPS == 24
    assert GRAPH_RECURSION_LIMIT > MAX_SUPERVISOR_STEPS
    with pytest.raises(ValueError):
        require_supervisor_deadline(3601)
    with pytest.raises(ValidationError):
        SupervisorRecommendation(route=SupervisorRoute.FINALIZE, reason="bad\nline")


def test_a_model_recommendation_cannot_leave_the_allowed_set():
    rules = decide_route({"phase": "triage", "status": "completed"})
    rejected = apply_recommendation(rules, SupervisorRoute.PROCEED_TO_APPROVAL)
    choice = decide_route(
        {
            "phase": "plan",
            "status": "completed",
            "plan_count": 1,
            "plan": _plan(proceed=False, rejected=2),
            "investigation": _investigation(1),
        }
    )
    accepted = apply_recommendation(choice, SupervisorRoute.FINALIZE)
    same = apply_recommendation(choice, SupervisorRoute.REPLAN)

    assert rejected.route is SupervisorRoute.FINALIZE
    assert "proceed_to_approval" in rejected.reason
    assert MODEL_REJECTED in rejected.reason
    assert "model recommendation" not in rejected.reason
    assert accepted.route is SupervisorRoute.FINALIZE
    assert MODEL_SELECTED in accepted.reason
    assert same is choice


def test_invalid_model_output_keeps_the_rule():
    rules = decide_route(
        {"phase": "triage", "status": "completed", "repo_id": "repo", "triage": {"ok": True}}
    )

    kept = with_model(rules, provider=_RejectingSupervisor(), state={"phase": "triage"})

    assert kept.route is SupervisorRoute.INVESTIGATE
    assert MODEL_OUTPUT_REJECTED in kept.reason
    assert "secret" not in kept.reason


def test_route_names_map_to_existing_nodes():
    assert route_from_supervisor({"route": "repair"}) == "develop"
    assert route_from_supervisor({"route": "investigate_more"}) == "investigate"
    assert route_from_supervisor({"route": "replan"}) == "plan"
    assert route_from_supervisor({"route": "escalate"}) == "finalize"
    assert route_from_supervisor({"route": "not-a-route"}) == "finalize"
    assert thread_config(uuid4())["recursion_limit"] == GRAPH_RECURSION_LIMIT


def test_a_resumed_develop_node_still_routes_to_tests():
    """An older checkpoint has no supervisor counters. The finished node still continues."""
    decision = decide_route(
        {"phase": "develop", "status": "completed", "patch": {"summary": "kept"}}
    )

    assert decision.route is SupervisorRoute.TEST


async def test_absent_repository_finalizes_without_a_model_call():
    provider = FakeProvider()

    outcome = await run_triage(
        compile_triage_graph(),
        task_id=uuid4(),
        title=DEMO_TITLE,
        description="Unknown tier returns HTTP 500.",
        provider=provider,
    )

    assert outcome.status == TaskStatus.COMPLETED
    assert [item["route"] for item in outcome.routes] == ["finalize"]
    assert "repository" in outcome.routes[0]["reason"]
    assert [call.schema_name for call in provider.calls] == ["TriageResult"]


async def test_missing_evidence_investigates_once_more_then_plans():
    provider = FakeProvider()

    outcome = await run_triage(
        compile_triage_graph(),
        task_id=uuid4(),
        title=DEMO_TITLE,
        description="This report has missing evidence for the unknown tier failure.",
        provider=provider,
        repo_id=uuid4(),
        repository_tools=RepositoryTools(DEMO_CHECKOUT),
    )

    assert outcome.status == TaskStatus.COMPLETED
    assert [item["route"] for item in outcome.routes] == [
        "investigate",
        "investigate_more",
        "plan",
        "finalize",
    ]
    assert outcome.plan is not None
    assert outcome.plan.can_proceed is False
    assert [call.schema_name for call in provider.calls].count("ChangePlan") == 1
    assert "SupervisorRecommendation" not in [call.schema_name for call in provider.calls]


async def test_an_invalid_plan_is_replanned_once():
    provider = FakeProvider()

    outcome = await run_triage(
        compile_triage_graph(),
        task_id=uuid4(),
        title=DEMO_TITLE,
        description="Use an unsafe path only and do not touch the checkout.",
        provider=provider,
        repo_id=uuid4(),
        repository_tools=RepositoryTools(DEMO_CHECKOUT),
    )

    assert outcome.status == TaskStatus.COMPLETED
    assert [item["route"] for item in outcome.routes] == [
        "investigate",
        "plan",
        "replan",
        "finalize",
    ]
    assert outcome.plan is not None
    assert outcome.plan.can_proceed is False
    assert [call.schema_name for call in provider.calls].count("ChangePlan") == 2


async def test_supervisor_model_cannot_approve_or_override_a_decline():
    blocked = FakeProvider()
    stopped = FakeProvider()

    no_repo = await run_triage(
        compile_triage_graph(),
        task_id=uuid4(),
        title="Save fails",
        description="supervisor recommend approval",
        provider=blocked,
        supervisor_model=True,
    )
    invalid = await run_triage(
        compile_triage_graph(),
        task_id=uuid4(),
        title=DEMO_TITLE,
        description="Use an unsafe path only. supervisor recommend finalize",
        provider=stopped,
        repo_id=uuid4(),
        repository_tools=RepositoryTools(DEMO_CHECKOUT),
        supervisor_model=True,
    )

    assert no_repo.status == TaskStatus.COMPLETED
    assert no_repo.routes[0]["route"] == "finalize"
    assert "proceed_to_approval" in no_repo.routes[0]["reason"]
    assert MODEL_REJECTED in no_repo.routes[0]["reason"]
    assert "model recommendation" not in no_repo.routes[0]["reason"]
    assert invalid.plan is not None
    assert invalid.plan.can_proceed is False
    assert [item["route"] for item in invalid.routes] == ["investigate", "plan", "finalize"]
    assert MODEL_SELECTED in invalid.routes[-1]["reason"]
    assert [call.schema_name for call in stopped.calls].count("ChangePlan") == 1
    assert "model recommendation" not in " ".join(item["reason"] for item in invalid.routes)


class _RejectingSupervisor:
    def generate_structured(self, prompt: str, schema: type, options: object = None) -> object:
        raise LLMResponseError("secret model text")

    def close(self) -> None:
        return None


def _investigation(citations: int) -> dict[str, object]:
    evidence = []
    if citations:
        evidence.append(
            EvidenceCitation(
                relative_path="src/demo_service/discounts.py",
                start_line=1,
                end_line=1,
            )
        )
    report = InvestigationReport(
        summary="The lookup fails.",
        hypotheses=[],
        evidence=evidence,
        probable_files=[],
        unanswered_questions=[] if citations else ["Which line raises the failure?"],
    )
    return {"report": report.model_dump(mode="json"), "tool_calls": 1, "rejected_citations": 0}


def _plan(*, proceed: bool, rejected: int = 0) -> dict[str, object]:
    modifications = []
    if proceed:
        modifications.append(
            ProposedModification(
                relative_path="src/demo_service/discounts.py",
                kind=ModificationKind.EXISTING,
                summary="Reject an unknown tier.",
            )
        )
    plan = ChangePlan(
        problem_statement="The lookup fails.",
        assumptions=[],
        modifications=modifications,
        test_strategy="Run pytest.",
        risks=[],
        confidence=0.5,
        can_proceed=proceed,
    )
    return {"plan": plan.model_dump(mode="json"), "rejected_paths": rejected}
