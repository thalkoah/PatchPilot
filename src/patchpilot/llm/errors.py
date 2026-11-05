"""Provider failures whose messages are safe to log."""


class LLMError(Exception):
    """A model call failed. The message omits model text and response bodies."""


class LLMResponseError(LLMError):
    """The response was missing, oversized, or rejected by the schema."""


class LLMTransientError(LLMError):
    """A later attempt may succeed. The caller waits before retrying."""


class LLMTimeoutError(LLMTransientError):
    """The provider did not answer before the configured timeout."""


class LLMTransportError(LLMError):
    """The provider was unreachable or returned a status that is not retried."""


class LLMPermanentError(LLMError):
    """Retrying will not succeed. The message does not include a response body."""


class LLMCapabilityError(LLMPermanentError):
    """The selected model does not advertise the capability this stage needs."""


class LLMBudgetError(LLMPermanentError):
    """A per-task budget is exhausted. The message names that budget."""


class LLMCircuitError(LLMPermanentError):
    """Invalid output recurred. The model is not called again for this stage."""
