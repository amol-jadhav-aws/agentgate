# ADR-0005: Cedar via cedarpy in process; Amazon Verified Permissions as the managed path

**Status:** accepted

## Context
Verified Permissions (AVP) is the managed Cedar store on AWS. It adds a network call per decision, a
per-request price, and an account dependency for local development and CI.

## Decision
Evaluate the same `.cedar` files in process with `cedarpy`. Keep the policy text AVP-compatible so a
`PolicyStore` can be created from `policies/` without edits when central management or audit UI is wanted.

## Consequences
- Sub-millisecond decisions, fully offline tests.
- Policy distribution is "deploy the container"; AVP would make it "update the store".
