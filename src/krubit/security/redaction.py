"""Credential redaction applied before data crosses a durable boundary."""

from __future__ import annotations

import re

from krubit.domain.models import JSONValue

_DISCORD_TOKEN = re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}\b")
_ASSIGNMENT_SECRET = re.compile(
    r"(?i)\b(api[_ -]?key|bot[_ -]?token|access[_ -]?token|password|secret)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_SENSITIVE_KEY = re.compile(
    r"(?i)^(api[_ -]?key|bot[_ -]?token|access[_ -]?token|password|secret)$"
)


def _redact_text(value: str) -> str:
    value = _DISCORD_TOKEN.sub("[REDACTED_DISCORD_TOKEN]", value)
    return _ASSIGNMENT_SECRET.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value
    )


def redact(value: JSONValue) -> JSONValue:
    """Return a recursively redacted JSON-compatible value."""

    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEY.fullmatch(str(key)) else redact(item)
            for key, item in value.items()
        }
    return value
