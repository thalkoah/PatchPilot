"""Deterministic provider for tests and offline runs."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from patchpilot.llm.errors import LLMResponseError
from patchpilot.llm.parsing import coerce_model, require_prompt, require_schema, resolve_options
from patchpilot.llm.provider import GenerateOptions, ModelT
from patchpilot.llm.triage import Classification, Severity, TriageResult

Fixture = BaseModel | Mapping[str, Any]

DISCOUNT_TIER_TRIAGE = TriageResult(
    classification=Classification.BUG,
    severity=Severity.HIGH,
    summary="An unrecognized customer tier makes the discount endpoint return HTTP 500.",
    acceptance_criteria=[
        "An unknown customer tier returns HTTP 422.",
        "The response explains the validation error.",
        "A regression test covers an unrecognized tier.",
    ],
    confidence=0.75,
    needs_clarification=False,
)

NEEDS_CLARIFICATION_TRIAGE = TriageResult(
    classification=Classification.UNCLEAR,
    severity=Severity.LOW,
    summary="The report does not identify a reproducible failure.",
    acceptance_criteria=[
        "The reporter names the expected behavior and the actual behavior.",
    ],
    confidence=0.25,
    needs_clarification=True,
)

GENERIC_BUG_TRIAGE = TriageResult(
    classification=Classification.BUG,
    severity=Severity.MEDIUM,
    summary="The report describes a defect that needs a small, tested fix.",
    acceptance_criteria=[
        "The reported failure no longer occurs.",
        "A regression test covers the reported case.",
    ],
    confidence=0.5,
    needs_clarification=False,
)


@dataclass(frozen=True, slots=True)
class ProviderCall:
    prompt: str
    schema_name: str
    temperature: float


def builtin_triage(prompt: str) -> TriageResult:
    text = prompt.casefold()
    if "discount" in text and "tier" in text:
        return DISCOUNT_TIER_TRIAGE
    if "needs clarification" in text or "not enough information" in text:
        return NEEDS_CLARIFICATION_TRIAGE
    return GENERIC_BUG_TRIAGE


class FakeProvider:
    """Return schema-valid fixtures. The same prompt always returns the same value."""

    def __init__(self, fixtures: Mapping[str, Fixture] | None = None) -> None:
        self._fixtures = dict(fixtures or {})
        self.calls: list[ProviderCall] = []

    def generate_structured(
        self,
        prompt: str,
        schema: type[ModelT],
        options: GenerateOptions | None = None,
    ) -> ModelT:
        checked_prompt = require_prompt(prompt)
        checked_schema = require_schema(schema)
        resolved = resolve_options(options)
        self.calls.append(
            ProviderCall(
                prompt=checked_prompt,
                schema_name=checked_schema.__name__,
                temperature=resolved.temperature,
            )
        )
        if checked_prompt in self._fixtures:
            return coerce_model(checked_schema, self._fixtures[checked_prompt])
        if checked_schema is TriageResult:
            return coerce_model(checked_schema, builtin_triage(checked_prompt))
        raise LLMResponseError(f"FakeProvider has no fixture for {checked_schema.__name__}")

    def close(self) -> None:
        return None
