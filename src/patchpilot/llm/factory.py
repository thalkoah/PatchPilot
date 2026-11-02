"""Select the provider named by settings. The default is the offline fake."""

from patchpilot.config import LLMSettings
from patchpilot.llm.fake import FakeProvider
from patchpilot.llm.ollama import OllamaProvider
from patchpilot.llm.provider import LLMProvider


def create_provider(settings: LLMSettings) -> LLMProvider:
    if settings.llm_provider == "fake":
        return FakeProvider()
    return OllamaProvider(
        base_url=settings.ollama_base_url,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
