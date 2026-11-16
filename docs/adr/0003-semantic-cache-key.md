# ADR-0003: Exact key for what must match, similarity only on the user turn

**Status:** accepted (revised after the eval gate caught a defect)

## Context
First version embedded the whole prompt. Two different questions over the same long document produced
vectors dominated by the shared context, cosine ≥ 0.95, and the cache served the first question's answer to
the second. `evals/golden/answer_grounded.jsonl` failed (`groundedness 0.83 < 0.85`).

## Decision
Cache key = `tenant | capability | prompt_version | model | context_hash | sha256(system + earlier turns)`.
Only the final user turn is embedded. Threshold stays 0.95. `propose_action` and `judge` are never cached.

## Consequences
- A cache entry cannot cross tenants, prompt versions, models or retrieved context.
- Hit rate is lower than whole-prompt matching but every hit is a correct hit.
- The failing eval case stays in the golden set as a regression guard.
