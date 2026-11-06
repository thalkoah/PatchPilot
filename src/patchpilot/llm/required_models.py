"""Ollama models the current settings will call.

Names come from `LLM_PROVIDER`, `LLM_MODEL`, and `LLM_STAGE_MODELS`. A stage
that uses `fake` or `openai` is omitted. The same model on several stages is
printed once. `scripts/bootstrap.sh` pulls this list, so a new model tag does
not need a new install script.

    uv run python -m patchpilot.llm.required_models
"""

import sys

from pydantic import ValidationError

from patchpilot.config import LLMSettings
from patchpilot.llm.routing import parse_stage_models, provider_capabilities


def required_ollama_models(settings: LLMSettings) -> tuple[str, ...]:
    """Return the distinct Ollama models the configured stages will call."""
    choices = parse_stage_models(
        settings.llm_stage_models,
        default_provider=settings.llm_provider,
        default_model=settings.llm_model,
        capabilities_for=lambda provider: provider_capabilities(settings, provider),
    )
    names = {choice.model for choice in choices.values() if choice.provider == "ollama"}
    return tuple(sorted(names))


def main() -> int:
    try:
        settings = LLMSettings()
    except ValidationError as exc:
        message = "invalid model configuration"
        if exc.errors():
            message = str(exc.errors()[0].get("msg", message))
        print(message, file=sys.stderr)
        return 2
    for name in required_ollama_models(settings):
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
