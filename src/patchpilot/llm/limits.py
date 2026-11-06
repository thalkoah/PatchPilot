"""Bounds for model settings and provider requests."""

import re
from urllib.parse import urlsplit

MAX_LLM_RETRIES = 3
MAX_LLM_TIMEOUT_SECONDS = 600.0
MAX_PROMPT_CHARS = 32_000
MAX_MODEL_CONTENT_CHARS = 8_192
# Envelope around one structured object. Cloud responses stay inside this.
MAX_HTTP_BODY_BYTES = MAX_MODEL_CONTENT_CHARS + 4_096
# Ollama may attach a thinking trace next to a small JSON object. The trace is
# discarded. This bound is only the memory cap before the body is parsed.
MAX_OLLAMA_HTTP_BODY_BYTES = 512 * 1024

_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


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
