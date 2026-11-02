"""Offline contracts for the fake provider, mocked Ollama, and the smoke script."""

import json
import logging

import httpx2
import pytest
from pydantic import BaseModel, ConfigDict
from scripts.check_llm import main

from patchpilot.config import Settings, get_settings
from patchpilot.llm.errors import LLMResponseError, LLMTimeoutError, LLMTransportError
from patchpilot.llm.factory import create_provider
from patchpilot.llm.fake import (
    DISCOUNT_TIER_TRIAGE,
    GENERIC_BUG_TRIAGE,
    NEEDS_CLARIFICATION_TRIAGE,
    FakeProvider,
)
from patchpilot.llm.ollama import OllamaProvider
from patchpilot.llm.provider import GenerateOptions
from patchpilot.llm.triage import TriageResult

SECRET = "raw-model-secret-token"
DATABASE_URL = "postgresql://patchpilot:patchpilot_dev@localhost/patchpilot"


class Echo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str


def assert_model_text_stays_out_of_the_error(exc: BaseException, secret: str) -> None:
    assert secret not in str(exc)
    assert secret not in repr(exc)
    assert exc.__cause__ is None or secret not in str(exc.__cause__)
    assert exc.__cause__ is None or secret not in repr(exc.__cause__)
    assert exc.__context__ is None or exc.__suppress_context__
    if exc.__context__ is not None and not exc.__suppress_context__:
        assert secret not in str(exc.__context__)


def test_fake_triage_is_deterministic():
    provider = FakeProvider()
    prompt = "Title: Unknown customer tier crashes discount endpoint"

    first = provider.generate_structured(prompt, TriageResult)
    second = provider.generate_structured(prompt, TriageResult)

    assert first == second == DISCOUNT_TIER_TRIAGE
    assert [call.schema_name for call in provider.calls] == ["TriageResult", "TriageResult"]
    assert provider.calls[0].temperature == 0.0


def test_fake_selects_clarification_and_generic_fixtures():
    provider = FakeProvider()

    vague = provider.generate_structured(
        "This report needs clarification before a change.",
        TriageResult,
    )
    generic = provider.generate_structured("The save button fails.", TriageResult)

    assert vague == NEEDS_CLARIFICATION_TRIAGE
    assert vague.needs_clarification is True
    assert generic == GENERIC_BUG_TRIAGE


def test_fake_fixture_overrides_the_builtin_and_validates_the_schema():
    provider = FakeProvider(fixtures={"ping": {"answer": "pong"}})

    result = provider.generate_structured("ping", Echo, GenerateOptions(temperature=0.2))

    assert result.answer == "pong"
    assert provider.calls[0].temperature == 0.2


def test_fake_rejected_fixture_does_not_leak_the_payload():
    provider = FakeProvider(fixtures={"ping": {"answer": "ok", "injected": SECRET}})

    with pytest.raises(LLMResponseError) as caught:
        provider.generate_structured("ping", Echo)

    assert_model_text_stays_out_of_the_error(caught.value, SECRET)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(provider.calls) == 1


def test_fake_missing_fixture_is_a_typed_error():
    provider = FakeProvider()

    with pytest.raises(LLMResponseError, match="no fixture for Echo"):
        provider.generate_structured("ping", Echo)


def test_blank_or_huge_prompt_is_rejected_before_a_call():
    provider = FakeProvider()

    with pytest.raises(ValueError, match="non-empty"):
        provider.generate_structured("   ", TriageResult)
    with pytest.raises(ValueError, match="at most"):
        provider.generate_structured("x" * 32_001, TriageResult)

    assert provider.calls == []


def test_options_must_be_the_typed_object():
    provider = FakeProvider()

    with pytest.raises(TypeError, match="GenerateOptions"):
        provider.generate_structured("The save button fails.", TriageResult, {"temperature": 1})


def test_factory_uses_fake_without_opening_a_client(monkeypatch):
    def fail_client(*args, **kwargs):
        raise AssertionError("fake provider must not construct an HTTP client")

    monkeypatch.setattr("patchpilot.llm.ollama.httpx2.Client", fail_client)
    settings = Settings(database_url=DATABASE_URL, llm_provider="fake", _env_file=None)

    provider = create_provider(settings)
    try:
        result = provider.generate_structured("The save button fails.", TriageResult)
    finally:
        provider.close()

    assert isinstance(provider, FakeProvider)
    assert result == GENERIC_BUG_TRIAGE


