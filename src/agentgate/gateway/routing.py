"""Capability routing: capability -> model tier, with a fallback for escalation."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import yaml

from agentgate.common.config import get_settings


@dataclass(frozen=True)
class ModelSpec:
    name: str
    model_id: str
    tier: str
    price_in_per_1k: float
    price_out_per_1k: float
    max_output_tokens: int
    supports_tools: bool


@dataclass(frozen=True)
class Route:
    capability: str
    primary: ModelSpec
    fallback: ModelSpec
    cache: bool
    temperature: float
    prompt_version: str


class UnknownCapability(KeyError):
    pass


class Router:
    def __init__(self, raw: dict):
        self._models: dict[str, ModelSpec] = {}
        for name, m in raw["models"].items():
            spec = ModelSpec(name=name, **m)
            self._models[name] = spec
            self._models[spec.model_id] = spec
        self._routes: dict[str, Route] = {}
        for cap, r in raw["capabilities"].items():
            self._routes[cap] = Route(
                capability=cap,
                primary=self._models[r["model"]],
                fallback=self._models[r.get("fallback", r["model"])],
                cache=bool(r.get("cache", False)),
                temperature=float(r.get("temperature", 0.0)),
                prompt_version=str(r.get("prompt_version", "v1")),
            )
        self.escalation = raw.get("escalation", {"on_schema_failure": "fallback", "max_retries": 1})

    def route(self, capability: str) -> Route:
        try:
            return self._routes[capability]
        except KeyError as e:
            raise UnknownCapability(capability) from e

    def model(self, name_or_id: str) -> ModelSpec:
        return self._models[name_or_id]

    @property
    def capabilities(self) -> list[str]:
        return sorted(self._routes)


@lru_cache
def get_router() -> Router:
    with open(get_settings().config_dir / "capabilities.yaml") as f:
        return Router(yaml.safe_load(f))
