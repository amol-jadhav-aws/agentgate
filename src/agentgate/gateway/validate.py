"""Structured-output validation: parse the model's text as JSON and check it against a JSON schema."""

from __future__ import annotations

import json
import re
from typing import Any

from jsonschema import Draft202012Validator


class OutputInvalid(ValueError):
    pass


def extract_json(text: str) -> Any:
    """Models sometimes wrap JSON in prose or code fences; pull out the first JSON object/array."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise OutputInvalid("no JSON found in model output")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError as e:
                    raise OutputInvalid(f"malformed JSON: {e}") from e
    raise OutputInvalid("unterminated JSON in model output")


def validate_against(schema: dict[str, Any], text: str) -> Any:
    parsed = extract_json(text)
    errors = sorted(Draft202012Validator(schema).iter_errors(parsed), key=lambda e: list(e.path))
    if errors:
        msg = "; ".join(f"{'/'.join(str(p) for p in e.path) or '$'}: {e.message}" for e in errors[:5])
        raise OutputInvalid(f"schema violation: {msg}")
    return parsed