def test_factory_builds_an_ollama_provider():
    settings = Settings(
        database_url=DATABASE_URL,
        llm_provider="ollama",
        llm_model="qwen3.5:9b",
        ollama_base_url="http://host.docker.internal:11434",
        llm_timeout_seconds=9,
        llm_max_retries=1,
        _env_file=None,
    )

    provider = create_provider(settings)
    try:
        assert isinstance(provider, OllamaProvider)
        assert provider._model == "qwen3.5:9b"
        assert provider._base_url == "http://host.docker.internal:11434"
        assert provider._timeout_seconds == 9
        assert provider._max_retries == 1
    finally:
        provider.close()


def test_ollama_client_ignores_proxy_environment(monkeypatch):
    captured: dict[str, object] = {}

    class RecordingClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def close(self):
            return None

    monkeypatch.setattr("patchpilot.llm.ollama.httpx2.Client", RecordingClient)
    provider = OllamaProvider(
        base_url="http://127.0.0.1:11434",
        model="gemma4:12b",
        timeout_seconds=5,
        max_retries=0,
    )

    provider.close()

    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False


def test_owned_client_closes_and_an_injected_client_stays_open():
    owned = OllamaProvider(
        base_url="http://127.0.0.1:11434",
        model="gemma4:12b",
        timeout_seconds=1,
        max_retries=0,
    )
    owned.close()
    owned.close()
    assert owned._client.is_closed is True

    client = httpx2.Client()
    injected = OllamaProvider(
        base_url="http://127.0.0.1:11434",
        model="gemma4:12b",
        timeout_seconds=1,
        max_retries=0,
        client=client,
    )
    injected.close()
    assert client.is_closed is False
    client.close()


def test_provider_rejects_credentials_and_unbounded_retries():
    with pytest.raises(ValueError, match="credentials"):
        OllamaProvider(
            base_url="http://user:secret@127.0.0.1:11434",
            model="gemma4:12b",
            timeout_seconds=1,
            max_retries=0,
        )
    with pytest.raises(ValueError, match="max_retries"):
        OllamaProvider(
            base_url="http://127.0.0.1:11434",
            model="gemma4:12b",
            timeout_seconds=1,
            max_retries=4,
        )


def scripted_provider(
    responses: list[httpx2.Response | Exception],
    *,
    max_retries: int = 2,
    timeout_seconds: float = 5.0,
) -> tuple[OllamaProvider, list[httpx2.Request], httpx2.Client]:
    seen: list[httpx2.Request] = []
    queue = list(responses)

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    provider = OllamaProvider(
        base_url="http://ollama.test:11434/",
        model="gemma4:12b",
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        client=client,
    )
    return provider, seen, client


def chat_response(content: str) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "model": "gemma4:12b",
            "message": {"role": "assistant", "content": content},
            "done": True,
        },
    )


def test_ollama_posts_a_schema_constrained_chat_request_and_validates_it():
    provider, seen, client = scripted_provider(
        [chat_response(GENERIC_BUG_TRIAGE.model_dump_json())]
    )

    try:
        result = provider.generate_structured(
            "The save button fails.",
            TriageResult,
            GenerateOptions(temperature=0.0),
        )
    finally:
        provider.close()
        client.close()

    assert result == GENERIC_BUG_TRIAGE
    assert len(seen) == 1
    assert str(seen[0].url) == "http://ollama.test:11434/api/chat"
    body = json.loads(seen[0].content)
    assert set(body) == {"model", "messages", "stream", "format", "options"}
    assert body["model"] == "gemma4:12b"
    assert body["stream"] is False
    assert body["format"] == TriageResult.model_json_schema()
    assert body["options"] == {"temperature": 0.0}
    assert body["messages"][1] == {"role": "user", "content": "The save button fails."}
    assert "patchpilot_dev" not in seen[0].content.decode()


def test_ollama_retries_invalid_json_then_accepts_a_valid_object():
    provider, seen, client = scripted_provider(
        [
            chat_response(f"not json {SECRET}"),
            chat_response(GENERIC_BUG_TRIAGE.model_dump_json()),
            chat_response("should not be requested"),
        ]
    )

    try:
        result = provider.generate_structured("The save button fails.", TriageResult)
    finally:
        provider.close()
        client.close()

    assert result == GENERIC_BUG_TRIAGE
    assert len(seen) == 2


def test_invalid_model_output_fails_closed_without_leaking_text(caplog):
    provider, seen, client = scripted_provider([chat_response(SECRET) for _ in range(3)])

    with caplog.at_level(logging.DEBUG), pytest.raises(LLMResponseError) as caught:
        try:
            provider.generate_structured("The save button fails.", TriageResult)
        finally:
            provider.close()
            client.close()

    assert len(seen) == 3
    assert "validation error" in str(caught.value)
    assert_model_text_stays_out_of_the_error(caught.value, SECRET)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert SECRET not in caplog.text


