"""Tool implementations. Each module exposes `run(session, args, run_id) -> dict`.

`session` is a boto3 Session with the tool's own role (aws mode) or None (local mode,
where the tool talks to the in-memory fake world in `fakeaws.py`). Registration is
by name: the manifest name must match a module here or the tool gateway refuses to start.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

ToolFn = Callable[[Any, dict[str, Any], str], dict[str, Any]]

_REGISTRY: dict[str, ToolFn] = {}


def get_tool(name: str) -> ToolFn:
    if name not in _REGISTRY:
        module = importlib.import_module(f"agentgate.toolgateway.tools.{name}")
        _REGISTRY[name] = module.run
    return _REGISTRY[name]
