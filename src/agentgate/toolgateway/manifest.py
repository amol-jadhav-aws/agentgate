"""Tool manifest loaded from config/tools.yaml. A tool not in the manifest does not exist."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from agentgate.common.config import get_settings


class ToolNotFound(KeyError):
    pass


class ArgumentsInvalid(ValueError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    risk_tier: int
    cost_usd: float
    role_arn: str
    timeout_seconds: int
    input_schema: dict[str, Any]
    constraints: dict[str, Any] = field(default_factory=dict)

    def validate(self, args: dict[str, Any]) -> None:
        errors = sorted(Draft202012Validator(self.input_schema).iter_errors(args), key=lambda e: list(e.path))
        if errors:
            msg = "; ".join(f"{'/'.join(str(p) for p in e.path) or '$'}: {e.message}" for e in errors[:5])
            raise ArgumentsInvalid(f"{self.name}: {msg}")

    def catalogue_entry(self) -> dict[str, Any]:
        """What an agent sees: name, description, schema, tier. Never the role ARN."""
        return {
            "name": self.name,
            "description": self.description,
            "risk_tier": self.risk_tier,
            "input_schema": self.input_schema,
        }


class Manifest:
    def __init__(self, raw: dict[str, Any]):
        self.tools: dict[str, ToolSpec] = {}
        for name, t in raw["tools"].items():
            self.tools[name] = ToolSpec(
                name=name,
                description=t["description"],
                risk_tier=int(t["risk_tier"]),
                cost_usd=float(t.get("cost_usd", 0.0)),
                role_arn=t["role_arn"],
                timeout_seconds=int(t.get("timeout_seconds", 15)),
                input_schema=t["input_schema"],
                constraints=t.get("constraints", {}),
            )
        self.grants: dict[str, list[str]] = {k: list(v) for k, v in raw.get("grants", {}).items()}
        for tenant, names in self.grants.items():
            for n in names:
                if n not in self.tools:
                    raise ValueError(f"grant for {tenant} names unknown tool {n}")

    def get(self, name: str) -> ToolSpec:
        try:
            return self.tools[name]
        except KeyError as e:
            raise ToolNotFound(name) from e

    def granted(self, tenant: str) -> list[ToolSpec]:
        return [self.tools[n] for n in self.grants.get(tenant, [])]

    def catalogue(self, tenant: str) -> list[dict[str, Any]]:
        return [t.catalogue_entry() for t in self.granted(tenant)]


@lru_cache
def get_manifest() -> Manifest:
    with open(get_settings().config_dir / "tools.yaml") as f:
        return Manifest(yaml.safe_load(f))
