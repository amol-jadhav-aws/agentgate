"""Human approval: a signed, single-use token tied to one run, one tool and one exact argument set.

Flow: the policy says APPROVE -> the agent pauses (LangGraph interrupt) and a request row
is stored -> a Slack card (or `agentgate approve <id>` on the CLI) records the approver ->
the agent resumes and presents the token -> the gateway verifies signature, expiry,
run id, tool and an argument hash, consumes it, and asks policy again with approved=true.

An approval is never a blanket yes: change one argument and the hash no longer matches.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from agentgate.common.config import get_settings
from agentgate.common.store import get_store


class ApprovalError(Exception):
    pass


def args_hash(args: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]


@dataclass
class ApprovalRequest:
    approval_id: str
    run_id: str
    tenant: str
    tool: str
    args: dict[str, Any]
    rationale: str
    expected_impact: str
    rule: str
    created_at: float
    status: str = "pending"  # pending | approved | rejected | expired | consumed
    approvers: list[str] | None = None
    decided_at: float | None = None
    comment: str | None = None


def _sign(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(get_settings().approval_signing_key.encode(), body, hashlib.sha256).hexdigest()


def request_approval(
    *,
    run_id: str,
    tenant: str,
    tool: str,
    args: dict[str, Any],
    rationale: str,
    expected_impact: str,
    rule: str,
    evidence: list[str] | None = None,
) -> ApprovalRequest:
    req = ApprovalRequest(
        uuid.uuid4().hex[:10],
        run_id,
        tenant,
        tool,
        args,
        rationale,
        expected_impact,
        rule,
        time.time(),
        approvers=[],
    )
    get_store().put(
        "approval",
        req.approval_id,
        {**req.__dict__, "evidence": evidence or []},
        ttl_seconds=get_settings().approval_ttl_seconds * 6,
    )
    get_store().put(f"run#{run_id}", f"approval#{req.approval_id}", {"status": "pending", "tool": tool})
    _notify_slack(req, evidence or [])
    return req


def _notify_slack(req: ApprovalRequest, evidence: list[str]) -> None:
    url = get_settings().slack_webhook_url
    if not url:
        return
    text = (
        f"*AgentGate approval needed* `{req.approval_id}`\n"
        f"*Agent:* {req.tenant}  *Run:* `{req.run_id}`\n"
        f"*Action:* `{req.tool}` {json.dumps(req.args)}\n"
        f"*Why:* {req.rationale}\n*Impact:* {req.expected_impact}\n*Rule:* {req.rule}\n"
        + ("*Evidence:*\n" + "\n".join(f"• {e}" for e in evidence[:6]) + "\n" if evidence else "")
        + f"Approve: `agentgate approve {req.approval_id} --as <you>`   Reject: `agentgate reject {req.approval_id}`"
    )
    try:
        httpx.post(url, json={"text": text}, timeout=5)
    except httpx.HTTPError:
        pass  # Slack being down must never block the audit trail; the CLI path still works


def decide(approval_id: str, approver: str, approve: bool, comment: str | None = None) -> dict[str, Any]:
    store = get_store()
    row = store.get("approval", approval_id)
    if not row:
        raise ApprovalError(f"approval {approval_id} not found or expired")
    if row["status"] not in ("pending", "approved"):
        raise ApprovalError(f"approval {approval_id} already {row['status']}")
    if time.time() - row["created_at"] > get_settings().approval_ttl_seconds:
        row["status"] = "expired"
        store.put("approval", approval_id, row)
        raise ApprovalError(f"approval {approval_id} expired after {get_settings().approval_ttl_seconds}s")
    approvers = list(row.get("approvers") or [])
    if approver in approvers:
        raise ApprovalError(
            f"{approver} already approved {approval_id}; a second approval must come from someone else"
        )
    row["decided_at"] = time.time()
    row["comment"] = comment
    if not approve:
        row["status"] = "rejected"
        row["approvers"] = approvers + [approver]
        store.put("approval", approval_id, row)
        store.put(
            f"run#{row['run_id']}",
            f"approval#{approval_id}",
            {"status": "rejected", "tool": row["tool"], "by": approver},
        )
        return {"approval_id": approval_id, "status": "rejected", "token": None}
    approvers.append(approver)
    row["status"] = "approved"
    row["approvers"] = approvers
    store.put("approval", approval_id, row)
    store.put(
        f"run#{row['run_id']}",
        f"approval#{approval_id}",
        {"status": "approved", "tool": row["tool"], "by": approvers},
    )
    return {"approval_id": approval_id, "status": "approved", "token": mint_token(row)}


def mint_token(row: dict[str, Any]) -> str:
    payload = {
        "approval_id": row["approval_id"],
        "run_id": row["run_id"],
        "tool": row["tool"],
        "args_hash": args_hash(row["args"]),
        "approvers": sorted(row["approvers"]),
        "exp": int(time.time()) + get_settings().approval_ttl_seconds,
    }
    return json.dumps({"p": payload, "s": _sign(payload)}, separators=(",", ":"))


def verify_and_consume(token: str, *, run_id: str, tool: str, args: dict[str, Any]) -> list[str]:
    """Return the list of approvers if the token is valid for exactly this call; consume it."""
    try:
        obj = json.loads(token)
        payload, sig = obj["p"], obj["s"]
    except (ValueError, KeyError, TypeError) as e:
        raise ApprovalError("malformed approval token") from e
    if not hmac.compare_digest(sig, _sign(payload)):
        raise ApprovalError("approval token signature invalid")
    if payload["exp"] < time.time():
        raise ApprovalError("approval token expired")
    if payload["run_id"] != run_id:
        raise ApprovalError("approval token belongs to a different run")
    if payload["tool"] != tool:
        raise ApprovalError("approval token is for a different tool")
    if payload["args_hash"] != args_hash(args):
        raise ApprovalError("arguments changed since approval; approve again")
    store = get_store()
    row = store.get("approval", payload["approval_id"])
    if not row or row["status"] != "approved":
        raise ApprovalError("approval already used or revoked")
    row["status"] = "consumed"
    store.put("approval", payload["approval_id"], row)
    return list(payload["approvers"])


def get(approval_id: str) -> dict[str, Any] | None:
    return get_store().get("approval", approval_id)


def pending() -> list[dict[str, Any]]:
    return [r for r in get_store().query("approval") if r.get("status") == "pending"]