def test_schema_mismatch_is_rejected_without_the_payload():
    bad = json.dumps({"classification": "bug", "summary": SECRET})
    provider, seen, client = scripted_provider([chat_response(bad)], max_retries=0)

    with pytest.raises(LLMResponseError, match="validation errors") as caught:
        try:
            provider.generate_structured("The save button fails.", TriageResult)
        finally:
            provider.close()
            client.close()

    assert len(seen) == 1
    assert_model_text_stays_out_of_the_error(caught.value, SECRET)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_timeout_retries_then_raises_a_typed_timeout():
    timeout = httpx2.ReadTimeout("timed out")
    provider, seen, client = scripted_provider(
        [timeout, timeout, timeout],
        max_retries=1,
        timeout_seconds=1.5,
    )

    with pytest.raises(LLMTimeoutError, match="1.5 seconds") as caught:
        try:
            provider.generate_structured("The save button fails.", TriageResult)
        finally:
            provider.close()
            client.close()

    assert len(seen) == 2
    assert SECRET not in str(caught.value)


def test_http_error_is_not_retried_and_hides_the_body(caplog):
    provider, seen, client = scripted_provider(
        [
            httpx2.Response(500, content=SECRET.encode()),
            chat_response(GENERIC_BUG_TRIAGE.model_dump_json()),
        ]
    )

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(LLMTransportError, match="HTTP 500") as caught,
    ):
        try:
            provider.generate_structured("The save button fails.", TriageResult)
        finally:
            provider.close()
            client.close()

    assert len(seen) == 1
    assert_model_text_stays_out_of_the_error(caught.value, SECRET)
    assert caught.value.__cause__ is None
    assert SECRET not in caplog.text


def test_connection_failure_is_not_retried():
    provider, seen, client = scripted_provider(
        [httpx2.ConnectError("connection refused"), httpx2.ConnectError("again")]
    )

    with pytest.raises(LLMTransportError, match="failed before a response"):
        try:
            provider.generate_structured("The save button fails.", TriageResult)
        finally:
            provider.close()
            client.close()

    assert len(seen) == 1


def test_non_json_and_non_text_bodies_fail_without_their_contents():
    provider, seen, client = scripted_provider(
        [
            httpx2.Response(200, content=f"<{SECRET}>".encode()),
            httpx2.Response(200, json={"message": {"content": {"leak": SECRET}}}),
        ],
        max_retries=1,
    )

    with pytest.raises(LLMResponseError) as caught:
        try:
            provider.generate_structured("The save button fails.", TriageResult)
        finally:
            provider.close()
            client.close()

    assert len(seen) == 2
    assert_model_text_stays_out_of_the_error(caught.value, SECRET)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_oversized_response_is_rejected_before_it_is_parsed():
    huge = "y" * 20_000
    provider, _seen, client = scripted_provider(
        [httpx2.Response(200, content=huge.encode())],
        max_retries=0,
    )

    with pytest.raises(LLMResponseError, match="not a JSON object") as caught:
        try:
            provider.generate_structured("The save button fails.", TriageResult)
        finally:
            provider.close()
            client.close()

    assert "yyyy" not in str(caught.value)
    assert caught.value.__context__ is None


def test_check_llm_script_prints_a_valid_fake_result(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    get_settings.cache_clear()

    assert main([]) == 0

    captured = capsys.readouterr()
    assert "provider=fake" in captured.err
    assert TriageResult.model_validate_json(captured.out) == DISCOUNT_TIER_TRIAGE


def test_check_llm_script_hides_provider_failures(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    get_settings.cache_clear()

    class Broken(FakeProvider):
        def generate_structured(self, prompt, schema, options=None):
            raise LLMResponseError("TriageResult rejected model output (1 validation error)")

    monkeypatch.setattr("scripts.check_llm.create_provider", lambda settings: Broken())

    assert main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "rejected model output" in captured.err
    assert SECRET not in captured.err


def test_check_llm_script_rejects_a_credentialed_base_url_without_echoing_it(monkeypatch, capsys):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://patchpilot:super-secret-db-password@localhost/patchpilot",
    )
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    get_settings.cache_clear()

    exit_code = main(["--base-url", "http://user:super-secret-token@127.0.0.1:11434"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "invalid LLM settings" in captured.err
    assert "super-secret-token" not in captured.err
    assert "super-secret-db-password" not in captured.err
    assert captured.out == ""


def test_check_llm_script_runs_offline_without_a_database(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    get_settings.cache_clear()

    assert main([]) == 0

    captured = capsys.readouterr()
    assert "provider=fake" in captured.err
    assert TriageResult.model_validate_json(captured.out) == DISCOUNT_TIER_TRIAGE
    assert "database" not in captured.err.casefold()
