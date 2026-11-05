"""Per-stage provider and model. Cloud is built only when a stage selects it."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from patchpilot.config import LLMSettings
from patchpilot.llm.backoff import MAX_BACKOFF_SECONDS
from patchpilot.llm.budget import BudgetSeed, TaskBudget
from patchpilot.llm.capabilities import (
    MODEL_STAGES,
    ModelCapability,
    parse_capabilities,
    require_capabilities,
)
from patchpilot.llm.errors import LLMPermanentError
from patchpilot.llm.fake import FakeProvider
from patchpilot.llm.limits import normalize_model_name
from patchpilot.llm.local_gate import LocalAdvisoryLock, LocalGate
from patchpilot.llm.meter import CallMeter, ModelCallDraft
from patchpilot.llm.ollama import OllamaProvider
from patchpilot.llm.openai_compatible import OpenAICompatibleProvider
from patchpilot.llm.pricing import PricingTable
from patchpilot.llm.provider import GenerateOptions, LLMProvider, ModelT

ProviderName = Literal["fake", "ollama", "openai"]
_PROVIDERS = frozenset({"fake", "ollama", "openai"})


@dataclass(frozen=True, slots=True)
class StageChoice:
    """The provider and model one stage is allowed to call."""

    provider: ProviderName
    model: str
    capabilities: frozenset[ModelCapability]
    local: bool


@dataclass(frozen=True, slots=True)
class UsageControl:
    """Shared provider instances and the limits applied to each task."""

    router: "StageRouter"
    pricing: PricingTable | None
    max_model_calls: int
    max_tool_calls: int
    max_tokens: int
    max_runtime_seconds: float
    max_parallelism: int
    max_repairs: int
    database_url: str | None
    gate: LocalGate


class StageModels:
    """The providers for one task. Each call is metered."""

    def __init__(self, router: "StageRouter", meter: CallMeter) -> None:
        self._router = router
        self._meter = meter

    def for_stage(self, stage: str) -> LLMProvider:
        return _StageProvider(self._router, self._meter, stage)

    def take_records(self) -> list[ModelCallDraft]:
        return self._meter.take_records()


class StageRouter:
    """One provider instance per provider and model pair."""

    def __init__(
        self,
        choices: dict[str, StageChoice],
        providers: dict[tuple[str, str], LLMProvider],
    ) -> None:
        self._choices = choices
        self._providers = providers

    @property
    def choices(self) -> dict[str, StageChoice]:
        return dict(self._choices)

    @property
    def uses_local(self) -> bool:
        return any(choice.local for choice in self._choices.values())

    def bind(
        self,
        *,
        task_id: UUID,
        budget: TaskBudget,
        pricing: PricingTable | None,
        gate: LocalGate,
        advisory: bool,
        database_url: str | None,
    ) -> StageModels:
        factory = None
        if advisory and database_url is not None and self.uses_local:
            url = database_url

            def factory() -> LocalAdvisoryLock:
                return LocalAdvisoryLock(url)

        meter = CallMeter(
            task_id=task_id,
            budget=budget,
            pricing=pricing,
            gate=gate,
            advisory=factory,
        )
        return StageModels(self, meter)

    def close(self) -> None:
        for provider in self._providers.values():
            provider.close()

    def fallback(self) -> LLMProvider:
        """The triage provider. Nodes use `StageModels` when a task is bound."""
        choice = self._choices["triage"]
        return self._providers[(choice.provider, choice.model)]


class _StageProvider:
    """LLMProvider for one stage. The graph still calls generate_structured."""

    def __init__(self, router: StageRouter, meter: CallMeter, stage: str) -> None:
        self._router = router
        self._meter = meter
        self._stage = stage

    def generate_structured(
        self,
        prompt: str,
        schema: type[ModelT],
        options: GenerateOptions | None = None,
    ) -> ModelT:
        choice = self._router._choices.get(self._stage)
        if choice is None:
            raise LLMPermanentError("model stage is not routable")
        provider = self._router._providers[(choice.provider, choice.model)]
        return self._meter.run(
            stage=self._stage,
            provider_name=choice.provider,
            model=choice.model,
            capabilities=choice.capabilities,
            local=choice.local,
            provider=provider,
            prompt=prompt,
            schema=schema,
            options=options,
        )

    def close(self) -> None:
        return None


def parse_stage_models(
    value: str,
    *,
    default_provider: ProviderName,
    default_model: str,
    capabilities_for: Callable[[ProviderName], frozenset[ModelCapability]],
) -> dict[str, StageChoice]:
    """Parse `stage=provider:model` pairs. An empty string uses the default."""
    choices = {
        stage: _choice(default_provider, default_model, capabilities_for) for stage in MODEL_STAGES
    }
    if not isinstance(value, str):
        raise ValueError("llm_stage_models must be a comma-separated list")
    stripped = value.strip()
    if stripped == "":
        return choices
    seen: set[str] = set()
    for part in stripped.split(","):
        item = part.strip()
        if "=" not in item:
            raise ValueError("llm_stage_models entries must be stage=provider:model")
        stage, spec = item.split("=", 1)
        stage = stage.strip()
        if stage not in MODEL_STAGES or stage in seen:
            raise ValueError("llm_stage_models names each model stage at most once")
        seen.add(stage)
        provider_name, model = _split_provider_model(spec)
        choices[stage] = _choice(provider_name, model, capabilities_for)
    return choices


def build_router(settings: LLMSettings) -> StageRouter:
    """Create the providers the configured stages need. Cloud stays unbuilt otherwise."""
    choices = parse_stage_models(
        settings.llm_stage_models,
        default_provider=settings.llm_provider,
        default_model=settings.llm_model,
        capabilities_for=lambda provider: provider_capabilities(settings, provider),
    )
    for stage, choice in choices.items():
        require_capabilities(stage, choice.capabilities)
        if choice.provider == "openai" and not settings.llm_cloud_enabled:
            raise LLMPermanentError("Cloud model calls are disabled")
    providers: dict[tuple[str, str], LLMProvider] = {}
    for choice in choices.values():
        key = (choice.provider, choice.model)
        if key in providers:
            continue
        providers[key] = _build_provider(settings, choice)
    return StageRouter(choices, providers)


def budget_for(settings: LLMSettings, seed: BudgetSeed | None = None) -> TaskBudget:
    """The task budget from settings, plus any calls already stored."""
    loaded = seed if seed is not None else BudgetSeed()
    return TaskBudget.from_seed(
        loaded,
        max_model_calls=settings.task_max_model_calls,
        max_tool_calls=settings.task_max_tool_calls,
        max_tokens=settings.task_max_tokens,
        max_runtime_seconds=settings.task_max_runtime_seconds,
        max_parallelism=settings.task_max_parallelism,
        max_repairs=settings.task_max_repairs,
    )


def _choice(
    provider: ProviderName,
    model: str,
    capabilities_for: Callable[[ProviderName], frozenset[ModelCapability]],
) -> StageChoice:
    checked = normalize_model_name(model)
    return StageChoice(
        provider=provider,
        model=checked,
        capabilities=capabilities_for(provider),
        local=provider == "ollama",
    )


def _split_provider_model(spec: str) -> tuple[ProviderName, str]:
    text = spec.strip()
    if ":" not in text:
        raise ValueError("llm_stage_models entries must be stage=provider:model")
    provider, model = text.split(":", 1)
    provider = provider.strip()
    model = model.strip()
    if provider not in _PROVIDERS or model == "":
        raise ValueError("llm_stage_models entries must be stage=provider:model")
    return cast(ProviderName, provider), model


def provider_capabilities(
    settings: LLMSettings,
    provider: ProviderName,
) -> frozenset[ModelCapability]:
    if provider == "fake":
        return frozenset({ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOL_CALLING})
    if provider == "ollama":
        caps = {ModelCapability.STRUCTURED_OUTPUT}
        if settings.ollama_tool_calling:
            caps.add(ModelCapability.TOOL_CALLING)
        return frozenset(caps)
    return parse_capabilities(settings.llm_cloud_capabilities)


def _build_provider(settings: LLMSettings, choice: StageChoice) -> LLMProvider:
    if choice.provider == "fake":
        return FakeProvider()
    if choice.provider == "ollama":
        return OllamaProvider(
            base_url=settings.ollama_base_url,
            model=choice.model,
            timeout_seconds=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            backoff_seconds=settings.llm_retry_backoff_seconds,
            tool_calling=settings.ollama_tool_calling,
        )
    if not settings.llm_cloud_enabled or settings.llm_cloud_base_url is None:
        raise LLMPermanentError("Cloud model calls are disabled")
    key = settings.llm_cloud_api_key
    if key is None:
        raise LLMPermanentError("Cloud model calls are disabled")
    return OpenAICompatibleProvider(
        enabled=True,
        base_url=settings.llm_cloud_base_url,
        api_key=key.get_secret_value(),
        model=choice.model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        capabilities=choice.capabilities,
        backoff_seconds=settings.llm_retry_backoff_seconds,
    )


def clamp_backoff(value: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("llm_retry_backoff_seconds must be a number of seconds")
    backoff = float(value)
    if backoff < 0 or backoff > MAX_BACKOFF_SECONDS:
        raise ValueError("llm_retry_backoff_seconds must be from 0 to 8")
    return backoff
