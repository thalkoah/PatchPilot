"""OpenAI-compatible chat completions. The client exists only when cloud is enabled."""

import json
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx2
from pydantic import BaseModel

from patchpilot.llm.backoff import retry_pause
from patchpilot.llm.capabilities import ModelCapability, require_capability
from patchpilot.llm.errors import (
    LLMPermanentError,
    LLMResponseError,
    LLMTimeoutError,
    LLMTransientError,
    LLMTransportError,
)
from patchpilot.llm.limits import (
    MAX_HTTP_BODY_BYTES,
    MAX_LLM_RETRIES,
    MAX_LLM_TIMEOUT_SECONDS,
    normalize_model_name,
    normalize_ollama_base_url,
)
from patchpilot.llm.parsing import (
    parse_structured_output,
    require_prompt,
    require_schema,
    resolve_options,
)
from patchpilot.llm.provider import GenerateOptions, ModelT
from patchpilot.llm.usage_report import ReportedUsage, reported_token

logger = logging.getLogger(__name__)

_TRANSIENT_STATUS = frozenset({408, 429, 502, 503, 504})


class OpenAICompatibleProvider:
    """POST `/chat/completions` and return only a schema-valid object.

    Construction refuses when cloud calls are disabled, and it does not open a
    socket in that case. The API key is sent as a bearer token and is not logged.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        capabilities: frozenset[ModelCapability],
        backoff_seconds: float = 0.0,
        sleeper: Callable[[float], None] = time.sleep,
        client: httpx2.Client | None = None,
    ) -> None:
        if enabled is not True:
            raise LLMPermanentError("Cloud model calls are disabled")
        if not isinstance(api_key, str) or api_key.strip() == "":
            raise LLMPermanentError("Cloud model calls are disabled")
        if not 0 <= max_retries <= MAX_LLM_RETRIES:
            raise ValueError(f"max_retries must be between 0 and {MAX_LLM_RETRIES}")
        if not 0 < timeout_seconds <= MAX_LLM_TIMEOUT_SECONDS:
            raise ValueError(
                f"timeout_seconds must be greater than 0 and at most {MAX_LLM_TIMEOUT_SECONDS:g}"
            )
        if not 0 <= backoff_seconds <= 8:
            raise ValueError("backoff_seconds must be from 0 to 8")
        self._base_url = normalize_ollama_base_url(base_url)
        self._api_key = api_key
        self._model = normalize_model_name(model)
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._capabilities = frozenset(capabilities)
        self._backoff_seconds = backoff_seconds
        self._sleeper = sleeper
        self._owns_client = client is None
        self._client = client or httpx2.Client(
            timeout=httpx2.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        )
        self._closed = False
        self._last_usage: ReportedUsage | None = None

    @property
    def capabilities(self) -> frozenset[ModelCapability]:
        return self._capabilities

    @property
    def model(self) -> str:
        return self._model

    def generate_structured(
        self,
        prompt: str,
        schema: type[ModelT],
        options: GenerateOptions | None = None,
    ) -> ModelT:
        require_capability(self._capabilities, ModelCapability.STRUCTURED_OUTPUT)
        checked_prompt = require_prompt(prompt)
        checked_schema = require_schema(schema)
        resolved = resolve_options(options)
        attempts = self._max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                content = self._request_content(checked_prompt, checked_schema, resolved)
                result = parse_structured_output(content, checked_schema)
            except (LLMTimeoutError, LLMResponseError, LLMTransientError) as exc:
                if attempt == attempts:
                    logger.warning(
                        "cloud structured output failed after %s attempt(s): %s",
                        attempt,
                        type(exc).__name__,
                    )
                    raise
                logger.warning(
                    "cloud structured output attempt %s failed: %s",
                    attempt,
                    type(exc).__name__,
                )
                retry_pause(attempt, self._backoff_seconds, self._sleeper)
                continue
            logger.info(
                "cloud structured output validated model=%s attempts=%s",
                self._model,
                attempt,
            )
            return result
        raise LLMTransportError("Cloud request ended without a result")

    def take_usage(self) -> ReportedUsage | None:
        usage = self._last_usage
        self._last_usage = None
        return usage

    def close(self) -> None:
        if self._owns_client and not self._closed:
            self._closed = True
            self._client.close()

    def _request_content(
        self,
        prompt: str,
        schema: type[BaseModel],
        options: GenerateOptions,
    ) -> str:
        schema_document = schema.model_json_schema()
        body = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Reply with one JSON object that matches the schema. "
                        "Do not include prose or markdown.\n"
                        + json.dumps(schema_document, separators=(",", ":"), sort_keys=True)
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": options.temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": schema_document,
                },
            },
        }
        try:
            response = self._client.post(
                f"{self._base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout_seconds,
            )
        except httpx2.TimeoutException as exc:
            raise LLMTimeoutError(
                f"Cloud request timed out after {self._timeout_seconds:g} seconds"
            ) from exc
        except httpx2.TransportError as exc:
            raise LLMTransportError("Cloud request failed before a response") from exc

        if response.status_code in _TRANSIENT_STATUS:
            raise LLMTransientError(f"Cloud model returned HTTP {response.status_code}")
        if response.status_code != 200:
            raise LLMPermanentError(f"Cloud model returned HTTP {response.status_code}")
        if len(response.content) > MAX_HTTP_BODY_BYTES:
            raise LLMResponseError("Cloud response was not a JSON object")
        self._last_usage = _usage_from_payload(response)
        return _message_text(response)


def _message_text(response: httpx2.Response) -> str:
    payload: Any = None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise LLMResponseError("Cloud response was not a JSON object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMResponseError("Cloud response did not include a message")
    first = choices[0]
    if not isinstance(first, dict):
        raise LLMResponseError("Cloud response did not include a message")
    message = first.get("message")
    if not isinstance(message, dict):
        raise LLMResponseError("Cloud response did not include a message")
    content = message.get("content")
    if not isinstance(content, str):
        raise LLMResponseError("Cloud response message content was not text")
    return content


def _usage_from_payload(response: httpx2.Response) -> ReportedUsage | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = reported_token(usage.get("prompt_tokens"))
    output = reported_token(usage.get("completion_tokens"))
    if prompt is None and output is None:
        return None
    return ReportedUsage(prompt_tokens=prompt, output_tokens=output)
