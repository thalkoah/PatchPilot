"""Bounds for model settings and provider requests."""

import re
from urllib.parse import urlsplit

MAX_LLM_RETRIES = 3
MAX_LLM_TIMEOUT_SECONDS = 1800.0
MAX_PROMPT_CHARS = 32_000
MAX_MODEL_CONTENT_CHARS = 8_192
# Envelope around one structured object. Cloud responses stay inside this.
MAX_HTTP_BODY_BYTES = MAX_MODEL_CONTENT_CHARS + 4_096
# Ollama may attach a thinking trace next to a small JSON object. The trace is
# discarded. This bound is only the memory cap before the body is parsed.
MAX_OLLAMA_HTTP_BODY_BYTES = 512 * 1024
# The local runner loaded the coder model with this context. Generating past
# the leftover tokens shifts the window and drops the start of the JSON.
OLLAMA_NUM_CTX = 4_096
OLLAMA_CONTEXT_MARGIN = 32

_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


def ollama_num_predict(text_chars: int) -> int:
    """Tokens the answer may take without sliding a 4096-token window.

    Two characters count as one token, so a dense prompt cannot overflow the
    window. A prompt that already fills it still gets one token.
    """
    if isinstance(text_chars, bool) or not isinstance(text_chars, int) or text_chars < 0:
        raise ValueError("text_chars must be a non-negative integer")
    prompt_tokens = (text_chars + 1) // 2
    room = OLLAMA_NUM_CTX - OLLAMA_CONTEXT_MARGIN - prompt_tokens
    if room < 1:
        return 1
    return room


def normalize_model_name(value: str) -> str:
    if _MODEL_NAME.fullmatch(value) is None:
        raise ValueError("llm_model contains unsupported characters")
    return value


def normalize_ollama_base_url(value: str) -> str:
    stripped = value.strip()
    parsed = urlsplit(stripped)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("ollama_base_url must be an absolute http or https URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("ollama_base_url must not include credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("ollama_base_url must not include a query or fragment")
    return stripped.rstrip("/")
