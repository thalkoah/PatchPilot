"""What a model may be asked to do. A missing capability fails before any call."""

from enum import StrEnum


class ModelCapability(StrEnum):
    """The two model interfaces this process knows how to ask for."""

    STRUCTURED_OUTPUT = "structured_output"
    TOOL_CALLING = "tool_calling"


# Every current stage asks for a schema-valid object. The investigator's tools
# are selected from that object. Native tool calling is a separate capability
# and is refused until a provider advertises it.
STAGE_CAPABILITIES: dict[str, frozenset[ModelCapability]] = {
    "triage": frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    "investigate": frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    "plan": frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    "develop": frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    "review": frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    "supervisor": frozenset({ModelCapability.STRUCTURED_OUTPUT}),
}

MODEL_STAGES = tuple(STAGE_CAPABILITIES)


def parse_capabilities(value: str) -> frozenset[ModelCapability]:
    """Parse a comma-separated capability list. Blank means none."""
    if not isinstance(value, str):
        raise ValueError("model capabilities must be a comma-separated list")
    stripped = value.strip()
    if stripped == "":
        return frozenset()
    found: set[ModelCapability] = set()
    for part in stripped.split(","):
        name = part.strip()
        if name == "":
            raise ValueError("model capabilities must be a comma-separated list")
        try:
            found.add(ModelCapability(name))
        except ValueError:
            raise ValueError(
                "model capabilities must name structured_output or tool_calling"
            ) from None
    return frozenset(found)


def require_capabilities(
    stage: str,
    offered: frozenset[ModelCapability],
) -> None:
    """Raise when `offered` does not cover what `stage` needs. No model is called."""
    from patchpilot.llm.errors import LLMCapabilityError

    required = STAGE_CAPABILITIES.get(stage)
    if required is None:
        raise LLMCapabilityError("model stage is not routable")
    missing = required - offered
    if missing:
        names = ", ".join(sorted(item.value for item in missing))
        raise LLMCapabilityError(f"model cannot provide {names}")


def require_capability(
    offered: frozenset[ModelCapability],
    capability: ModelCapability,
) -> None:
    """Raise when a direct call needs a capability the model does not advertise."""
    from patchpilot.llm.errors import LLMCapabilityError

    if capability not in offered:
        raise LLMCapabilityError(f"model cannot provide {capability.value}")
