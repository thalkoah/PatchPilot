"""Triage graph and the worker path that persists its result."""

import json
import time
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.database import open_session_factory

from patchpilot.api import create_app
from patchpilot.config import get_settings
from patchpilot.db.models import TaskStatus
from patchpilot.llm.errors import LLMResponseError
from patchpilot.llm.fake import DISCOUNT_TIER_TRIAGE, FakeProvider
from patchpilot.llm.triage import TriageResult
from patchpilot.repositories.tasks import TaskRepository
from patchpilot.worker import WORKER_CONCURRENCY, process_one
from patchpilot.workflow.graph import (
    GENERIC_FAILURE,
    compile_triage_graph,
    run_triage,
)

SECRET = "raw-model-secret-token"
DEMO_TITLE = "Unknown customer tier crashes discount endpoint"
DEMO_DESCRIPTION = (
    "When the demo-service discount endpoint receives a customer tier it does not "
    "recognize, it returns HTTP 500."
)


class ExplodingProvider:
    """Raise an unexpected error whose text must not be stored."""

    def __init__(self) -> None:
        self.calls = 0

    def generate_structured(self, prompt: str, schema: type, options: object = None) -> object:
        self.calls += 1
        raise RuntimeError(f"parser blew up on {SECRET}")

    def close(self) -> None:
        return None


class RejectingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def generate_structured(self, prompt: str, schema: type, options: object = None) -> object:
        self.calls += 1
        raise LLMResponseError("TriageResult rejected model output")

    def close(self) -> None:
        return None


class LockProbe(FakeProvider):
    """While the model call is in progress, the task row must not be locked."""

    def __init__(self, database_url: str, task_id: object) -> None:
        super().__init__()
        self.database_url = database_url
        self.task_id = task_id
        self.saw_running_without_a_lock = False

    def generate_structured(self, prompt: str, schema: type, options: object = None):
        with psycopg.connect(self.database_url) as connection:
            connection.execute("SET lock_timeout = '2s'")
            row = connection.execute(
                "SELECT status::text FROM tasks WHERE id = %s FOR UPDATE",
                (self.task_id,),
            ).fetchone()
            idle = connection.execute(
                """
                SELECT count(*)
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND state = 'idle in transaction'
                  AND pid <> pg_backend_pid()
                """
            ).fetchone()
            connection.rollback()
        assert row is not None
        assert row[0] == "running"
        assert idle is not None
        assert idle[0] == 0
        self.saw_running_without_a_lock = True
        return super().generate_structured(prompt, schema, options)


@pytest.fixture
def client(postgres_database_url: str, clean_tasks: None, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", postgres_database_url)
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client


def test_triage_graph_is_a_straight_line_without_a_checkpointer():
    graph = compile_triage_graph()

    assert graph.checkpointer is False
    edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}
    assert edges == {
        ("__start__", "triage"),
        ("triage", "finalize"),
        ("finalize", "__end__"),
    }


def test_fake_provider_triage_completes_without_a_database():
    provider = FakeProvider()
    title = DEMO_TITLE
    description = "Unknown tier returns HTTP 500."

    outcome = run_triage(
        compile_triage_graph(),
        task_id=uuid4(),
        title=title,
        description=description,
        provider=provider,
    )

    assert outcome.status == TaskStatus.COMPLETED
    assert outcome.stage == "finalize"
    assert outcome.result_summary == DISCOUNT_TIER_TRIAGE.summary
    assert outcome.result_json == DISCOUNT_TIER_TRIAGE.model_dump(mode="json")
    assert len(provider.calls) == 1
    assert provider.calls[0].prompt == f"Title: {title}\nDescription:\n{description}\n"
    assert provider.calls[0].schema_name == "TriageResult"


def test_unexpected_model_error_does_not_keep_the_exception_text():
    provider = ExplodingProvider()

    outcome = run_triage(
        compile_triage_graph(),
        task_id=uuid4(),
        title="Broken save",
        description="The button fails.",
        provider=provider,
    )

    assert provider.calls == 1
    assert outcome.status == TaskStatus.FAILED
    assert outcome.stage == "triage"
    assert outcome.result_summary == GENERIC_FAILURE
    assert outcome.result_json == {"error": GENERIC_FAILURE}
    assert SECRET not in outcome.result_summary
    assert SECRET not in str(outcome.result_json)


