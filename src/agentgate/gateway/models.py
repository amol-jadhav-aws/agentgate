"""Request and response contracts for the inference gateway."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    capability: str = Field(
        description="What you need done, e.g. classify, extract_json, propose_action. Never a model id."
    )
    messages: list[Message]
    feature: str = Field(
        default="default", description="Cost allocation tag: which feature of the tenant is paying."
    )
    run_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex,
        description="One id per agent run; shared with traces and CloudTrail.",
    )
    output_schema: dict[str, Any] | None = Field(
        default=None,
        description="JSON schema the answer must satisfy. Validated, one retry on fallback model.",
    )
    context_hash: str | None = Field(
        default=None,
        description="Hash of retrieved context; part of the cache key so a changed KB never serves a stale answer.",
    )
    max_tokens: int | None = None
    temperature: float | None = None
    bypass_cache: bool = False


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    cost_usd: float


class ChatResponse(BaseModel):
    run_id: str
    capability: str
    model: str
    content: str
    parsed: dict[str, Any] | list[Any] | None = None
    cached: bool = False
    cache_similarity: float | None = None
    guardrail_action: str = "NONE"
    usage: Usage
    latency_ms: int
    trace_id: str = ""
    retries: int = 0


class ErrorBody(BaseModel):
    error: str
    detail: str
    run_id: str | None = None
