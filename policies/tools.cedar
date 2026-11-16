// AgentGate tool policies (Cedar).
//
// Two actions are evaluated for every tool call:
//   Action::"invoke"        may this principal call this tool with these arguments at all?
//   Action::"auto_execute"  may it run WITHOUT a human approval?
//
// Decision mapping (policy.py):
//   invoke forbidden                    -> DENY
//   invoke permitted, auto permitted    -> ALLOW
//   invoke permitted, auto forbidden    -> APPROVE (LangGraph interrupt, Slack card)
//
// Entities: Agent::"<tenant>", Tool::"<name>" with attrs risk_tier, Env::"<env>".
// Context: { "args": {...}, "env": "dev|prod", "approved": bool, "approver": "...", "current_count": n }

// 1. A tenant may invoke only tools it has been granted (grants come from config/tools.yaml as entity parents).
permit(
  principal,
  action == Action::"invoke",
  resource
) when { resource in principal.granted_tools };

// 2. Tier 0 and 1 tools run without approval.
permit(
  principal,
  action == Action::"auto_execute",
  resource
) when { resource.risk_tier <= 1 };

// 3. Tier 2 tools run only with a valid approval token (checked by approvals.py, surfaced in context).
permit(
  principal,
  action == Action::"auto_execute",
  resource
) when { resource.risk_tier == 2 && context.approved == true };

// 4. Tier 3 (irreversible) tools are never allowed in prod, for anyone, approval or not.
forbid(
  principal,
  action,
  resource
) when { resource.risk_tier == 3 && context.env == "prod" };

// 5. Tier 3 in non-prod needs approval from two distinct approvers.
permit(
  principal,
  action == Action::"auto_execute",
  resource
) when { resource.risk_tier == 3 && context.env != "prod" && context.approved == true && context.approver_count >= 2 };

// 6. Scaling guard: no single step may change desired_count by more than max_delta, and never to zero in prod.
forbid(
  principal,
  action == Action::"invoke",
  resource == Tool::"ecs_update_desired_count"
) when {
  context has current_count &&
  (context.args.desired_count - context.current_count > resource.max_delta ||
   context.current_count - context.args.desired_count > resource.max_delta)
};

forbid(
  principal,
  action == Action::"invoke",
  resource == Tool::"ecs_update_desired_count"
) when { context.env == "prod" && context.args.desired_count == 0 };

// 7. The knowledge agent must never touch infrastructure tools even if a grant is mis-edited.
forbid(
  principal == Agent::"knowledge-agent",
  action,
  resource
) unless { resource == Tool::"kb_search" };
