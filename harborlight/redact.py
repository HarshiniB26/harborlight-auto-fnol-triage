"""Remove restricted synthetic values before anything is logged or returned."""

import re

_PRIVATE = re.compile(r"SYNTHETIC-PRIVATE-[A-Za-z0-9_-]+")
_SECRET_KEYS = {"private_token", "api_key", "password", "secret", "authorization"}


def redact_text(value: str) -> str:
    return _PRIVATE.sub("[REDACTED]", value)


def redact(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if str(key).lower() in _SECRET_KEYS:
                cleaned[key] = "[REDACTED]"
            else:
                cleaned[key] = redact(item)
        return cleaned
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
