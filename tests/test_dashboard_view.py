"""Dashboard display rules that do not need PostgreSQL."""

from patchpilot.llm.investigation import EvidenceCitation, Hypothesis, InvestigationReport
from patchpilot.web.present import EventView, citation_labels, list_href, select_new_events
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


def test_progress_cursor_drops_events_that_were_already_shown():
    first, second = _event("one"), _event("two")

    assert select_new_events((first, second), None) == (first, second)
    assert select_new_events((first, second), "one") == (second,)
    assert select_new_events((first, second), "two") == ()
    assert select_new_events((first, second), "missing") == (first, second)
    assert list_href(status="queued", offset=20) == "/?status=queued&offset=20"
    assert list_href() == "/"
