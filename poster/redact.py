"""Keep API keys and bearer tokens out of logs and committed error files."""

from __future__ import annotations

import os
import re

_SECRET_ENV = ("BUFFER_API_KEY", "GITHUB_TOKEN", "GH_TOKEN")
_BEARER = re.compile(r"Bearer\s+[A-Za-z0-9._\-+/=]+", re.IGNORECASE)
_BUFFER_KEY = re.compile(r"\bbuf_[A-Za-z0-9]+\b")


def redact(text: str, env: dict[str, str] | None = None) -> str:
    if not text:
        return text
    source = os.environ if env is None else env
    redacted = text
    for name in _SECRET_ENV:
        value = source.get(name) or ""
        if len(value) >= 6:
            redacted = redacted.replace(value, f"[REDACTED:{name}]")
    redacted = _BEARER.sub("Bearer [REDACTED]", redacted)
    redacted = _BUFFER_KEY.sub("[REDACTED:BUFFER_API_KEY]", redacted)
    return redacted
