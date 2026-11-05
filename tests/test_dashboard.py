"""HTML dashboard. Writes still go through the task API."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from fastapi.testclient import TestClient
from tests.database import open_session_factory
from tests.test_task_boundaries import imported_modules
from tests.test_tasks_api import post_task

from patchpilot.api import create_app
from patchpilot.config import get_settings
from patchpilot.llm.investigation import EvidenceCitation, Hypothesis, InvestigationReport
from patchpilot.llm.meter import ModelCallDraft
from patchpilot.llm.patch import PatchProposal
from patchpilot.llm.plan import ChangePlan, ModificationKind, ProposedModification
from patchpilot.llm.review import FindingSeverity, ReviewAction, ReviewFinding, ReviewResult
from patchpilot.llm.triage import Classification, Severity, TriageResult
from patchpilot.repositories.artifacts import (
    persist_patch,
    persist_plan,
    persist_review,
    persist_test_report,
)
from patchpilot.repositories.model_calls import ModelCallRepository
from patchpilot.runner.protocol import TestReport as StoredTestReport
from patchpilot.runner.protocol import TestStatus as StoredTestStatus

ROOT = Path(__file__).resolve().parents[1]
CLOUD_KEY = "sk-test-secret-key-should-not-appear"
APPLIED_DIFF = """diff --git a/src/demo_service/discounts.py b/src/demo_service/discounts.py
--- a/src/demo_service/discounts.py
+++ b/src/demo_service/discounts.py
@@ -1 +1 @@
-old
+APPLIED-FIX-MARKER
"""


def test_dashboard_modules_do_not_call_a_model():
    source = ""
    modules: set[str] = set()
    for relative in (
        "src/patchpilot/web/routes.py",
        "src/patchpilot/web/load.py",
        "src/patchpilot/web/present.py",
        "src/patchpilot/api.py",
    ):
        modules.update(imported_modules(relative))
        source += (ROOT / relative).read_text(encoding="utf-8")

    assert "generate_structured" not in source
    assert "BackgroundTasks" not in source
    for name in modules:
        assert "openai_compatible" not in name
        assert not name.endswith(".ollama")
        assert not name.endswith(".factory")


def test_readme_explains_the_live_dashboard():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "http://127.0.0.1:8000/" in readme
    assert "POST /api/v1/tasks" in readme
    assert "/decisions" in readme
    assert "FakeProvider" in readme or "fake provider" in readme.casefold()
    assert "Dashboard" in example


def client(postgres_database_url: str, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_database_url)
    get_settings.cache_clear()
    return TestClient(create_app())


def test_index_lists_a_task_and_serves_the_dashboard(
    postgres_database_url: str,
    clean_tasks: None,
    monkeypatch,
):
    with client(postgres_database_url, monkeypatch) as test_client:
        empty = test_client.get("/")
        created = post_task(test_client, title="Broken discount", description="Unknown tier.")
        listed = test_client.get("/")
        script = test_client.get("/static/dashboard.js")
        style = test_client.get("/static/dashboard.css")
        rejected = test_client.post("/", json={"title": "no", "description": "no"})

    assert empty.status_code == 200
    assert "text/html" in empty.headers["content-type"]
    assert 'data-create-url="/api/v1/tasks"' in empty.text
    assert 'name="title"' in empty.text
    assert 'name="description"' in empty.text
    assert 'name="repo_id"' in empty.text
    assert "No tasks yet." in empty.text
    assert "width=device-width" in empty.text
    assert "no-store" in empty.headers["cache-control"]
    assert "script-src 'self'" in empty.headers["content-security-policy"]
    assert listed.status_code == 200
    assert "Broken discount" in listed.text
    assert "Queued" in listed.text
    assert f"/tasks/{created['id']}" in listed.text
    assert "No tasks yet." not in listed.text
    assert script.status_code == 200
    assert "data-event-id" in script.text
    assert "CSS.escape" in script.text
    assert "data-create-url" in script.text
    assert "data-decision-url" in script.text
    assert 'method: "POST"' in script.text
    assert "password" not in script.text.casefold()
    assert "@media" in style.text
    assert "max-width" in style.text
    assert rejected.status_code == 405


def test_index_lists_only_enabled_repositories(
    postgres_database_url: str,
    clean_tasks: None,
    monkeypatch,
):
    enabled = uuid4()
    hidden = uuid4()
    with psycopg.connect(postgres_database_url, autocommit=True) as connection:
        connection.execute(
            """
            INSERT INTO repositories (id, display_name, canonical_path, enabled, registration)
            VALUES (%s, 'Demo service', 'demo_service', true, '{}'::jsonb),
                   (%s, 'Hidden checkout', 'hidden_checkout', false, '{}'::jsonb)
            """,
            (enabled, hidden),
        )
    with client(postgres_database_url, monkeypatch) as test_client:
        page = test_client.get("/")

    assert f'value="{enabled}"' in page.text
    assert "Demo service" in page.text
    assert "Hidden checkout" not in page.text
    assert "hidden_checkout" not in page.text
    assert "demo_service" not in page.text


def test_task_page_escapes_markup_and_hides_configured_secrets(
    postgres_database_url: str,
    clean_tasks: None,
    monkeypatch,
):
    monkeypatch.setenv("LLM_CLOUD_API_KEY", CLOUD_KEY)
    description = f"See postgresql://patchpilot:patchpilot_dev@db:5432/patchpilot and {CLOUD_KEY}."
    with client(postgres_database_url, monkeypatch) as test_client:
        created = post_task(
            test_client,
            title='<script>alert("xss")</script>',
            description=description,
        )
        _insert_event(
            postgres_database_url,
            created["id"],
            "route",
            "supervisor",
            {
                "api_key": "sk-live-secretvalue",
                "note": f"<img src=x onerror=alert(1)> {CLOUD_KEY}",
                "reason": "Rules chose finalize.",
            },
            datetime(2026, 11, 5, 12, 0, tzinfo=UTC),
        )
        get_settings.cache_clear()
        page = test_client.get(f"/tasks/{created['id']}")

    assert page.status_code == 200
    assert "<script>" not in page.text
    assert "&lt;script&gt;" in page.text
    assert "<img" not in page.text
    assert "&lt;img" in page.text
    assert "patchpilot_dev" not in page.text
    assert CLOUD_KEY not in page.text
    assert "sk-live-secretvalue" not in page.text
    assert "[redacted]" in page.text
    assert "Rules chose finalize." in page.text
    assert f'data-progress-url="/tasks/{created["id"]}/progress"' in page.text


def test_progress_returns_each_event_once(
    postgres_database_url: str,
    clean_tasks: None,
    monkeypatch,
):
    with client(postgres_database_url, monkeypatch) as test_client:
        created = post_task(test_client, title="Timeline", description="Two events.")
        first = _insert_event(
            postgres_database_url,
            created["id"],
            "stage_started",
            "triage",
            {"reason": "Triage started."},
            datetime(2026, 11, 5, 12, 0, tzinfo=UTC),
        )
        second = _insert_event(
            postgres_database_url,
            created["id"],
            "stage_completed",
            "triage",
            {"reason": "Triage finished."},
            datetime(2026, 11, 5, 12, 0, 1, tzinfo=UTC),
        )
        page = test_client.get(f"/tasks/{created['id']}")
        after_first = test_client.get(
            f"/tasks/{created['id']}/progress",
            params={"after": str(first)},
        )
        after_second = test_client.get(
            f"/tasks/{created['id']}/progress",
            params={"after": str(second)},
        )
        missing = test_client.get(f"/tasks/{uuid4()}")
        missing_progress = test_client.get(f"/tasks/{uuid4()}/progress")

    assert page.text.count(f'data-event-id="{first}"') == 1
    assert page.text.count(f'data-event-id="{second}"') == 1
    assert f'data-event-id="{first}"' not in after_first.text
    assert f'data-event-id="{second}"' in after_first.text
    assert "data-event-id" not in after_second.text
    assert "Triage finished." in after_first.text
    assert missing.status_code == 404
    assert "text/html" in missing.headers["content-type"]
    assert "Task not found" in missing.text
    assert missing_progress.status_code == 404


def test_waiting_approval_posts_to_the_decisions_api(
    postgres_database_url: str,
    clean_tasks: None,
    monkeypatch,
):
    with client(postgres_database_url, monkeypatch) as test_client:
        created = post_task(test_client, title="Needs approval", description="A plan is ready.")
        approval_id = uuid4()
        with psycopg.connect(postgres_database_url, autocommit=True) as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = 'waiting_approval'::task_status,
                    stage = 'request_plan_approval',
                    result_summary = 'Map an unknown tier to HTTP 422.',
                    result_json = %s::jsonb
                WHERE id = %s
                """,
                (
                    json.dumps(
                        {
                            "plan": {
                                "can_proceed": True,
                                "confidence": 0.8,
                                "paths": ["src/demo_service/discounts.py"],
                            }
                        }
                    ),
                    created["id"],
                ),
            )
            connection.execute(
                """
                INSERT INTO approvals (id, task_id, status, plan_version)
                VALUES (%s, %s, 'pending'::approval_status, 1)
                """,
                (approval_id, created["id"]),
            )
        page = test_client.get(f"/tasks/{created['id']}")
        page_write = test_client.post(
            f"/tasks/{created['id']}",
            json={"approval_id": str(approval_id), "decision": "approve"},
        )
        missing_write = test_client.post(
            f"/tasks/{created['id']}/decisions",
            json={"approval_id": str(approval_id), "decision": "approve"},
        )

    assert page.status_code == 200
    assert "Waiting for approval" in page.text
    assert "src/demo_service/discounts.py" in page.text
    assert f'data-decision-url="/api/v1/tasks/{created["id"]}/decisions"' in page.text
    assert f'data-approval-id="{approval_id}"' in page.text
    assert 'data-decision="approve"' in page.text
    assert 'data-decision="reject"' in page.text
    assert "No approval request yet." not in page.text
    assert page_write.status_code == 405
    assert missing_write.status_code == 404


