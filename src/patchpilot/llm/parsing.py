"""Turn untrusted model text into a schema instance, or a typed error."""

import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from patchpilot.llm.errors import LLMResponseError
from patchpilot.llm.limits import MAX_MODEL_CONTENT_CHARS, MAX_PROMPT_CHARS
from patchpilot.llm.provider import GenerateOptions, ModelT

_KIND = re.compile(r"[a-z0-9_]{1,40}")
_MAX_DETAIL_ERRORS = 8
_MAX_DETAIL_CHARS = 360
_MAX_LOCATION_CHARS = 40
_MAX_MESSAGE_CHARS = 160
_MIN_INPUT_LEAK_CHARS = 8


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

    if not isinstance(content, str):
        raise LLMResponseError(f"{schema.__name__} rejected model output", detail="not text")
    if len(content) > MAX_MODEL_CONTENT_CHARS:
        raise LLMResponseError(
            f"{schema.__name__} rejected model output",
            detail=f"{len(content)} characters",
        )
    text = content.strip()
    if not text:
        raise LLMResponseError(f"{schema.__name__} rejected model output", detail="empty")

    detail = ""
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        error_count = exc.error_count()
        detail = _validation_detail(exc)
    noun = "validation error" if error_count == 1 else "validation errors"
    raise LLMResponseError(
        f"{schema.__name__} rejected model output ({error_count} {noun})",
        detail=detail,
    )


def _validation_detail(exc: ValidationError) -> str:
    """Field paths and error types. Values from the model text are left out."""
    errors = exc.errors()
    parts = [_error_detail(error) for error in errors[:_MAX_DETAIL_ERRORS]]
    remaining = len(errors) - len(parts)
    if remaining > 0:
        parts.append(f"{remaining} more")
    text = "; ".join(parts)
    if len(text) > _MAX_DETAIL_CHARS:
        return text[:_MAX_DETAIL_CHARS] + "…"
    return text


def _error_detail(error: Mapping[str, Any]) -> str:
    location = _location(error.get("loc"))
    kind = _kind(error.get("type"))
    message = _safe_message(error.get("msg"), error.get("input"))
    if message is None:
        return f"{location}: {kind}"
    return f"{location}: {kind}: {message}"


def _location(loc: object) -> str:
    if not isinstance(loc, tuple) or not loc:
        return "body"
    parts: list[str] = []
    for part in loc:
        if isinstance(part, int):
            parts.append(str(part))
            continue
        if not isinstance(part, str) or not part or any(ord(character) < 32 for character in part):
            parts.append("?")
            continue
        parts.append(part if len(part) <= _MAX_LOCATION_CHARS else part[:_MAX_LOCATION_CHARS] + "…")
    return ".".join(parts) or "body"


def _kind(value: object) -> str:
    if isinstance(value, str) and _KIND.fullmatch(value):
        return value
    return "invalid"


def _safe_message(message: object, raw_input: object) -> str | None:
    if not isinstance(message, str):
        return None
    text = " ".join(message.split())
    if not text or len(text) > _MAX_MESSAGE_CHARS:
        return None
    if isinstance(raw_input, str) and len(raw_input) >= _MIN_INPUT_LEAK_CHARS and raw_input in text:
        return None
    return text


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
