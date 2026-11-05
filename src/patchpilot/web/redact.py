"""Display redaction. Stored rows are already scrubbed; the page scrubs them again.

A connection string, a bearer token, an `sk-` key, or a configured secret of at
least eight characters is replaced before it reaches a template. Short strings
are left alone so a tiny password cannot erase ordinary words.
"""

import re
from urllib.parse import unquote, urlsplit

from patchpilot.config import Settings

_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+):([^@\s/]+)@")
_BEARER = re.compile(r"(?i)\bBearer\s+\S{8,}")
_SECRET_KEY = re.compile(r"\bsk-[A-Za-z0-9-]{8,}\b")
_MIN_MARKER = 8


def secret_markers(settings: Settings) -> tuple[str, ...]:
    """Secrets this process was configured with. They are not rendered."""
    found = [settings.database_url]
    password = urlsplit(settings.database_url).password
    if password:
        found.append(unquote(password))
    key = settings.llm_cloud_api_key
    if key is not None:
        found.append(key.get_secret_value())
    markers: list[str] = []
    for item in found:
        if len(item) >= _MIN_MARKER and item not in markers:
            markers.append(item)
    return tuple(markers)


def redact_text(value: str, markers: tuple[str, ...] = ()) -> str:
    """Return text safe to show. The replacement is the same for every secret."""
    text = value
    for marker in sorted(markers, key=len, reverse=True):
        if len(marker) >= _MIN_MARKER:
            text = text.replace(marker, "[redacted]")
    text = _USERINFO.sub(r"\1:[redacted]@", text)
    text = _BEARER.sub("Bearer [redacted]", text)
    return _SECRET_KEY.sub("[redacted]", text)
