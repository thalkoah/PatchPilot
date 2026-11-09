"""Provider failures whose messages are safe to log."""


class LLMError(Exception):
    """A model call failed. The message omits model text and response bodies."""


class LLMResponseError(LLMError):
    """The response was missing, oversized, or rejected by the schema.

    `detail` names schema locations and error types for the log. It is not
    part of the message, and it does not include model text.
    """

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


def log_failure_reason(exc: BaseException) -> str:
    """Type, safe message, and schema locations when the parser recorded them."""
    text = f"{type(exc).__name__}: {exc}"
    detail = getattr(exc, "detail", "")
    if not isinstance(detail, str):
        return text
    detail = " ".join(detail.split())
    if not detail:
        return text
    return f"{text} [{detail}]"


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
