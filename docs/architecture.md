# AgentGate architecture

Six views. The platform is one idea applied five ways: an agent is a tenant, and every path from a tenant
to a model or to AWS passes through code the platform owns.

## 1. System architecture

```mermaid
flowchart TB
  subgraph triggers [Triggers]
    A1[CloudWatch alarm → EventBridge]; A2[Slack or CLI question]; A3[REST call with caller identity]
  end
  subgraph agents [Agents — tenants of the platform]
    B1[Ops / FinOps agent<br/>LangGraph: triage, gather, propose, act, verify]
    B2[Knowledge agent<br/>rewrite, retrieve, rerank, answer, self-check]
    B3[Vendor / n8n agent<br/>same door, same controls]
  end
  subgraph platform [AgentGate platform]
    G[Inference gateway<br/>identity, token budgets, cost tags<br/>routing by capability, semantic cache<br/>Guardrails, schema check, ledger row]
    T[Tool / MCP gateway<br/>manifest + JSON schema<br/>one MCP server, one IAM role per tool<br/>secrets at call time, output redaction]
    P[Policy and approval<br/>Cedar: allow, deny, approve<br/>risk tiers 0–3, audit written first<br/>LangGraph interrupt, Slack approval]
    E[Evaluation gate CI<br/>golden set, deterministic scorers, judge<br/>thresholds block the merge<br/>production sample scored nightly]
    O[Observability and audit<br/>OTel: trace per run, span per call<br/>cost ledger per tenant, feature, run<br/>alerts on spend, loops, denials]
    T --> P
  end
  subgraph aws [AWS]
    C1[Amazon Bedrock<br/>Claude, Titan, Guardrails, Knowledge Bases, AgentCore]
    C2[IAM, STS, Secrets Manager<br/>role per tool, session = run id, CloudTrail]
    C3[DynamoDB, ElastiCache, S3<br/>ledger, budgets, audit, checkpoints, cache, S3 Vectors]
    C4[CloudWatch, ECS Fargate, EventBridge, SNS]
  end
  A1 --> B1; A2 --> B2; A3 --> B3
  B1 & B2 & B3 --> G
  B1 & B2 & B3 --> T
  G --> C1; T --> C2; G & T --> C3; O --> C4
```

Add a fourth agent, or a vendor's, and nothing in the platform changes except a tenant entry in
`config/tools.yaml` (grants) and `config/budgets.yaml`.

## 2. One agent run

```mermaid
flowchart LR
  S([1 alarm or request]) --> TR[2 triage · code<br/>runbook family, budget, run id]:::det
  TR --> GA[3 gather evidence<br/>read-only tools]
  GA --> PR[4 propose action<br/>structured JSON, schema-checked]
  PR --> PO{5 policy · Cedar<br/>audit row first}:::det
  PO -- allow --> EX[6 execute tool<br/>MCP server, own IAM role]:::det
  PO -- needs approval --> AP[pause: interrupt + checkpoint<br/>Slack card: evidence, impact, rule]:::det
  AP -- approved, resume --> EX
  AP -- rejected: replan --> PR
  PO -- deny --> GA
  EX --> VE{7 recovered?<br/>metric re-checked}:::det
  VE -- no, steps left max 8 --> PR
  VE -- yes --> RE[8 report and trace<br/>timeline, cost, one trace id]:::det
  classDef det fill:#4F46E5,fill-opacity:0.15,stroke:#4F46E5,stroke-width:2px
```

Filled boxes are code with no model. Only step 4 calls a model, and its output must satisfy `ACTION_SCHEMA`
or the gateway rejects it before the agent ever sees it.

## 3. Inside the inference gateway

