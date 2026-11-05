"""Budgets, routing, and usage records. Cloud stays uncalled unless it is enabled."""

import json
from decimal import Decimal
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from tests.database import open_session_factory
from tests.workflow_support import compiled_worker_graph

from patchpilot.agents.investigator import InvestigatorAgent
from patchpilot.api import create_app
from patchpilot.config import Settings, get_settings
from patchpilot.llm.budget import TaskBudget
from patchpilot.llm.capabilities import ModelCapability
from patchpilot.llm.errors import (
    LLMBudgetError,
    LLMCapabilityError,
    LLMCircuitError,
    LLMPermanentError,
    LLMResponseError,
)
from patchpilot.llm.fake import DISCOUNT_TIER_TRIAGE, FakeProvider
from patchpilot.llm.investigation import InvestigatorStep, ToolName, ToolRequest
from patchpilot.llm.local_gate import LocalAdvisoryLock, LocalGate
from patchpilot.llm.meter import CallMeter
from patchpilot.llm.ollama import OllamaProvider
from patchpilot.llm.openai_compatible import OpenAICompatibleProvider
from patchpilot.llm.pricing import estimate_cost_usd, load_pricing
from patchpilot.llm.provider import GenerateOptions
from patchpilot.llm.routing import (
    UsageControl,
    build_router,
    parse_stage_models,
    provider_capabilities,
)
from patchpilot.llm.triage import TriageResult
from patchpilot.llm.usage_report import ReportedUsage
from patchpilot.repositories.model_calls import ModelCallRepository
from patchpilot.source.tools import TreeListing
from patchpilot.worker import process_one

DATABASE_URL = "postgresql://patchpilot:patchpilot_dev@localhost/patchpilot"
PROMPT = "Title: discount tier\nDescription:\nAn unknown customer tier returns HTTP 500.\n"


def _settings(**overrides: object) -> Settings:
    return Settings(database_url=DATABASE_URL, _env_file=None, **overrides)  # type: ignore[arg-type]


def _meter(
    provider: FakeProvider,
    *,
    pricing=None,
    gate: LocalGate | None = None,
    advisory=None,
    **limits: object,
) -> CallMeter:
    budget = TaskBudget(**limits)  # type: ignore[arg-type]
    return CallMeter(
        task_id=uuid4(),
        budget=budget,
        pricing=pricing,
        gate=gate or LocalGate(),
        advisory=advisory,
    )


def _run(meter: CallMeter, provider: FakeProvider, *, local: bool = False) -> object:
    return meter.run(
        stage="triage",
        provider_name="fake",
        model="gemma4:12b",
        capabilities=frozenset({ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOL_CALLING}),
        local=local,
        provider=provider,
        prompt=PROMPT,
        schema=TriageResult,
        options=None,
    )


def test_stage_routes_default_to_the_configured_provider():
    settings = _settings()
    choices = parse_stage_models(
        "",
        default_provider=settings.llm_provider,
        default_model=settings.llm_model,
        capabilities_for=lambda name: provider_capabilities(settings, name),
    )

    assert set(choices) == {
        "triage",
        "investigate",
        "plan",
        "develop",
        "review",
        "supervisor",
    }
    assert all(choice.provider == "fake" for choice in choices.values())
    overridden = _settings(llm_stage_models="triage=fake:fixture-model")
    routed = parse_stage_models(
        overridden.llm_stage_models,
        default_provider=overridden.llm_provider,
        default_model=overridden.llm_model,
        capabilities_for=lambda name: provider_capabilities(overridden, name),
    )
    assert routed["triage"].model == "fixture-model"
    assert routed["plan"].model == "gemma4:12b"


def test_cloud_provider_is_rejected_until_it_is_enabled():
    with pytest.raises(ValidationError):
        _settings(llm_provider="openai")
    with pytest.raises(ValidationError):
        _settings(llm_stage_models="review=openai:gpt-4.1-mini")
    with pytest.raises(ValidationError, match="structured_output"):
        _settings(
            llm_cloud_enabled=True,
            llm_cloud_base_url="https://example.test/v1",
            llm_cloud_api_key="sk-test",
            llm_stage_models="review=openai:gpt-4.1-mini",
        )


def test_fake_router_does_not_construct_a_cloud_client(monkeypatch: pytest.MonkeyPatch):
    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("cloud client")

    monkeypatch.setattr("patchpilot.llm.openai_compatible.httpx2.Client", boom)
    router = build_router(_settings())
    try:
        assert router.choices["develop"].provider == "fake"
        assert router.uses_local is False
    finally:
        router.close()


