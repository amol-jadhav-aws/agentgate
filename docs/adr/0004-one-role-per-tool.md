# ADR-0004: One IAM role per tool, assumed per call, run id in the session name

**Status:** accepted

## Context
A single broad task role for "the agent" makes every CloudTrail event look the same and gives any prompt
injection the union of all permissions.

## Decision
`config/tools.yaml` names a role ARN per tool. The tool gateway's task role may only `sts:AssumeRole` roles
matching `agentgate-<env>-tool-*`. Each call does `AssumeRole(RoleSessionName="agentgate-<run_id>",
Tags=[agentgate:run_id])`. Secrets are read with that session at call time.

## Consequences
- CloudTrail, the ledger and the OTel trace join on the run id.
- Blast radius of a bad proposal is one tool's permissions.
- More roles to manage; Terraform `modules/iam_tool_roles` generates them from one map.
