"""Turn untrusted model text into a schema instance, or a typed error."""

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from patchpilot.llm.errors import LLMResponseError
from patchpilot.llm.limits import MAX_MODEL_CONTENT_CHARS, MAX_PROMPT_CHARS
from patchpilot.llm.provider import GenerateOptions, ModelT


def require_prompt(prompt: str) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt must be at most {MAX_PROMPT_CHARS} characters")
    return prompt


def require_schema(schema: type[ModelT]) -> type[ModelT]:
    if not isinstance(schema, type) or not issubclass(schema, BaseModel):
        raise TypeError("schema must be a Pydantic BaseModel subclass")
    return schema


def resolve_options(options: GenerateOptions | None) -> GenerateOptions:
    if options is None:
        return GenerateOptions()
    if not isinstance(options, GenerateOptions):
        raise TypeError("options must be GenerateOptions or None")
    return options


def parse_structured_output(content: str, schema: type[ModelT]) -> ModelT:
    """Validate model text. The raised error does not carry that text."""

    if not isinstance(content, str) or len(content) > MAX_MODEL_CONTENT_CHARS:
        raise LLMResponseError(f"{schema.__name__} rejected model output")
    text = content.strip()
    if not text:
        raise LLMResponseError(f"{schema.__name__} rejected model output")

    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        error_count = exc.error_count()
    noun = "validation error" if error_count == 1 else "validation errors"
    raise LLMResponseError(f"{schema.__name__} rejected model output ({error_count} {noun})")


def coerce_model(schema: type[ModelT], payload: BaseModel | Mapping[str, Any]) -> ModelT:
    """Validate a fixture through the same JSON parser as a model response."""

    data: Mapping[str, Any] | None
    if isinstance(payload, BaseModel):
        data = payload.model_dump(mode="json")
    elif isinstance(payload, Mapping):
        data = payload
    else:
        data = None

    encoded: str | None = None
    if data is not None:
        try:
            encoded = json.dumps(data)
        except (TypeError, ValueError):
            encoded = None
    if encoded is None:
        raise LLMResponseError(f"{schema.__name__} rejected model output")
    return parse_structured_output(encoded, schema)