def test_disabled_cloud_provider_does_not_open_a_client(monkeypatch: pytest.MonkeyPatch):
    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("cloud client")

    monkeypatch.setattr("patchpilot.llm.openai_compatible.httpx2.Client", boom)
    with pytest.raises(LLMPermanentError, match="disabled"):
        OpenAICompatibleProvider(
            enabled=False,
            base_url="https://example.test/v1",
            api_key="sk-test",
            model="gpt-4.1-mini",
            timeout_seconds=5,
            max_retries=0,
            capabilities=frozenset({ModelCapability.STRUCTURED_OUTPUT}),
        )


def test_cloud_call_is_refused_when_structured_output_is_not_advertised():
    class Client:
        def __init__(self) -> None:
            self.posts = 0

        def post(self, *args: object, **kwargs: object) -> object:
            self.posts += 1
            raise AssertionError("cloud post")

        def close(self) -> None:
            return None

    client = Client()
    provider = OpenAICompatibleProvider(
        enabled=True,
        base_url="https://example.test/v1",
        api_key="sk-test",
        model="gpt-4.1-mini",
        timeout_seconds=5,
        max_retries=0,
        capabilities=frozenset({ModelCapability.TOOL_CALLING}),
        client=client,  # type: ignore[arg-type]
    )
    try:
        with pytest.raises(LLMCapabilityError, match="structured_output"):
            provider.generate_structured(PROMPT, TriageResult)
    finally:
        provider.close()

    assert client.posts == 0


def test_enabled_cloud_posts_once_and_hides_the_key():
    seen: list[httpx2.Request] = []
    body = {
        "classification": "bug",
        "severity": "medium",
        "summary": "The report describes a defect that needs a small, tested fix.",
        "acceptance_criteria": ["The reported failure no longer occurs."],
        "confidence": 0.5,
        "needs_clarification": False,
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(body)}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            },
        )

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        enabled=True,
        base_url="https://example.test/v1",
        api_key="sk-test-secret",
        model="gpt-4.1-mini",
        timeout_seconds=5,
        max_retries=0,
        capabilities=frozenset({ModelCapability.STRUCTURED_OUTPUT}),
        client=client,
    )
    try:
        result = provider.generate_structured(PROMPT, TriageResult, GenerateOptions())
    finally:
        provider.close()

    assert result.classification.value == "bug"
    assert provider.take_usage() == ReportedUsage(prompt_tokens=3, output_tokens=4)
    assert len(seen) == 1
    assert seen[0].headers["authorization"] == "Bearer sk-test-secret"
    assert b"sk-test-secret" not in seen[0].url.raw_path


def test_model_call_budget_stops_before_another_fake_call():
    provider = FakeProvider()
    meter = _meter(provider, max_model_calls=1)
    _run(meter, provider)
    with pytest.raises(LLMBudgetError, match="model call budget"):
        _run(meter, provider)

    assert len(provider.calls) == 1
    assert [record.outcome for record in meter.records] == ["success", "budget_exhausted"]


def test_token_budget_counts_only_reported_tokens():
    silent = FakeProvider()
    open_budget = _meter(silent, max_tokens=1)
    _run(open_budget, silent)
    _run(open_budget, silent)
    assert len(silent.calls) == 2

    provider = FakeProvider(usage=ReportedUsage(prompt_tokens=80, output_tokens=30))
    meter = _meter(provider, max_tokens=100)
    with pytest.raises(LLMBudgetError, match="token budget"):
        _run(meter, provider)

    assert len(provider.calls) == 1
    assert meter.records[0].outcome == "budget_exhausted"
    assert meter.records[0].prompt_tokens == 80
    assert meter.records[0].cost_known is False
    assert meter.records[0].estimated_cost_usd is None


def test_runtime_budget_stops_the_next_fake_call():
    now = {"t": 0.0}
    provider = FakeProvider()
    meter = _meter(provider, max_runtime_seconds=10, clock=lambda: now["t"])
    _run(meter, provider)
    now["t"] = 11
    with pytest.raises(LLMBudgetError, match="runtime"):
        _run(meter, provider)

    assert len(provider.calls) == 1


def test_parallelism_budget_and_local_slot_do_not_call_the_provider():
    provider = FakeProvider()
    budget = TaskBudget(max_parallelism=1)
    meter = CallMeter(task_id=uuid4(), budget=budget, pricing=None, gate=LocalGate())
    budget.begin_model_call()
    with pytest.raises(LLMBudgetError, match="parallelism"):
        _run(meter, provider)
    assert provider.calls == []

    local = FakeProvider()
    gate = LocalGate()
    gate.acquire()
    blocked = CallMeter(task_id=uuid4(), budget=TaskBudget(), pricing=None, gate=gate)
    with pytest.raises(LLMBudgetError, match="local model"):
        _run(blocked, local, local=True)
    assert local.calls == []
    gate.release()