```mermaid
flowchart LR
  I([POST /v1/chat<br/>capability, messages, schema?]) --> A[1 Identity<br/>API key or JWT]
  A --> B[2 Budget<br/>atomic DynamoDB ADD + condition]:::det
  B -- over --> X1([429, nothing spent])
  B --> R[3 Route<br/>capability → model tier]:::det
  R --> C[4 Cache?<br/>Redis, cosine ≥ 0.95 on the user turn]:::det
  C -- hit --> X2([return, log similarity, cost 0])
  C --> M[5 Bedrock Converse<br/>+ Guardrails]
  M --> V[6 Validate<br/>Pydantic/JSON schema, one retry on fallback]
  V --> L[7 Record<br/>ledger row + OTel span: tokens, cost, model, cached, guardrail]:::det
  classDef det fill:#4F46E5,fill-opacity:0.15,stroke:#4F46E5,stroke-width:2px
```

Cache key = `tenant | capability | prompt_version | model | context_hash | sha(system + earlier turns)`.
Only the final user turn is embedded and compared by similarity.

## 4. A tool call that needs approval

```mermaid
sequenceDiagram
  autonumber
  participant A as Agent (LangGraph)
  participant P as Policy engine
  participant T as Tool gateway
  participant H as Approver (Slack / CLI)
  participant W as AWS API
  A->>T: propose action JSON
  T->>P: evaluate(invoke, auto_execute) — audit row written first
  P-->>T: APPROVE (tier 2)
  T-->>A: 202 {approval_id}
  Note over A: interrupt(): state checkpointed, graph paused
  A->>H: card: evidence, expected impact, the rule
  H-->>A: approved → signed token: run id, tool, args hash, approver, 10-min expiry
  A->>T: invoke with approval_token
  T->>P: verify token (signature, expiry, run, tool, args), consume, re-evaluate approved=true
  P-->>T: ALLOW
  T->>W: AssumeRole(session name = run id) → execute
  W-->>T: result; CloudTrail event carries the run id
  T-->>A: redacted output + OTel span
```

Tier 0 and 1 skip steps 3–7. A deny at step 2 returns the rule to the agent. An expired, reused or
argument-changed token fails at step 9.

## 5. The CI evaluation gate

```mermaid
flowchart LR
  PR[1 Pull request<br/>prompt, route, tool, policy, model version] --> FC[2 Fast checks<br/>pytest, schemas, Cedar tests, ruff]
  FC --> GS[3 Golden set run<br/>JSONL cases through the real gateway, tools mocked, temp 0]:::det
  GS --> SC[4 Scorers<br/>exact, schema, tool choice, cost + judge for groundedness]:::det
  SC --> TH{5 thresholds.yaml<br/>every metric at or above?}:::det
  TH -- yes --> MG[6 Merge and deploy<br/>GitHub OIDC → Terraform, dev first, prod on tag]
  TH -- no --> BL[Block the merge<br/>PR comment: which cases fell, before/after, cost delta]
  PF[Production feedback nightly<br/>sample of real runs scored the same way] -- adds cases --> GS
  classDef det fill:#4F46E5,fill-opacity:0.15,stroke:#4F46E5,stroke-width:2px
```

## 6. AWS deployment

```mermaid
flowchart TB
  GH[GitHub Actions<br/>OIDC, no stored keys, Terraform] --> DR[IAM deploy role<br/>trust scoped to repo + branch]
  U[Callers<br/>Slack, CLI, REST · Cognito JWT or SigV4] --> ALB
  subgraph vpc [VPC, two AZs]
    subgraph pub [Public subnets]
      ALB[ALB · TLS · WAF rate limits]
    end
    subgraph priv [Private subnets · ECS Fargate]
      GW[gateway<br/>FastAPI, 2 tasks]; TG[tool-gateway<br/>policy + MCP servers, role per tool]; AG[agents<br/>LangGraph workers or AgentCore]
      RD[ElastiCache Redis<br/>semantic cache]; EP[VPC endpoints<br/>Bedrock, DynamoDB, S3, Secrets, STS, CloudWatch]
    end
  end
  ALB --> GW & TG
  GW & TG & AG --> EP
  EP --> BR[Amazon Bedrock] & DD[DynamoDB + S3 / S3 Vectors] & SM[IAM · STS · Secrets · KMS] & CW[CloudWatch · EventBridge · SNS]
```

Dev ceiling USD 40/month: Fargate Spot, ARM64, `cache.t4g.micro`, on-demand DynamoDB, Haiku by default,
`make destroy` after each session.
