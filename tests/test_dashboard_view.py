"""Dashboard display rules that do not need PostgreSQL."""

from datetime import UTC, datetime
from types import SimpleNamespace

from patchpilot.llm.investigation import EvidenceCitation, Hypothesis, InvestigationReport
from patchpilot.web.present import (
    EventView,
    citation_labels,
    list_href,
    select_new_events,
    stage_class,
)
from patchpilot.web.present import (
    _event as present_event,
)
from patchpilot.web.redact import redact_text


def _event(event_id: str) -> EventView:
    return EventView(event_id, "2026-11-05T12:00:00+00:00", "time", "Triage", "Route", ())


def test_redact_text_hides_connection_strings_tokens_and_long_markers():
    raw = (
        "postgresql://patchpilot:patchpilot_dev@db:5432/patchpilot "
        "Bearer super-secret-token sk-test-secret-key"
    )

    cleaned = redact_text(raw, ("super-secret-token",))

    assert "patchpilot_dev" not in cleaned
    assert "super-secret-token" not in cleaned
    assert "sk-test-secret-key" not in cleaned
    assert "postgresql://patchpilot:[redacted]@db:5432/patchpilot" in cleaned
    assert redact_text("pass word", ("pass",)) == "pass word"


def test_citations_are_unique_and_invalid_reports_are_omitted():
    report = InvestigationReport(
        summary="The lookup raises KeyError.",
        hypotheses=[
            Hypothesis(
                statement="KNOWN_TIERS has no fallback.",
                citations=[
                    EvidenceCitation(
                        relative_path="src/demo_service/discounts.py",
                        start_line=12,
                        end_line=12,
                    )
                ],
            )
        ],
        evidence=[
            EvidenceCitation(
                relative_path="src/demo_service/discounts.py",
                start_line=12,
                end_line=12,
            ),
            EvidenceCitation(
                relative_path="src/demo_service/app.py",
                start_line=4,
                end_line=6,
            ),
        ],
        probable_files=["src/demo_service/discounts.py"],
        unanswered_questions=[],
    )
    result = {"investigation": {"report": report.model_dump(mode="json")}}

    assert citation_labels(result) == (
        "src/demo_service/discounts.py:12",
        "src/demo_service/app.py:4-6",
    )
    assert citation_labels({"investigation": {"report": {"summary": ""}}}) == ()
    assert citation_labels(None) == ()


def _row(event_type: str, stage: str, payload: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        id="evt",
        timestamp=datetime(2026, 11, 9, 8, 29, 45, tzinfo=UTC),
        stage=stage,
        event_type=event_type,
        payload=payload,
    )


def _tones(event: EventView) -> dict[str, tuple[str, str]]:
    return {detail.key: (detail.value, detail.tone) for detail in event.details}


def _shown(event_type: str, stage: str, payload: dict[str, object]) -> EventView:
    return present_event(_row(event_type, stage, payload), ())


def test_timeline_cards_color_each_stage_and_mark_decisions():
    declined = _shown("stage_completed", "plan", {"attempt": 1, "can_proceed": False})
    proceeded = _shown("stage_completed", "plan", {"can_proceed": True})
    approved = _shown(
        "approval_decided", "request_plan_approval", {"decision": "approve", "kind": "plan"}
    )
    rejected = _shown("approval_decided", "request_publish_approval", {"decision": "reject"})
    failure = _shown(
        "model_error",
        "triage",
        {"attempt": 1, "error_code": "model_error", "message": "The model failed."},
    )
    exhausted = _shown("recovery", "develop", {"attempt": 3, "action": "exhausted"})
    route = _shown(
        "route",
        "supervisor",
        {
            "attempt": 1,
            "index": 2,
            "phase": "investigate",
            "route": "plan",
            "reason": "The investigation is finished.",
        },
    )
    tests = _shown(
        "stage_completed",
        "test",
        {"passed": False, "status": "failed", "timed_out": True, "policy": "fail"},
    )

    assert declined.stage_class == "stage-plan"
    assert declined.tone == ""
    assert _tones(declined)["Can proceed"] == ("no", "bad")
    assert _tones(proceeded)["Can proceed"] == ("yes", "good")
    assert _tones(declined)["Attempt"] == ("1", "")
    assert approved.stage_class == "stage-request-plan-approval"
    assert approved.tone == ""
    assert _tones(approved)["Decision"] == ("approve", "good")
    assert _tones(rejected)["Decision"] == ("reject", "bad")
    assert failure.stage_class == "stage-triage"
    assert failure.tone == "bad"
    assert _tones(failure)["Error code"] == ("model_error", "bad")
    assert _tones(failure)["Message"] == ("The model failed.", "")
    assert exhausted.tone == "bad"
    assert _tones(exhausted)["Action"] == ("exhausted", "bad")
    assert route.stage_class == "stage-supervisor"
    assert route.tone == ""
    assert all(tone == "" for _, tone in _tones(route).values())
    assert _tones(tests)["Passed"] == ("no", "bad")
    assert _tones(tests)["Status"] == ("failed", "bad")
    assert _tones(tests)["Timed out"] == ("yes", "bad")
    assert _tones(tests)["Policy"] == ("fail", "bad")
    assert stage_class("not-a-stage") == "stage-other"
    assert stage_class(None) == "stage-other"
    assert stage_class("investigate") == "stage-investigate"


def test_progress_cursor_drops_events_that_were_already_shown():
    first, second = _event("one"), _event("two")

    assert select_new_events((first, second), None) == (first, second)
    assert select_new_events((first, second), "one") == (second,)
    assert select_new_events((first, second), "two") == ()
    assert select_new_events((first, second), "missing") == (first, second)
    assert list_href(status="queued", offset=20) == "/?status=queued&offset=20"
    assert list_href() == "/"