def test_tool_budget_stops_the_investigator_before_another_read():
    class AlwaysTool(FakeProvider):
        def generate_structured(self, prompt, schema, options=None):  # type: ignore[no-untyped-def]
            self.calls.append(prompt)
            if schema is not InvestigatorStep:
                raise LLMResponseError("FakeProvider has no fixture for that schema")
            return InvestigatorStep(
                tool=ToolRequest(name=ToolName.LIST_TREE, relative_path="."),
                report=None,
            )

    class StubTools:
        def list_tree(self, path: str) -> TreeListing:
            return TreeListing(path=".", entries=[], truncated=False)

        def search_text(self, query: str, path: str) -> object:
            raise AssertionError(query)

        def read_text_file(self, path: str) -> object:
            raise AssertionError(path)

    provider = AlwaysTool()
    budget = TaskBudget(max_tool_calls=1)
    with pytest.raises(LLMBudgetError, match="tool call"):
        InvestigatorAgent(
            provider,
            StubTools(),  # type: ignore[arg-type]
            max_tool_calls=4,
            charge_tool=budget.charge_tool,
        ).run(
            title="discount",
            description="unknown tier",
            triage=DISCOUNT_TIER_TRIAGE,
        )

    assert len(provider.calls) == 2


def test_repair_budget_stops_before_another_fake_call():
    provider = FakeProvider()
    budget = TaskBudget(max_repairs=2)
    budget.allow_repair(2)
    provider.generate_structured(PROMPT, TriageResult)
    with pytest.raises(LLMBudgetError, match="repair"):
        budget.allow_repair(3)
        provider.generate_structured(PROMPT, TriageResult)

    assert len(provider.calls) == 1


def test_invalid_output_opens_the_circuit_and_does_not_repeat():
    class Broken(FakeProvider):
        def generate_structured(self, prompt, schema, options=None):  # type: ignore[no-untyped-def]
            self.calls.append(prompt)
            raise LLMResponseError("TriageResult rejected model output")

    provider = Broken()
    meter = _meter(provider)
    with pytest.raises(LLMResponseError):
        _run(meter, provider)
    with pytest.raises(LLMResponseError):
        _run(meter, provider)
    with pytest.raises(LLMCircuitError, match="circuit"):
        _run(meter, provider)

    assert len(provider.calls) == 2
    assert meter.records[-1].outcome == "circuit_open"


