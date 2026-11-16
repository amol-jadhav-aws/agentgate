# ADR-0001: Policy is deterministic code, never a prompt

**Status:** accepted · 2026-11

## Context
Agent frameworks let you "tell the model" what it may not do. A model can be argued out of that by a tool
result, a retrieved document or a user. Approvals, risk tiers and tenant boundaries must hold regardless.

## Decision
Every tool call is authorised by Cedar policies evaluated in process (`cedarpy`). Two actions are asked:
`invoke` (may this happen at all) and `auto_execute` (may it happen without a human). The pair maps to
ALLOW / DENY / APPROVE. The audit row is written before the decision is returned.

## Consequences
- Policy tests are exact: `decision_accuracy` must be 1.00 in the eval gate.
- Prompts still say "never propose destructive actions", but as hygiene, not as the control.
- Policy changes are PRs with CODEOWNERS review.
