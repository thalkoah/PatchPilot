"""Token counts a provider actually returned. Absence stays unknown."""

from pydantic import BaseModel, ConfigDict, Field


class ReportedUsage(BaseModel):
    """Prompt and output tokens. Either side may be missing."""

    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


def reported_token(value: object) -> int | None:
    """Keep a non-negative integer. Anything else was not reported."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def take_reported_usage(provider: object) -> ReportedUsage | None:
    method = getattr(provider, "take_usage", None)
    if not callable(method):
        return None
    usage = method()
    if usage is None:
        return None
    if not isinstance(usage, ReportedUsage):
        return None
    if usage.prompt_tokens is None and usage.output_tokens is None:
        return None
    return usage
