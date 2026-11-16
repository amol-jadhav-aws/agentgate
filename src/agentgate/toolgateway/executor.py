"""Tool gateway core: the only way an agent touches AWS.

manifest lookup -> schema check -> budget -> read current state -> approval token ->
Cedar policy (audit before action) -> AssumeRole per tool -> execute with timeout ->
redact output -> ledger row + span
"""

from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass, field
from typing import Any

from agentgate.common.config import get_settings
from agentgate.common.ledger import LedgerRow, get_ledger
from agentgate.common.redaction import redact
from agentgate.common.store import BudgetExceeded
from agentgate.common.telemetry import current_trace_id, span
from agentgate.toolgateway import approvals
from agentgate.toolgateway.manifest import ArgumentsInvalid, ToolNotFound, get_manifest
from agentgate.toolgateway.policy import Decision, evaluate
from agentgate.toolgateway.sts import session_for
from agentgate.toolgateway.tools import get_tool


class ToolGatewayError(Exception):
    status = 500
    code = "tool_gateway_error"


class UnknownTool(ToolGatewayError):
    status = 404
    code = "unknown_tool"


class BadArguments(ToolGatewayError):
    status = 400
    code = "invalid_arguments"


class Denied(ToolGatewayError):
    status = 403
    code = "policy_denied"


class OverBudget(ToolGatewayError):
    status = 429
    code = "over_budget"


class ToolFailed(ToolGatewayError):
    status = 502
    code = "tool_failed"


class ApprovalInvalid(ToolGatewayError):
    status = 403
    code = "approval_invalid"


@dataclass
class InvokeResult:
    run_id: str
    tool: str
    decision: str
    audit_id: str
    output: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    redactions: dict[str, int] = field(default_factory=dict)
    approvers: list[str] = field(default_factory=list)
    latency_ms: int = 0
    cost_usd: float = 0.0
    trace_id: str = ""
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__


_pool = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="tool")


def invoke(
    tenant: str,
    tool_name: str,
    args: dict[str, Any],
    *,
    run_id: str,
    feature: str = "default",
    approval_token: str | None = None,
    rationale: str = "",
    expected_impact: str = "",
    evidence: list[str] | None = None,
) -> InvokeResult:
    t0 = time.time()
    manifest = get_manifest()
    ledger = get_ledger()
    try:
        tool = manifest.get(tool_name)
    except ToolNotFound as e:
        raise UnknownTool(f"unknown tool {tool_name!r}") from e
    try:
        tool.validate(args)
    except ArgumentsInvalid as e:
        raise BadArguments(str(e)) from e

    with span("tool.invoke", tenant=tenant, tool=tool_name, run_id=run_id, risk_tier=tool.risk_tier) as sp:
        # budget: fixed per-call cost reserved before anything happens
        try:
            ledger.reserve(tenant, run_id, tool.cost_usd)
        except BudgetExceeded as e:
            raise OverBudget(f"tenant {tenant} over budget: {e}") from e

        session = session_for(tool.role_arn, run_id)

        # read-before-write state for policy guards (e.g. scaling delta)
        current_state: dict[str, Any] = {}
        if tool_name == "ecs_update_desired_count":
            from agentgate.toolgateway.tools.ecs_update_desired_count import current_state as _cs

            current_state = _cs(session, args)

        approved, approvers = False, []
        if approval_token:
            try:
                approvers = approvals.verify_and_consume(
                    approval_token, run_id=run_id, tool=tool_name, args=args
                )
                approved = True
            except approvals.ApprovalError as e:
                ledger.settle(tenant, run_id, tool.cost_usd, 0.0)
                raise ApprovalInvalid(str(e)) from e

        decision = evaluate(
            tenant,
            tool,
            args,
            run_id=run_id,
            approved=approved,
            approver_count=len(approvers),
            current_state=current_state,
        )
        sp.set_attribute("agentgate.decision", decision.decision.value)
        sp.set_attribute("agentgate.audit_id", decision.audit_id)

        if decision.decision is Decision.DENY:
            ledger.settle(tenant, run_id, tool.cost_usd, 0.0)
            raise Denied(
                f"policy denied {tool_name} for {tenant}: rules {decision.reasons or ['default deny']}"
            )

        if decision.decision is Decision.APPROVE:
            ledger.settle(tenant, run_id, tool.cost_usd, 0.0)
            req = approvals.request_approval(
                run_id=run_id,
                tenant=tenant,
                tool=tool_name,
                args=args,
                rationale=rationale,
                expected_impact=expected_impact,
                rule=f"risk tier {tool.risk_tier} requires human approval",
                evidence=evidence,
            )
            return InvokeResult(
                run_id,
                tool_name,
                "approve",
                decision.audit_id,
                approval={
                    "approval_id": req.approval_id,
                    "status": req.status,
                    "expires_in_s": get_settings().approval_ttl_seconds,
                },
                latency_ms=int((time.time() - t0) * 1000),
                trace_id=current_trace_id(),
                reasons=decision.reasons,
            )

        # ALLOW: execute with the tool's own role and a hard timeout
        call_args = dict(args)
        if tool_name == "kb_search":
            call_args["_tenant"] = tenant  # tenant filter comes from identity, never from the model
        fn = get_tool(tool_name)
        future = _pool.submit(fn, session, call_args, run_id)
        try:
            raw = future.result(timeout=tool.timeout_seconds)
        except concurrent.futures.TimeoutError as e:
            ledger.record(
                LedgerRow(
                    tenant,
                    feature,
                    run_id,
                    "tool",
                    tool_name,
                    cost_usd=tool.cost_usd,
                    extra={"error": "timeout"},
                )
            )
            raise ToolFailed(f"{tool_name} timed out after {tool.timeout_seconds}s") from e
        except Exception as e:  # noqa: BLE001 - any tool failure is reported, never swallowed
            ledger.record(
                LedgerRow(
                    tenant,
                    feature,
                    run_id,
                    "tool",
                    tool_name,
                    cost_usd=tool.cost_usd,
                    extra={"error": str(e)[:200]},
                )
            )
            raise ToolFailed(f"{tool_name} failed: {e}") from e

        output, redactions = redact(raw)
        latency = int((time.time() - t0) * 1000)
        ledger.record(
            LedgerRow(
                tenant,
                feature,
                run_id,
                "tool",
                tool_name,
                cost_usd=tool.cost_usd,
                latency_ms=latency,
                extra={"audit_id": decision.audit_id, "redactions": redactions, "approvers": approvers},
            )
        )
        return InvokeResult(
            run_id,
            tool_name,
            "allow",
            decision.audit_id,
            output=output,
            redactions=redactions,
            approvers=approvers,
            latency_ms=latency,
            cost_usd=tool.cost_usd,
            trace_id=current_trace_id(),
            reasons=decision.reasons,
        )