def test_cost_is_unknown_without_a_verified_price(tmp_path):
    priced = tmp_path / "prices.json"
    priced.write_text(
        json.dumps(
            {
                "version": 1,
                "prices": {
                    "fake:gemma4:12b": {
                        "prompt_usd_per_million": "2",
                        "output_usd_per_million": "3",
                        "verified": True,
                    },
                    "fake:other": {
                        "prompt_usd_per_million": "9",
                        "output_usd_per_million": "9",
                        "verified": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    table = load_pricing(str(priced))
    assert (
        estimate_cost_usd(
            table,
            provider="fake",
            model="other",
            prompt_tokens=10,
            output_tokens=10,
        )
        is None
    )
    assert (
        estimate_cost_usd(
            None,
            provider="fake",
            model="gemma4:12b",
            prompt_tokens=10,
            output_tokens=10,
        )
        is None
    )
    assert (
        estimate_cost_usd(
            table,
            provider="fake",
            model="gemma4:12b",
            prompt_tokens=None,
            output_tokens=10,
        )
        is None
    )
    cost = estimate_cost_usd(
        table,
        provider="fake",
        model="gemma4:12b",
        prompt_tokens=1_000_000,
        output_tokens=2_000_000,
    )
    assert cost == Decimal("8.000000")

    provider = FakeProvider(usage=ReportedUsage(prompt_tokens=1_000, output_tokens=2_000))
    meter = _meter(provider, pricing=table)
    _run(meter, provider)
    assert meter.records[0].cost_known is True
    assert meter.records[0].estimated_cost_usd == Decimal("0.008000")

    missing = tmp_path / "missing.json"
    missing.write_text("{", encoding="utf-8")
    assert load_pricing(str(missing)) is None
    assert load_pricing(None) is None


def test_transient_http_status_backs_off_and_a_500_does_not():
    pauses: list[float] = []

    def handler_factory(statuses: list[int]):
        seen: list[int] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request.url.path)
            status = statuses[len(seen) - 1]
            if status == 200:
                return httpx2.Response(
                    200,
                    json={
                        "model": "gemma4:12b",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "classification": "bug",
                                    "severity": "medium",
                                    "summary": "The report describes a defect.",
                                    "acceptance_criteria": ["The failure is gone."],
                                    "confidence": 0.5,
                                    "needs_clarification": False,
                                }
                            ),
                        },
                        "done": True,
                        "prompt_eval_count": 2,
                        "eval_count": 3,
                    },
                )
            return httpx2.Response(status, content=b"secret-body")

        return handler, seen

    retry, seen = handler_factory([429, 200])
    provider = OllamaProvider(
        base_url="http://ollama.test:11434",
        model="gemma4:12b",
        timeout_seconds=5,
        max_retries=1,
        backoff_seconds=0.25,
        sleeper=pauses.append,
        client=httpx2.Client(transport=httpx2.MockTransport(retry)),
    )
    try:
        result = provider.generate_structured("The save button fails.", TriageResult)
    finally:
        provider.close()

    assert result.needs_clarification is False
    assert pauses == [0.25]
    assert len(seen) == 2
    assert provider.take_usage() == ReportedUsage(prompt_tokens=2, output_tokens=3)

    permanent, permanent_seen = handler_factory([500, 200])
    failed = OllamaProvider(
        base_url="http://ollama.test:11434",
        model="gemma4:12b",
        timeout_seconds=5,
        max_retries=2,
        backoff_seconds=0.25,
        sleeper=pauses.append,
        client=httpx2.Client(transport=httpx2.MockTransport(permanent)),
    )
    with pytest.raises(Exception, match="HTTP 500"):
        try:
            failed.generate_structured("The save button fails.", TriageResult)
        finally:
            failed.close()
    assert len(permanent_seen) == 1


def test_parallelism_cannot_be_raised():
    with pytest.raises(ValidationError):
        _settings(task_max_parallelism=2)


def test_advisory_lock_allows_one_local_model(postgres_database_url: str):
    first = LocalAdvisoryLock(postgres_database_url)
    try:
        with pytest.raises(LLMBudgetError, match="local model"):
            LocalAdvisoryLock(postgres_database_url)
    finally:
        first.release()
    second = LocalAdvisoryLock(postgres_database_url)
    second.release()


@pytest.fixture
def client(postgres_database_url: str, clean_tasks: None, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", postgres_database_url)
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


async def test_usage_endpoint_lists_a_fake_triage_call(
    client: TestClient,
    postgres_database_url: str,
):
    missing = client.get(f"/api/v1/tasks/{uuid4()}/usage")
    assert missing.status_code == 404

    created = client.post(
        "/api/v1/tasks",
        json={
            "title": "Unknown customer tier crashes discount endpoint",
            "description": "An unrecognized customer tier returns HTTP 500.",
        },
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]

    settings = _settings()
    router = build_router(settings)
    usage = UsageControl(
        router=router,
        pricing=None,
        max_model_calls=settings.task_max_model_calls,
        max_tool_calls=settings.task_max_tool_calls,
        max_tokens=settings.task_max_tokens,
        max_runtime_seconds=settings.task_max_runtime_seconds,
        max_parallelism=settings.task_max_parallelism,
        max_repairs=settings.task_max_repairs,
        database_url=postgres_database_url,
        gate=LocalGate(),
    )
    engine, factory = open_session_factory(postgres_database_url)
    try:
        async with compiled_worker_graph(postgres_database_url) as graph:
            assert await process_one(factory, router.fallback(), graph, usage=usage) is True
        async with factory() as session:
            rows = await ModelCallRepository(session).list_for_task(UUID(task_id))
    finally:
        router.close()
        await engine.dispose()

    listed = client.get(f"/api/v1/tasks/{task_id}/usage")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["totals"]["model_calls"] == 1
    assert body["totals"]["cost"] == "unknown"
    assert body["totals"]["estimated_cost_usd"] is None
    assert body["totals"]["prompt_tokens"] is None
    assert body["limits"]["parallelism"] == 1
    assert body["limits"]["repairs"] == 2
    call = body["calls"][0]
    assert call["stage"] == "triage"
    assert call["provider"] == "fake"
    assert call["model"] == "gemma4:12b"
    assert call["outcome"] == "success"
    assert call["cost"] == "unknown"
    assert call["estimated_cost_usd"] is None
    assert "prompt" not in call
    assert len(rows) == 1