def test_finished_task_shows_evidence_diff_tests_review_and_usage(
    postgres_database_url: str,
    clean_tasks: None,
    monkeypatch,
    tmp_path: Path,
):
    root = tmp_path / "artifacts"
    root.mkdir()
    with client(postgres_database_url, monkeypatch) as test_client:
        created = post_task(test_client, title="Discount crash", description="Unknown tier.")
        task_id = UUID(str(created["id"]))
        _store_finished(postgres_database_url, task_id, root)
        _store_result(postgres_database_url, task_id)
        monkeypatch.setenv("ARTIFACTS_ROOT", str(root))
        get_settings.cache_clear()
        page = test_client.get(f"/tasks/{task_id}")

    assert page.status_code == 200
    assert page.text.count("src/demo_service/discounts.py:12") == 1
    assert "KNOWN_TIERS has no fallback." in page.text
    assert "Unknown tier raises KeyError." in page.text
    assert "APPLIED-FIX-MARKER" in page.text
    assert "PROPOSAL-ONLY-MARKER" not in page.text
    assert "diff --git" in page.text
    diff_checksum = _attr(page.text, "data-diff-checksum")
    proposal_checksum = _attr(page.text, "data-proposal-checksum")
    assert diff_checksum != proposal_checksum
    assert "1 passed" in page.text
    assert "supersecretpass" not in page.text
    assert "The patch matches the plan." in page.text
    assert 'data-cost="unknown"' in page.text
    assert "triage" in page.text
    assert "success" in page.text
    assert str(root) not in page.text
    assert "v1.patch" not in page.text
    assert f"/api/v1/tasks/{task_id}/patch" in page.text
    assert f"/api/v1/tasks/{task_id}/usage" in page.text


