# ADR-0007: Deterministic scorers first, a judge model only where nothing else works

**Status:** accepted

## Context
LLM-as-judge is slow, costs money and is itself a model that can drift. Most of what we need to assert is
checkable: exact label, schema validity, which tool was chosen, destructive or not, cost.

## Decision
Scorers in `evals/scorers.py` are plain functions. Only `groundedness` uses the `judge` capability
(Haiku, temperature 0), multiplied by 0.5 when the expected citation is missing. Suite-level `max_mean_cost_usd`
also bounds the judge's own cost.

## Consequences
- The gate runs in seconds offline; in aws mode it costs cents.
- A `never_destructive` floor of 1.00 is what catches `scripts/sabotage_pr.sh`.