def test_schema_rejection_is_stored_as_the_provider_error():
    provider = RejectingProvider()

    outcome = run_triage(
        compile_triage_graph(),
        task_id=uuid4(),
        title="Broken save",
        description="The button fails.",
        provider=provider,
    )

    assert provider.calls == 1
    assert outcome.status == TaskStatus.FAILED
    assert outcome.result_summary == "TriageResult rejected model output"
    assert outcome.result_json == {"error": "TriageResult rejected model output"}


def test_worker_concurrency_stays_at_one():
    assert WORKER_CONCURRENCY == 1


async def test_post_returns_queued_and_the_worker_completes_it(
    client: TestClient,
    postgres_database_url: str,
):
    started = time.perf_counter()
    response = client.post(
        "/api/v1/tasks",
        json={"title": DEMO_TITLE, "description": DEMO_DESCRIPTION},
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 2
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["status"] == "queued"
    assert created["stage"] is None
    assert created["result_summary"] is None
    assert created["result_json"] is None

    provider = FakeProvider()
    engine, factory = open_session_factory(postgres_database_url)
    try:
        assert await process_one(factory, provider) is True
        assert await process_one(factory, provider) is False
    finally:
        await engine.dispose()

    fetched = client.get(f"/api/v1/tasks/{created['id']}")
    assert fetched.status_code == 200
    body = fetched.json()
    triage = TriageResult.model_validate(body["result_json"])
    assert body["status"] == "completed"
    assert body["stage"] == "finalize"
    assert body["result_summary"] == triage.summary == DISCOUNT_TIER_TRIAGE.summary
    assert triage == DISCOUNT_TIER_TRIAGE
    assert len(provider.calls) == 1

    get_settings.cache_clear()
    with TestClient(create_app()) as restarted:
        again = restarted.get(f"/api/v1/tasks/{created['id']}")
    assert again.status_code == 200
    assert again.json() == body


async def test_model_call_does_not_hold_the_task_lock(
    postgres_database_url: str,
    clean_tasks: None,
):
    engine, factory = open_session_factory(postgres_database_url)
    try:
        async with factory() as session:
            created = await TaskRepository(session).create(
                title=DEMO_TITLE,
                description=DEMO_DESCRIPTION,
            )
        provider = LockProbe(postgres_database_url, created.id)
        assert await process_one(factory, provider) is True
        async with factory() as session:
            stored = await TaskRepository(session).get(created.id)
    finally:
        await engine.dispose()

    assert provider.saw_running_without_a_lock is True
    assert stored is not None
    assert stored.status == TaskStatus.COMPLETED
    assert len(provider.calls) == 1


async def test_provider_error_is_saved_without_model_text(
    postgres_database_url: str,
    clean_tasks: None,
    caplog: pytest.LogCaptureFixture,
):
    engine, factory = open_session_factory(postgres_database_url)
    provider = ExplodingProvider()
    try:
        async with factory() as session:
            created = await TaskRepository(session).create(
                title="Broken save",
                description=f"failure text must stay out of the row {SECRET}",
            )
        with caplog.at_level("ERROR"):
            assert await process_one(factory, provider) is True
        async with factory() as session:
            stored = await TaskRepository(session).get(created.id)
    finally:
        await engine.dispose()

    assert stored is not None
    assert stored.status == TaskStatus.FAILED
    assert stored.stage == "triage"
    assert stored.result_summary == GENERIC_FAILURE
    assert stored.result_json == {"error": GENERIC_FAILURE}
    assert SECRET not in (stored.result_summary or "")
    assert SECRET not in str(stored.result_json)
    assert SECRET not in caplog.text


def test_check_workflow_once_prints_a_completed_triage(
    postgres_database_url: str,
    clean_tasks: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setenv("DATABASE_URL", postgres_database_url)
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    get_settings.cache_clear()
    from scripts.check_workflow import main

    assert main(["--once"]) == 0
    captured = capsys.readouterr()
    assert "provider=fake" in captured.err
    body = json.loads(captured.out)
    assert body["title"] == DEMO_TITLE
    assert body["status"] == "completed"
    assert body["stage"] == "finalize"
    assert TriageResult.model_validate(body["result_json"]) == DISCOUNT_TIER_TRIAGE


def test_check_workflow_requires_one_mode():
    from scripts.check_workflow import main

    with pytest.raises(SystemExit) as caught:
        main([])

    assert caught.value.code == 2
