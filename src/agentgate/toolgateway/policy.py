"""Policy engine: Cedar, evaluated in-process with cedarpy. Deterministic; never a prompt.

Three outcomes: ALLOW, DENY, APPROVE. The audit row is written BEFORE the decision
is returned to the caller, so there is never an action without a record of why it was
permitted. The same policies can be loaded into Amazon Verified Permissions unchanged
when a managed store is preferred (ADR-0005).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from functools import lru_cache
from typing import Any

import cedarpy

from agentgate.common.config import get_settings
from agentgate.common.store import get_store
from agentgate.toolgateway.manifest import ToolSpec, get_manifest


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVE = "approve"


@dataclass
class PolicyResult:
    decision: Decision
    reasons: list[str]
    audit_id: str
    tool: str
    tenant: str
    risk_tier: int
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["decision"] = self.decision.value
        return d


@lru_cache
def load_policies() -> str:
    parts = []
    for p in sorted(get_settings().policy_dir.glob("*.cedar")):
        parts.append(p.read_text())
    return "\n".join(parts)


def _entities(tenant: str, tool: ToolSpec) -> list[dict[str, Any]]:
    manifest = get_manifest()
    granted = [{"__entity": {"type": "Tool", "id": t.name}} for t in manifest.granted(tenant)]
    tool_attrs: dict[str, Any] = {"risk_tier": tool.risk_tier}
    if "max_delta" in tool.constraints:
        tool_attrs["max_delta"] = int(tool.constraints["max_delta"])
    return [
        {"uid": {"type": "Agent", "id": tenant}, "attrs": {"granted_tools": granted}, "parents": []},
        {"uid": {"type": "Tool", "id": tool.name}, "attrs": tool_attrs, "parents": []},
    ]


def _ask(action: str, tenant: str, tool: ToolSpec, context: dict[str, Any], entities: list[dict[str, Any]]):
    request = {
        "principal": f'Agent::"{tenant}"',
        "action": f'Action::"{action}"',
        "resource": f'Tool::"{tool.name}"',
        "context": context,
    }
    return cedarpy.is_authorized(request, load_policies(), entities)


def evaluate(
    tenant: str,
    tool: ToolSpec,
    args: dict[str, Any],
    *,
    run_id: str,
    approved: bool = False,
    approver_count: int = 0,
    current_state: dict[str, Any] | None = None,
    env: str | None = None,
) -> PolicyResult:
    s = get_settings()
    env = env or s.env_name
    context: dict[str, Any] = {
        "args": args,
        "env": env,
        "approved": approved,
        "approver_count": approver_count,
    }
    if current_state:
        context.update(current_state)  # e.g. current_count for the scaling guard
    entities = _entities(tenant, tool)

    invoke = _ask("invoke", tenant, tool, context, entities)
    reasons = list(invoke.diagnostics.reasons)
    errors = [str(e) for e in invoke.diagnostics.errors]
    if not invoke.allowed:
        decision = Decision.DENY
    else:
        auto = _ask("auto_execute", tenant, tool, context, entities)
        reasons += list(auto.diagnostics.reasons)
        errors += [str(e) for e in auto.diagnostics.errors]
        decision = Decision.ALLOW if auto.allowed else Decision.APPROVE

    audit_id = uuid.uuid4().hex[:12]
    result = PolicyResult(
        decision=decision,
        reasons=reasons,
        audit_id=audit_id,
        tool=tool.name,
        tenant=tenant,
        risk_tier=tool.risk_tier,
        errors=errors,
    )
    # Audit before action: the row exists before any caller learns the decision.
    get_store().put(
        f"audit#{tenant}",
        f"{int(time.time() * 1000):013d}#{audit_id}",
        {
            "run_id": run_id,
            "tool": tool.name,
            "args": json.dumps(args, sort_keys=True),
            "decision": decision.value,
            "reasons": reasons,
            "errors": errors,
            "env": env,
            "approved": approved,
            "risk_tier": tool.risk_tier,
        },
    )
    get_store().put(
        f"run#{run_id}",
        f"audit#{int(time.time() * 1000):013d}#{audit_id}",
        {"tool": tool.name, "decision": decision.value, "reasons": reasons},
    )
    return result


def audit_rows(tenant: str) -> list[dict[str, Any]]:
    return get_store().query(f"audit#{tenant}")
