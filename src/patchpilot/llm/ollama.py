"""Ollama chat API with schema-constrained responses."""

import json
import logging
from typing import Any

import httpx2
from pydantic import BaseModel

from patchpilot.llm.errors import LLMResponseError, LLMTimeoutError, LLMTransportError
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

logger = logging.getLogger(__name__)


class OllamaProvider:
    """Call `/api/chat` and return only a schema-valid object.

    Invalid JSON and timeouts retry up to `max_retries` additional times.
    Other transport failures fail on the first attempt. Exception messages
    omit response bodies.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        client: httpx2.Client | None = None,
    ) -> None:
        if not 0 <= max_retries <= MAX_LLM_RETRIES:
            raise ValueError(f"max_retries must be between 0 and {MAX_LLM_RETRIES}")
        if not 0 < timeout_seconds <= MAX_LLM_TIMEOUT_SECONDS:
            raise ValueError(
                f"timeout_seconds must be greater than 0 and at most {MAX_LLM_TIMEOUT_SECONDS:g}"
            )
        self._base_url = normalize_ollama_base_url(base_url)
        self._model = normalize_model_name(model)
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._owns_client = client is None
        self._client = client or httpx2.Client(
            timeout=httpx2.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        )
        self._closed = False

    def generate_structured(
        self,
        prompt: str,
        schema: type[ModelT],
        options: GenerateOptions | None = None,
    ) -> ModelT:
        checked_prompt = require_prompt(prompt)
        checked_schema = require_schema(schema)
        resolved = resolve_options(options)
        attempts = self._max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                content = self._request_content(checked_prompt, checked_schema, resolved)
                result = parse_structured_output(content, checked_schema)
            except (LLMTimeoutError, LLMResponseError) as exc:
                if attempt == attempts:
                    logger.warning(
                        "ollama structured output failed after %s attempt(s): %s",
                        attempt,
                        type(exc).__name__,
                    )
                    raise
                logger.warning(
                    "ollama structured output attempt %s failed: %s",
                    attempt,
                    type(exc).__name__,
                )
                continue
            logger.info(
                "ollama structured output validated model=%s attempts=%s",
                self._model,
                attempt,
            )
            return result
        raise LLMTransportError("Ollama request ended without a result")

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
            "stream": False,
            "format": schema_document,
            "options": {"temperature": options.temperature},
        }
        try:
            response = self._client.post(
                f"{self._base_url}/api/chat",
                json=body,
                timeout=self._timeout_seconds,
            )
        except httpx2.TimeoutException as exc:
            raise LLMTimeoutError(
                f"Ollama request timed out after {self._timeout_seconds:g} seconds"
            ) from exc
        except httpx2.TransportError as exc:
            raise LLMTransportError("Ollama request failed before a response") from exc

        if response.status_code != 200:
            raise LLMTransportError(f"Ollama returned HTTP {response.status_code}")
        if len(response.content) > MAX_HTTP_BODY_BYTES:
            raise LLMResponseError("Ollama response was not a JSON object")

        payload: Any = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise LLMResponseError("Ollama response was not a JSON object")
        message = payload.get("message")
        if not isinstance(message, dict):
            raise LLMResponseError("Ollama response did not include a message")
        content = message.get("content")
        if not isinstance(content, str):
            raise LLMResponseError("Ollama response message content was not text")
        return content
