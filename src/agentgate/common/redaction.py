"""Output redaction applied to every tool result before it reaches a model.

A tool result is data that will be pasted into a prompt. Account ids, access keys,
secrets and emails have no business there, so they are masked at the gateway, not in
each tool. Add a pattern here and every tool is covered.
"""

from __future__ import annotations

import re
from typing import Any

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws_access_key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("aws_secret_key", re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*\S+")),
    ("aws_account_id", re.compile(r"\b\d{12}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")),
    ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*")),
    ("secret_json", re.compile(r'(?i)("(?:password|secret|token|api_key)"\s*:\s*")[^"]*(")')),
]


def redact_text(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    for name, pat in PATTERNS:
        text, n = pat.subn(rf"\1[REDACTED:{name}]\2" if name == "secret_json" else f"[REDACTED:{name}]", text)
        if n:
            counts[name] = n
    return text, counts


def redact(obj: Any) -> tuple[Any, dict[str, int]]:
    """Walk dicts/lists/strings and redact every string leaf."""
    total: dict[str, int] = {}

    def _walk(o: Any) -> Any:
        if isinstance(o, str):
            out, counts = redact_text(o)
            for k, v in counts.items():
                total[k] = total.get(k, 0) + v
            return out
        if isinstance(o, dict):
            return {k: _walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [_walk(v) for v in o]
        return o

    return _walk(obj), total
