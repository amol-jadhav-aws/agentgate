# ADR-0008: The gateways own the cost ledger

**Status:** accepted

## Context
If each agent logs its own cost, one agent forgets, another rounds, a vendor's agent logs nothing, and
finance gets a number nobody trusts.

## Decision
The inference gateway and tool gateway write one `LedgerRow` per call (tenant, feature, run id, tokens,
cost, cached, guardrail action, latency) into DynamoDB. Budgets are reserved before the call with an atomic
conditional update and settled to actual cost after. Agents can only read their own rows.

## Consequences
- `GET /v1/budget` and `GET /v1/runs/{id}` are the FinOps API; no agent code involved.
- A refused call costs exactly zero.
