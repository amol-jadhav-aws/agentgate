# ADR-0002: Build a small inference gateway instead of adopting LiteLLM / a vendor proxy

**Status:** accepted

## Context
LiteLLM Proxy and Bedrock-side gateways offer routing and spend tracking. We need: capability routing (callers
never name a model), atomic per-tenant and per-run budgets, a cache key that includes retrieved-context hash
and prompt version, and schema validation with one escalation retry.

## Decision
Own gateway, ~600 lines of FastAPI, with a `ModelClient` interface so Bedrock Converse is one implementation
and a deterministic fake is another. Prices and routes live in `config/capabilities.yaml`.

## Consequences
- Nothing in the request path we do not understand; easy to explain in a review.
- We re-implement retries and streaming later if needed; streaming is out of scope for v0.1.
- Bedrock's own features (Guardrails, cross-region inference) are used, not replaced.
