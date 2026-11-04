"""The planner proposes a bounded change and does not write the checkout."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from tests.database import open_session_factory
from tests.workflow_support import compiled_worker_graph

from patchpilot.agents.plan_paths import modification_is_allowed
from patchpilot.agents.planner import accept_plan
from patchpilot.api import create_app
from patchpilot.config import get_settings
from patchpilot.db.models import TaskStatus
from patchpilot.llm.errors import LLMResponseError
from patchpilot.llm.fake import (
    AMBIGUOUS_PLAN,
    DEMO_CHANGE_PLAN,
    UNSAFE_PATH_PLAN,
    FakeProvider,
)
from patchpilot.llm.investigation import EvidenceCitation, InvestigationReport
from patchpilot.llm.parsing import parse_structured_output
from patchpilot.llm.plan import ChangePlan, ModificationKind, ProposedModification
from patchpilot.llm.triage import TriageResult
from patchpilot.repositories.artifacts import ArtifactRepository, persist_plan
from patchpilot.repositories.repos import RepoRepository
from patchpilot.repositories.tasks import TaskRepository
from patchpilot.source.tools import RepositoryTools
from patchpilot.worker import process_one
from patchpilot.workflow.graph import compile_triage_graph, run_triage

ROOT = Path(__file__).resolve().parents[1]
DEMO_ROOT = ROOT / "examples"
DEMO_CHECKOUT = DEMO_ROOT / "demo_service"
DEMO_TITLE = "Unknown customer tier crashes discount endpoint"
DEMO_DESCRIPTION = (
    "When the demo-service discount endpoint receives a customer tier it does not "
    "recognize, it returns HTTP 500. It should respond with HTTP 422."
)
SECRET = "plan-schema-secret"
OUTSIDE_SECRET = "outside-plan-secret"


@pytest.fixture
def artifacts_root(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    root.mkdir()
    return root


@pytest.fixture
def client(
    postgres_database_url: str,
    clean_tasks: None,
    artifacts_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("DATABASE_URL", postgres_database_url)
    monkeypatch.setenv("ARTIFACTS_ROOT", str(artifacts_root))
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client


def test_change_plan_rejects_a_schema_that_does_not_match():
    payload = json.dumps({"can_proceed": True, "note": SECRET})

    with pytest.raises(LLMResponseError) as caught:
        parse_structured_output(payload, ChangePlan)

    assert "ChangePlan" in str(caught.value)
    assert SECRET not in str(caught.value)
    assert "note" not in str(caught.value)


def test_permitted_paths_are_existing_python_files_or_new_tests():
    assert modification_is_allowed(
        DEMO_CHECKOUT,
        "src/demo_service/discounts.py",
        ModificationKind.EXISTING,
    )
    assert modification_is_allowed(
        DEMO_CHECKOUT,
        "tests/test_unknown_tier_added.py",
        ModificationKind.NEW_TEST,
    )
    assert not modification_is_allowed(DEMO_CHECKOUT, "../etc/passwd", ModificationKind.EXISTING)
    assert not modification_is_allowed(DEMO_CHECKOUT, "Dockerfile", ModificationKind.NEW_TEST)
    assert not modification_is_allowed(
        DEMO_CHECKOUT,
        ".github/workflows/ci.yml",
        ModificationKind.EXISTING,
    )
    assert not modification_is_allowed(
        DEMO_CHECKOUT,
        "tests/nested/test_more.py",
        ModificationKind.NEW_TEST,
    )
    assert not (DEMO_CHECKOUT / "tests/test_unknown_tier_added.py").exists()
    assert UNSAFE_PATH_PLAN.modifications[1].relative_path == "../etc/passwd"


def test_missing_evidence_overrides_a_plan_that_wants_to_proceed(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    draft = _plan_for("app.py", ModificationKind.EXISTING, proceed=True)
    report = InvestigationReport(
        summary="No line was found.",
        hypotheses=[],
        evidence=[],
        probable_files=[],
        unanswered_questions=["Which file fails?"],
    )

    accepted, rejected = accept_plan(
        draft,
        triage=_triage(needs_clarification=False),
        report=report,
        checkout=tmp_path,
    )

    assert rejected == 0
    assert accepted.can_proceed is False
    assert accepted.modifications == []
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "x = 1\n"


def test_clarification_and_symlinks_cannot_become_a_change(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    app = source / "app.py"
    app.write_text("def ready() -> None:\n    return None\n", encoding="utf-8")
    (source / "link.py").symlink_to(app)
    tests = tmp_path / "tests"
    tests.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text(f"token = {OUTSIDE_SECRET!r}\n", encoding="utf-8")
    (tmp_path / "escaped.py").symlink_to(outside)
    before = _snapshot(tmp_path)
    draft = ChangePlan(
        problem_statement="Change the handler and the link.",
        assumptions=[],
        modifications=[
            ProposedModification(
                relative_path="src/app.py",
                kind=ModificationKind.EXISTING,
                summary="Return a validation error.",
            ),
            ProposedModification(
                relative_path="src/link.py",
                kind=ModificationKind.EXISTING,
                summary="Edit the symlink.",
            ),
            ProposedModification(
                relative_path="escaped.py",
                kind=ModificationKind.EXISTING,
                summary="Edit the file outside the checkout.",
            ),
            ProposedModification(
                relative_path="tests/test_added.py",
                kind=ModificationKind.NEW_TEST,
                summary="Cover the validation error.",
            ),
            ProposedModification(
                relative_path="../outside.py",
                kind=ModificationKind.EXISTING,
                summary="Read the parent directory.",
            ),
        ],
        test_strategy="Run pytest.",
        risks=[],
        confidence=0.4,
        can_proceed=True,
    )
    report = InvestigationReport(
        summary="The handler returns None.",
        hypotheses=[],
        evidence=[EvidenceCitation(relative_path="src/app.py", start_line=1, end_line=2)],
        probable_files=["src/app.py"],
        unanswered_questions=[],
    )

    accepted, rejected = accept_plan(
        draft,
        triage=_triage(needs_clarification=True),
        report=report,
        checkout=tmp_path,
    )

    encoded = accepted.model_dump_json()
    assert accepted.can_proceed is False
    assert accepted.modifications == []
    assert rejected >= 3
    assert "link.py" not in encoded
    assert "../outside.py" not in encoded
    assert OUTSIDE_SECRET not in encoded
    assert _snapshot(tmp_path) == before
    assert outside.read_text(encoding="utf-8") == f"token = {OUTSIDE_SECRET!r}\n"
    assert not (tests / "test_added.py").exists()


async def test_valid_plan_names_existing_files_and_a_new_test():
    before = _snapshot(DEMO_CHECKOUT)
    provider = FakeProvider()

    outcome = await run_triage(
        compile_triage_graph(),
        task_id=uuid4(),
        title=DEMO_TITLE,
        description=DEMO_DESCRIPTION,
        provider=provider,
        repo_id=uuid4(),
        repository_tools=RepositoryTools(DEMO_CHECKOUT),
    )

    assert outcome.status == TaskStatus.WAITING_APPROVAL
    assert outcome.stage == "request_plan_approval"
    assert outcome.plan is not None
    assert outcome.plan.can_proceed is True
    assert outcome.plan == DEMO_CHANGE_PLAN
    assert [call.schema_name for call in provider.calls].count("ChangePlan") == 1
    assert outcome.result_json["plan"]["paths"] == [
        "src/demo_service/discounts.py",
        "src/demo_service/app.py",
        "tests/test_unknown_tier_added.py",
    ]
    assert _snapshot(DEMO_CHECKOUT) == before
    assert not (DEMO_CHECKOUT / "tests/test_unknown_tier_added.py").exists()


async def test_ambiguous_issue_declines_without_modifications():
    before = _snapshot(DEMO_CHECKOUT)

    outcome = await run_triage(
        compile_triage_graph(),
        task_id=uuid4(),
        title=DEMO_TITLE,
        description="This report is ambiguous and does not name a single failing request.",
        provider=FakeProvider(),
        repo_id=uuid4(),
        repository_tools=RepositoryTools(DEMO_CHECKOUT),
    )

    assert outcome.status == TaskStatus.COMPLETED
    assert outcome.plan == AMBIGUOUS_PLAN
    assert outcome.plan is not None
    assert outcome.plan.can_proceed is False
    assert outcome.result_json["plan"]["paths"] == []
    assert _snapshot(DEMO_CHECKOUT) == before


async def test_unsafe_paths_are_dropped_and_the_checkout_stays_unchanged():
    before = _snapshot(DEMO_CHECKOUT)

    outcome = await run_triage(
        compile_triage_graph(),
        task_id=uuid4(),
        title=DEMO_TITLE,
        description="Propose an unsafe path while fixing the unknown tier.",
        provider=FakeProvider(),
        repo_id=uuid4(),
        repository_tools=RepositoryTools(DEMO_CHECKOUT),
    )

    assert outcome.status == TaskStatus.WAITING_APPROVAL
    assert outcome.plan is not None
    assert outcome.plan.can_proceed is True
    assert [item.relative_path for item in outcome.plan.modifications] == [
        "src/demo_service/discounts.py"
    ]
    encoded = outcome.plan.model_dump_json()
    assert "../etc/passwd" not in encoded
    assert "Dockerfile" not in encoded
    assert ".github" not in encoded
    assert outcome.result_json["plan"]["rejected_paths"] >= 1
    assert _snapshot(DEMO_CHECKOUT) == before


async def test_only_unsafe_paths_decline():
    outcome = await run_triage(
        compile_triage_graph(),
        task_id=uuid4(),
        title=DEMO_TITLE,
        description="Use an unsafe path only and do not touch the checkout.",
        provider=FakeProvider(),
        repo_id=uuid4(),
        repository_tools=RepositoryTools(DEMO_CHECKOUT),
    )

    assert outcome.plan is not None
    assert outcome.plan.can_proceed is False
    assert outcome.plan.modifications == []
    encoded = outcome.plan.model_dump_json()
    assert "../etc/passwd" not in encoded
    assert "Dockerfile" not in encoded


async def test_plan_schema_failure_stops_at_the_plan_stage():
    before = _snapshot(DEMO_CHECKOUT)

    outcome = await run_triage(
        compile_triage_graph(),
        task_id=uuid4(),
        title=DEMO_TITLE,
        description=f"Trigger a plan schema failure. Token {SECRET}.",
        provider=FakeProvider(),
        repo_id=uuid4(),
        repository_tools=RepositoryTools(DEMO_CHECKOUT),
    )

    assert outcome.status == TaskStatus.FAILED
    assert outcome.stage == "plan"
    assert outcome.plan is None
    assert "ChangePlan" in outcome.result_summary
    stored = json.dumps(outcome.result_json)
    assert SECRET not in stored
    assert outcome.result_json == {"error": outcome.result_summary}
    assert _snapshot(DEMO_CHECKOUT) == before


async def test_plan_revisions_keep_earlier_bytes(
    postgres_database_url: str,
    clean_tasks: None,
    tmp_path: Path,
):
    engine, factory = open_session_factory(postgres_database_url)
    try:
        async with factory() as session:
            created = await TaskRepository(session).create(
                title="Save fails",
                description="The button fails.",
            )
        async with factory() as session:
            first = await persist_plan(session, tmp_path, created.id, AMBIGUOUS_PLAN)
            again = await persist_plan(session, tmp_path, created.id, AMBIGUOUS_PLAN)
            second = await persist_plan(session, tmp_path, created.id, DEMO_CHANGE_PLAN)
            await session.commit()
            repository = ArtifactRepository(session)
            latest = await repository.get_plan(created.id)
            earlier = await repository.get_plan(created.id, 1)
    finally:
        await engine.dispose()

    assert first == 1
    assert again == 1
    assert second == 2
    assert latest is not None and earlier is not None
    assert latest.version == 2
    assert earlier.version == 1
    assert earlier.checksum != latest.checksum
    files = sorted(path.name for path in tmp_path.rglob("*.json"))
    assert files == ["v1.json", "v2.json"]


async def test_worker_exposes_a_versioned_plan_without_writing_the_checkout(
    client: TestClient,
    postgres_database_url: str,
    artifacts_root: Path,
):
    before = _snapshot(DEMO_CHECKOUT)
    engine, factory = open_session_factory(postgres_database_url)
    try:
        async with factory() as session:
            repository = await RepoRepository(session).save(
                display_name="Demo service",
                canonical_path="demo_service",
                registration={"head": "0" * 40},
            )
        created = client.post(
            "/api/v1/tasks",
            json={
                "title": DEMO_TITLE,
                "description": DEMO_DESCRIPTION,
                "repo_id": str(repository.id),
            },
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        missing = client.get(f"/api/v1/tasks/{task_id}/plan")
        assert missing.status_code == 404

        async with compiled_worker_graph(postgres_database_url) as graph:
            assert (
                await process_one(
                    factory,
                    FakeProvider(),
                    graph,
                    repository_root=DEMO_ROOT.resolve(),
                    artifacts_root=artifacts_root,
                )
                is True
            )
    finally:
        await engine.dispose()

    fetched = client.get(f"/api/v1/tasks/{task_id}/plan")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["version"] == 1
    assert body["schema_version"] == 1
    plan = ChangePlan.model_validate(body["plan"])
    assert plan == DEMO_CHANGE_PLAN
    assert plan.can_proceed is True
    encoded = json.dumps(body)
    assert str(artifacts_root) not in encoded
    assert str(DEMO_CHECKOUT) not in encoded
    assert "../" not in encoded
    files = list(artifacts_root.rglob("*.json"))
    assert len(files) == 1
    payload = files[0].read_bytes()
    assert hashlib.sha256(payload).hexdigest() == body["checksum"]
    assert len(payload) == body["size"]
    assert client.get(f"/api/v1/tasks/{task_id}/plan?version=1").json() == body
    assert client.get(f"/api/v1/tasks/{task_id}/plan?version=2").status_code == 404
    assert client.get(f"/api/v1/tasks/{task_id}/plan?version=0").status_code == 422

    events = client.get(f"/api/v1/tasks/{task_id}/events")
    plan_event = next(
        item
        for item in events.json()["items"]
        if item["stage"] == "plan" and item["event_type"] == "stage_completed"
    )
    assert plan_event["payload"]["can_proceed"] is True
    assert plan_event["payload"]["version"] == 1
    assert "src/demo_service/discounts.py" in plan_event["payload"]["paths"]
    assert "return KNOWN_TIERS" not in json.dumps(plan_event["payload"])

    files[0].write_bytes(payload + b" ")
    assert client.get(f"/api/v1/tasks/{task_id}/plan").status_code == 404
    assert _snapshot(DEMO_CHECKOUT) == before


async def test_worker_stores_a_decline_when_the_issue_is_ambiguous(
    client: TestClient,
    postgres_database_url: str,
    artifacts_root: Path,
):
    before = _snapshot(DEMO_CHECKOUT)
    engine, factory = open_session_factory(postgres_database_url)
    try:
        async with factory() as session:
            repository = await RepoRepository(session).save(
                display_name="Demo service",
                canonical_path="demo_service",
                registration={"head": "0" * 40},
            )
        created = client.post(
            "/api/v1/tasks",
            json={
                "title": DEMO_TITLE,
                "description": "This report is ambiguous and names no single failing request.",
                "repo_id": str(repository.id),
            },
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        async with compiled_worker_graph(postgres_database_url) as graph:
            assert (
                await process_one(
                    factory,
                    FakeProvider(),
                    graph,
                    repository_root=DEMO_ROOT.resolve(),
                    artifacts_root=artifacts_root,
                )
                is True
            )
    finally:
        await engine.dispose()

    fetched = client.get(f"/api/v1/tasks/{task_id}/plan")
    assert fetched.status_code == 200
    plan = ChangePlan.model_validate(fetched.json()["plan"])
    assert plan == AMBIGUOUS_PLAN
    assert fetched.json()["version"] == 1
    assert _snapshot(DEMO_CHECKOUT) == before


def _plan_for(relative_path: str, kind: ModificationKind, *, proceed: bool) -> ChangePlan:
    return ChangePlan(
        problem_statement="Change the cited file.",
        assumptions=[],
        modifications=[
            ProposedModification(
                relative_path=relative_path,
                kind=kind,
                summary="Return a validation error.",
            )
        ],
        test_strategy="Run pytest.",
        risks=[],
        confidence=0.8,
        can_proceed=proceed,
    )


def _triage(*, needs_clarification: bool) -> TriageResult:
    from patchpilot.llm.fake import DISCOUNT_TIER_TRIAGE, NEEDS_CLARIFICATION_TRIAGE

    if needs_clarification:
        return NEEDS_CLARIFICATION_TRIAGE
    return DISCOUNT_TIER_TRIAGE


def _snapshot(root: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if any(part in {"__pycache__", ".git", ".pytest_cache"} for part in path.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found
