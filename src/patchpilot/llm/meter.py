"""Time one structured call, enforce the task budget, and keep a usage record.

The record stores provider, model, latency, and reported tokens. It does not
store the prompt or the model text. Cost is filled only from a verified price.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from patchpilot.llm.budget import TaskBudget
from patchpilot.llm.capabilities import require_capabilities
from patchpilot.llm.errors import (
    LLMBudgetError,
    LLMCapabilityError,
    LLMCircuitError,
    LLMError,
    LLMResponseError,
)
from patchpilot.llm.local_gate import LocalAdvisoryLock, LocalGate
from patchpilot.llm.pricing import PricingTable, estimate_cost_usd
from patchpilot.llm.provider import GenerateOptions, LLMProvider, ModelT
from patchpilot.llm.usage_report import ReportedUsage, take_reported_usage

OUTCOME_SUCCESS = "success"
OUTCOME_TRANSIENT = "transient_error"
OUTCOME_PERMANENT = "permanent_error"
OUTCOME_INVALID = "invalid_output"
OUTCOME_BUDGET = "budget_exhausted"
OUTCOME_CIRCUIT = "circuit_open"
OUTCOME_CAPABILITY = "capability_rejected"


@dataclass(frozen=True, slots=True)
class ModelCallDraft:
    """One finished or refused call. The repository inserts this row."""

    task_id: UUID
    stage: str
    provider: str
    model: str
    prompt_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    estimated_cost_usd: Decimal | None
    cost_known: bool
    outcome: str


def _is_token_budget(exc: LLMBudgetError) -> bool:
    return bool(exc.args) and exc.args[0] == "token budget is exhausted"


def classify_outcome(exc: BaseException) -> str:
    """Map a provider error onto a stored outcome. The class is the message."""
    from patchpilot.llm.errors import LLMTimeoutError, LLMTransientError

    if isinstance(exc, LLMCircuitError):
        return OUTCOME_CIRCUIT
    if isinstance(exc, LLMBudgetError):
        return OUTCOME_BUDGET
    if isinstance(exc, LLMCapabilityError):
        return OUTCOME_CAPABILITY
    if isinstance(exc, LLMResponseError):
        return OUTCOME_INVALID
    if isinstance(exc, (LLMTimeoutError, LLMTransientError)):
        return OUTCOME_TRANSIENT
    return OUTCOME_PERMANENT


class CallMeter:
    """Budget, circuit, and usage for the calls of one task."""

    def __init__(
        self,
        *,
        task_id: UUID,
        budget: TaskBudget,
        pricing: PricingTable | None,
        gate: LocalGate,
        advisory: Callable[[], LocalAdvisoryLock] | None = None,
    ) -> None:
        self._task_id = task_id
        self._budget = budget
        self._pricing = pricing
        self._gate = gate
        self._advisory = advisory
        self.records: list[ModelCallDraft] = []

    def take_records(self) -> list[ModelCallDraft]:
        found = list(self.records)
        self.records.clear()
        return found

    def run(
        self,
        *,
        stage: str,
        provider_name: str,
        model: str,
        capabilities: frozenset,
        local: bool,
        provider: LLMProvider,
        prompt: str,
        schema: type[ModelT],
        options: GenerateOptions | None,
    ) -> ModelT:
        started = time.perf_counter()
        try:
            require_capabilities(stage, capabilities)
            self._budget.ensure_circuit(stage, provider_name, model)
        except LLMError as exc:
            self._record(
                stage=stage,
                provider_name=provider_name,
                model=model,
                started=started,
                usage=None,
                outcome=classify_outcome(exc),
            )
            raise
        try:
            self._budget.begin_model_call()
        except LLMError as exc:
            self._record(
                stage=stage,
                provider_name=provider_name,
                model=model,
                started=started,
                usage=None,
                outcome=classify_outcome(exc),
            )
            raise

        held_local = False
        lock: LocalAdvisoryLock | None = None
        try:
            if local:
                self._gate.acquire()
                held_local = True
                if self._advisory is not None:
                    lock = self._advisory()
            return self._invoke(
                stage=stage,
                provider_name=provider_name,
                model=model,
                provider=provider,
                prompt=prompt,
                schema=schema,
                options=options,
                started=started,
            )
        except LLMBudgetError as exc:
            if _is_token_budget(exc):
                raise
            self._budget.abort_model_call()
            self._record(
                stage=stage,
                provider_name=provider_name,
                model=model,
                started=started,
                usage=None,
                outcome=OUTCOME_BUDGET,
            )
            raise
        finally:
            if lock is not None:
                lock.release()
            if held_local:
                self._gate.release()

    def _invoke(
        self,
        *,
        stage: str,
        provider_name: str,
        model: str,
        provider: LLMProvider,
        prompt: str,
        schema: type[ModelT],
        options: GenerateOptions | None,
        started: float,
    ) -> ModelT:
        try:
            result = provider.generate_structured(prompt, schema, options)
        except LLMError as exc:
            self._budget.finish_model_call(
                stage=stage,
                provider=provider_name,
                model=model,
                prompt_tokens=None,
                output_tokens=None,
                invalid_output=isinstance(exc, LLMResponseError),
                succeeded=False,
            )
            self._record(
                stage=stage,
                provider_name=provider_name,
                model=model,
                started=started,
                usage=None,
                outcome=classify_outcome(exc),
            )
            raise
        usage = take_reported_usage(provider)
        try:
            self._budget.finish_model_call(
                stage=stage,
                provider=provider_name,
                model=model,
                prompt_tokens=None if usage is None else usage.prompt_tokens,
                output_tokens=None if usage is None else usage.output_tokens,
                invalid_output=False,
                succeeded=True,
            )
        except LLMBudgetError:
            self._record(
                stage=stage,
                provider_name=provider_name,
                model=model,
                started=started,
                usage=usage,
                outcome=OUTCOME_BUDGET,
            )
            raise
        self._record(
            stage=stage,
            provider_name=provider_name,
            model=model,
            started=started,
            usage=usage,
            outcome=OUTCOME_SUCCESS,
        )
        return result

    def _record(
        self,
        *,
        stage: str,
        provider_name: str,
        model: str,
        started: float,
        usage: ReportedUsage | None,
        outcome: str,
    ) -> None:
        prompt_tokens = None if usage is None else usage.prompt_tokens
        output_tokens = None if usage is None else usage.output_tokens
        cost = estimate_cost_usd(
            self._pricing,
            provider=provider_name,
            model=model,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
        )
        elapsed = time.perf_counter() - started
        latency_ms = max(0, int(elapsed * 1000))
        self.records.append(
            ModelCallDraft(
                task_id=self._task_id,
                stage=stage,
                provider=provider_name,
                model=model,
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                estimated_cost_usd=cost,
                cost_known=cost is not None,
                outcome=outcome,
            )
        )
