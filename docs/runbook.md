# Runbook

## Local
- `make install && make test && make eval` — everything offline.
- `make up` — services + Jaeger at http://localhost:16686. `make down` to stop.
- Approvals: `agentgate pending`, `agentgate approve <id> --as <you>`, `agentgate reject <id> --as <you>`.
- Reset the fake world: restart the process (`fakeaws.reset()` in tests).

## AWS bootstrap (once)
1. Create the GitHub OIDC provider in the account if absent:
   `aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com --client-id-list sts.amazonaws.com --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1`
2. Terraform state: an S3 bucket (versioned, encrypted) and a DynamoDB lock table; uncomment the backend block in `envs/dev/main.tf`.
3. Enable Bedrock model access for Claude 3.5 Haiku, Claude 3.7 Sonnet, Nova Micro, Titan Embed v2 in the region.
4. `terraform apply`; copy `deploy_role_arn` to the repo variable `AWS_DEPLOY_ROLE_ARN`.
5. Optional: create a Guardrail (PII + prompt-attack filters) and a Knowledge Base on S3 Vectors with a `tenant` metadata field; pass their ids as variables.
6. `python scripts/seed_budgets.py` with `AGENTGATE_MODE=aws`.

## Incident demo in AWS
- `python scripts/chaos_cpu_spike.py --cluster demo --service orders-api` runs a stress task; the alarm fires in ~15 min; EventBridge invokes the agents service.
- Approve from Slack (webhook card) or `agentgate approve`.
- Verify in Jaeger/X-Ray: one trace, `agentgate.run_id`, spans for each call; CloudTrail: `userIdentity.sessionContext.sessionIssuer` shows the per-tool role and the session name carries the run id.

## When the eval gate is red
- Read the PR comment: failing cases list the suite, case id and metric.
- A real regression → fix the prompt/route/policy. A legitimately changed expectation → update the golden case in the same PR with a one-line reason in the PR description.
- Lowering a threshold requires CODEOWNERS review.

## Cost control
- AWS Budget alerts at 80% of USD 40. `make destroy` after every session. Fargate Spot and Haiku by default.
- `agentgate budget` shows tenant spend from the ledger; `GET /v1/runs/{run_id}` shows one run's cost timeline.
