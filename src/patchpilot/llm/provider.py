"""The narrow model interface shared by the fake and Ollama providers."""

from typing import Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field

ModelT = TypeVar("ModelT", bound=BaseModel)


class GenerateOptions(BaseModel):
    """Per-call generation settings. The provider and model come from settings."""

    model_config = ConfigDict(extra="forbid")

    temperature: float = Field(default=0.0, ge=0.0, le=2.0)


class LLMProvider(Protocol):
    """One structured generation call. Implementations validate before returning."""

    def generate_structured(
        self,
        prompt: str,
        schema: type[ModelT],
        options: GenerateOptions | None = None,
    ) -> ModelT: ...

    def close(self) -> None: ...