def _insert_event(
    database_url: str,
    task_id: object,
    event_type: str,
    stage: str,
    payload: dict[str, object],
    timestamp: datetime,
) -> UUID:
    event_id = uuid4()
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(
            """
            INSERT INTO events (
                id, task_id, timestamp, stage, event_type, payload, idempotency_key
            )
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                event_id,
                task_id,
                timestamp,
                stage,
                event_type,
                json.dumps(payload),
                f"{task_id}:{event_type}:{event_id}",
            ),
        )
    return event_id


def _store_result(database_url: str, task_id: UUID) -> None:
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
            )
        ],
        probable_files=["src/demo_service/discounts.py"],
        unanswered_questions=["Should the message name the tier?"],
    )
    triage = TriageResult(
        classification=Classification.BUG,
        severity=Severity.HIGH,
        summary="Unknown tier returns HTTP 500.",
        acceptance_criteria=["Unknown tier returns HTTP 422."],
        confidence=0.9,
        needs_clarification=False,
    )
    body = {
        "triage": triage.model_dump(mode="json"),
        "investigation": {
            "report": report.model_dump(mode="json"),
            "tool_calls": 2,
            "rejected_citations": 0,
        },
    }
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(
            """
            UPDATE tasks
            SET status = 'completed'::task_status,
                stage = 'finalize',
                result_summary = 'Raise UnknownTierError for an unknown tier.',
                result_json = %s::jsonb
            WHERE id = %s
            """,
            (json.dumps(body), task_id),
        )


def _store_finished(database_url: str, task_id: UUID, root: Path) -> None:
    plan = ChangePlan(
        problem_statement="Unknown tier raises KeyError.",
        assumptions=["The checkout is the demo service."],
        modifications=[
            ProposedModification(
                relative_path="src/demo_service/discounts.py",
                kind=ModificationKind.EXISTING,
                summary="Raise a validation error for an unknown tier.",
            )
        ],
        test_strategy="Add a regression test for an unknown tier.",
        risks=["A known tier could be rejected."],
        confidence=0.8,
        can_proceed=True,
    )
    proposal = PatchProposal(
        summary="Raise UnknownTierError for an unknown tier.",
        unified_diff="PROPOSAL-ONLY-MARKER\n",
    )
    report = StoredTestReport(
        job_id=uuid4(),
        status=StoredTestStatus.PASSED,
        exit_code=0,
        timed_out=False,
        duration_ms=15,
        stdout="1 passed\npostgresql://user:supersecretpass@db/patchpilot\n",
        stderr="",
    )
    review = ReviewResult(
        summary="The patch matches the plan.",
        action=ReviewAction.ACCEPT,
        findings=[
            ReviewFinding(
                summary="The new test covers the unknown tier.",
                severity=FindingSeverity.LOW,
                evidence="tests/test_unknown_tier_added.py",
            )
        ],
        evidence=["src/demo_service/discounts.py"],
    )

    async def _run() -> None:
        engine, factory = open_session_factory(database_url)
        try:
            async with factory() as session:
                await persist_plan(session, root, task_id, plan)
                await persist_patch(session, root, task_id, proposal, APPLIED_DIFF.encode())
                await persist_test_report(session, root, task_id, report)
                await persist_review(
                    session,
                    root,
                    task_id,
                    review,
                    policy="accept",
                    repair_count=0,
                )
                await session.commit()
            async with factory() as session:
                await ModelCallRepository(session).insert_many(
                    [
                        ModelCallDraft(
                            task_id=task_id,
                            stage="triage",
                            provider="fake",
                            model="gemma4:12b",
                            prompt_tokens=None,
                            output_tokens=None,
                            latency_ms=12,
                            estimated_cost_usd=None,
                            cost_known=False,
                            outcome="success",
                        )
                    ]
                )
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _attr(text: str, name: str) -> str:
    marker = f'{name}="'
    start = text.index(marker) + len(marker)
    end = text.index('"', start)
    return text[start:end]
