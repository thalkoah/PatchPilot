"""Provider failures whose messages are safe to log."""


class LLMError(Exception):
    """A model call failed. The message omits model text and response bodies."""


class LLMResponseError(LLMError):
    """The response was missing, oversized, or rejected by the schema."""


class LLMTimeoutError(LLMError):
    """The provider did not answer before the configured timeout."""


class LLMTransportError(LLMError):
    """The provider was unreachable or returned a non-success status."""
