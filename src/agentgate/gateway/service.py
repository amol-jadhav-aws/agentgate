"""The inference gateway's core: seven checks between a caller and Bedrock.

  1 identity (done by the HTTP layer)   5 Bedrock Converse + Guardrails
  2 budget, atomic reserve              6 validate against output schema, one retry on fallback
  3 route capability -> model           7 record: ledger row + OTel span
  4 semantic cache

Kept free of FastAPI so the eval runner and tests call `chat()` directly.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from agentgate.common.config import get_settings
from agentgate.common.ledger import LedgerRow, get_ledger, price_for
from agentgate.common.store import BudgetExceeded
from agentgate.common.telemetry import current_trace_id, span
from agentgate.gateway.bedrock import ModelResult, get_model_client
from agentgate.gateway.cache import SemanticCache, bucket_key
from agentgate.gateway.identity import Principal
from agentgate.gateway.models import ChatRequest, ChatResponse, Usage
from agentgate.gateway.routing import Route, get_router
from agentgate.gateway.validate import OutputInvalid, validate_against


class GatewayError(Exception):
    status = 500
    code = "gateway_error"


class OverBudget(GatewayError):
    status = 429
    code = "over_budget"


class BadCapability(GatewayError):
    status = 400
    code = "unknown_capability"


class InvalidOutput(GatewayError):
    status = 502
    code = "invalid_model_output"


@dataclass
class _Attempt:
    result: ModelResult
    model_id: str
    parsed: object | None


_cache: SemanticCache | None = None


def get_cache() -> SemanticCache:
    global _cache
    if _cache is None:
        _cache = SemanticCache(embedder=get_model_client())
    return _cache


def reset_cache_for_tests() -> None:
    global _cache
    _cache = None


def _system_prefix(route: Route) -> str:
    return f"[capability:{route.capability}] [prompt:{route.prompt_version}] You are a component of the AgentGate platform. Follow the caller's instructions exactly."


def _estimate_usd(route: Route, messages: list[dict[str, str]], max_tokens: int) -> float:
    in_tokens = sum(len(m["content"]) for m in messages) // 4 + 50
    return price_for(route.primary.model_id, in_tokens, max_tokens)


def _call(
    route: Route, model_id: str, messages: list[dict[str, str]], req: ChatRequest, max_tokens: int
) -> _Attempt:
    s = get_settings()
    client = get_model_client()
    guardrail = {"id": s.guardrail_id, "version": s.guardrail_version} if s.guardrail_id else None
    with span("model.call", model=model_id, capability=route.capability, run_id=req.run_id) as sp:
        result = client.converse(
            model_id,
            messages,
            max_tokens=max_tokens,
            temperature=req.temperature if req.temperature is not None else route.temperature,
            guardrail=guardrail,
        )
        sp.set_attribute("agentgate.input_tokens", result.input_tokens)
        sp.set_attribute("agentgate.output_tokens", result.output_tokens)
        sp.set_attribute("agentgate.guardrail_action", result.guardrail_action)
    parsed = None
    if req.output_schema and result.guardrail_action == "NONE":
        parsed = validate_against(req.output_schema, result.content)  # raises OutputInvalid
    return _Attempt(result=result, model_id=model_id, parsed=parsed)


def chat(principal: Principal, req: ChatRequest) -> ChatResponse:
    t0 = time.time()
    router = get_router()
    ledger = get_ledger()
    try:
        route = router.route(req.capability)
    except KeyError as e:
        raise BadCapability(f"unknown capability {req.capability!r}; known: {router.capabilities}") from e

    messages = [{"role": "system", "content": _system_prefix(route)}] + [m.model_dump() for m in req.messages]
    max_tokens = min(req.max_tokens or route.primary.max_output_tokens, route.primary.max_output_tokens)
    bucket = bucket_key(
        principal.tenant,
        route.capability,
        route.prompt_version,
        route.primary.model_id,
        req.context_hash,
        messages,
    )

    with span(
        "gateway.chat",
        tenant=principal.tenant,
        feature=req.feature,
        capability=req.capability,
        run_id=req.run_id,
    ) as sp:
        # 2 budget: reserve the worst case; refused calls cost nothing
        estimate = _estimate_usd(route, messages, max_tokens)
        try:
            ledger.reserve(principal.tenant, req.run_id, estimate)
        except BudgetExceeded as e:
            sp.set_attribute("agentgate.budget_refused", True)
            raise OverBudget(
                f"tenant {principal.tenant} over budget: limit {e.limit:.4f} USD, spent {ledger.spent(principal.tenant):.4f} USD"
            ) from e

        # 4 cache
        if route.cache and not req.bypass_cache:
            hit = get_cache().lookup(bucket, messages)
            if hit:
                ledger.settle(principal.tenant, req.run_id, estimate, 0.0)
                parsed = validate_against(req.output_schema, hit.content) if req.output_schema else None
                latency = int((time.time() - t0) * 1000)
                ledger.record(
                    LedgerRow(
                        principal.tenant,
                        req.feature,
                        req.run_id,
                        "model",
                        hit.model_id,
                        0,
                        0,
                        0.0,
                        cached=True,
                        latency_ms=latency,
                        extra={"similarity": hit.similarity},
                    )
                )
                sp.set_attribute("agentgate.cache_hit", True)
                return ChatResponse(
                    run_id=req.run_id,
                    capability=req.capability,
                    model=hit.model_id,
                    content=hit.content,
                    parsed=parsed,
                    cached=True,
                    cache_similarity=hit.similarity,
                    usage=Usage(input_tokens=0, output_tokens=0, cost_usd=0.0),
                    latency_ms=latency,
                    trace_id=current_trace_id(),
                )

        # 5 + 6 model call with one escalation to the fallback on schema failure
        attempts, errors = 0, []
        candidates = [route.primary.model_id]
        if (
            router.escalation.get("on_schema_failure") == "fallback"
            and route.fallback.model_id != route.primary.model_id
        ):
            candidates.append(route.fallback.model_id)
        candidates = candidates[: 1 + int(router.escalation.get("max_retries", 1))]
        attempt: _Attempt | None = None
        for model_id in candidates:
            attempts += 1
            try:
                attempt = _call(route, model_id, messages, req, max_tokens)
                break
            except OutputInvalid as e:
                errors.append(f"{model_id}: {e}")
                attempt = None
        actual = 0.0
        if attempt is None:
            ledger.settle(principal.tenant, req.run_id, estimate, 0.0)
            ledger.record(
                LedgerRow(
                    principal.tenant,
                    req.feature,
                    req.run_id,
                    "model",
                    route.primary.model_id,
                    0,
                    0,
                    0.0,
                    extra={"error": "invalid_output", "errors": errors},
                )
            )
            raise InvalidOutput(
                "model output failed schema validation on every candidate: " + " | ".join(errors)
            )

        r = attempt.result
        actual = price_for(attempt.model_id, r.input_tokens, r.output_tokens)
        ledger.settle(principal.tenant, req.run_id, estimate, actual)

        if route.cache and r.guardrail_action == "NONE":
            get_cache().store(
                bucket,
                messages,
                r.content,
                attempt.model_id,
                {"input_tokens": r.input_tokens, "output_tokens": r.output_tokens},
            )

        # 7 record
        latency = int((time.time() - t0) * 1000)
        ledger.record(
            LedgerRow(
                principal.tenant,
                req.feature,
                req.run_id,
                "model",
                attempt.model_id,
                r.input_tokens,
                r.output_tokens,
                actual,
                guardrail_action=r.guardrail_action,
                latency_ms=latency,
                extra={"retries": attempts - 1},
            )
        )
        sp.set_attribute("agentgate.cost_usd", actual)
        return ChatResponse(
            run_id=req.run_id,
            capability=req.capability,
            model=attempt.model_id,
            content=r.content,
            parsed=attempt.parsed,
            guardrail_action=r.guardrail_action,
            usage=Usage(input_tokens=r.input_tokens, output_tokens=r.output_tokens, cost_usd=actual),
            latency_ms=latency,
            trace_id=current_trace_id(),
            retries=attempts - 1,
        )


def context_hash(*chunks: str) -> str:
    """Helper for callers: hash retrieved context so the cache key changes when the KB does."""
    h = hashlib.sha256()
    for c in chunks:
        h.update(c.encode())
    return h.hexdigest()[:16]
