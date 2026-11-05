"""Select the provider named by settings. The default is the offline fake."""

from patchpilot.config import LLMSettings
from patchpilot.llm.errors import LLMPermanentError
from patchpilot.llm.fake import FakeProvider
from patchpilot.llm.ollama import OllamaProvider
from patchpilot.llm.openai_compatible import OpenAICompatibleProvider
from patchpilot.llm.provider import LLMProvider
from patchpilot.llm.routing import provider_capabilities


def create_provider(settings: LLMSettings) -> LLMProvider:
    if settings.llm_provider == "fake":
        return FakeProvider()
    if settings.llm_provider == "ollama":
        return OllamaProvider(
            base_url=settings.ollama_base_url,
            model=settings.llm_model,
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
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        capabilities=provider_capabilities(settings, "openai"),
        backoff_seconds=settings.llm_retry_backoff_seconds,
    )
