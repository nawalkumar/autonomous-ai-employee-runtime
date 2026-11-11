"""Redaction of secret-looking values before data leaves the API."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

REDACTED = "[redacted]"

_SENSITIVE_KEY = re.compile(
    r"pass(word|phrase|wd)|secret|token|api[_-]?key|authori[sz]ation|cookie|credential|"
    r"private[_-]?key|access[_-]?code",
    re.IGNORECASE,
)


class Redactor:
    """Masks values under sensitive keys and any configured secret strings."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)

    def text(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, REDACTED)
        return value

    def __call__(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: (
                    REDACTED
                    if isinstance(key, str) and _SENSITIVE_KEY.search(key)
                    else self(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value
