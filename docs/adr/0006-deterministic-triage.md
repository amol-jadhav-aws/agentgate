# ADR-0006: Deterministic triage before any model call

**Status:** accepted

## Context
Most triggers are structured: an alarm carries its metric name. Asking a model "what kind of incident is
this" costs money and adds a failure mode for information we already have.

## Decision
`OpsAgent.triage` maps metric names to runbook families in code. The `classify` capability is used only for
free-text triggers, on the cheapest model tier, cached.

## Consequences
- Fewer model calls per incident (the CPU demo makes exactly one).
- The classify golden set guards the free-text path.
