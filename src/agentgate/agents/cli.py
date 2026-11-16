"""`agentgate` command line.

    agentgate demo cpu            run the CPU-spike incident end to end (pauses for approval)
    agentgate demo cost           run the FinOps cost-spike investigation
    agentgate ask "question"      knowledge agent
    agentgate approve <id> --as amol      approve a pending action (prints the token, resumes the run)
    agentgate reject <id>  --as amol
    agentgate pending             list approvals waiting
    agentgate budget              tenant spend
    agentgate audit               policy audit trail

--inproc runs everything in one process (no servers needed). Without it the CLI talks to
the gateways at AGENTGATE_GATEWAY_URL / AGENTGATE_TOOLGATEWAY_URL (docker compose).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from agentgate.agents.client import HttpPlatform, InProcessPlatform, Platform, PlatformError
from agentgate.agents.knowledge_agent import KnowledgeAgent
from agentgate.agents.ops_agent import OpsAgent

_AGENTS: dict[str, OpsAgent] = {}


def platform(args: argparse.Namespace, tenant: str) -> Platform:
    if args.inproc:
        return InProcessPlatform(tenant)
    keys = {"ops-agent": "dev-key-ops", "knowledge-agent": "dev-key-kb", "cli": "dev-key-cli"}
    return HttpPlatform(args.api_key or keys[tenant], tenant)


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_demo(args: argparse.Namespace) -> int:
    from agentgate.toolgateway.tools import fakeaws

    p = platform(args, "ops-agent")
    agent = _AGENTS.setdefault("ops", OpsAgent(p))
    if args.scenario == "cpu":
        fakeaws.spike_cpu("demo", "orders-api", 93.0)
        trigger = {
            "kind": "alarm",
            "metric": "CPUUtilization",
            "cluster": "demo",
            "service": "orders-api",
            "alarm": "orders-api-cpu-high",
        }
    else:
        fakeaws.spike_cost("AmazonBedrock", 6.0)
        trigger = {"kind": "cost", "text": "Daily AWS spend jumped; investigate the cost spike by service."}
    print(f"chaos injected: {args.scenario}", file=sys.stderr)
    res = agent.start(trigger)
    if res["status"] == "awaiting_approval":
        a = res["approval"]
        print("\n=== APPROVAL NEEDED ===", file=sys.stderr)
        print(
            f"run {res['run_id']}  approval {a['approval_id']}\n{a['tool']} {json.dumps(a['arguments'])}\nwhy: {a['rationale']}\nimpact: {a['expected_impact']}",
            file=sys.stderr,
        )
        if args.auto_approve:
            tok = p.decide_approval(a["approval_id"], args.auto_approve, True)["token"]
            res = agent.resume(res["run_id"], tok)
        else:
            print(
                f"\nrun:  agentgate approve {a['approval_id']} --as <you> --run {res['run_id']}",
                file=sys.stderr,
            )
    _print(res)
    return 0 if res["status"] == "done" and (res.get("report") or {}).get("recovered") else 2


def cmd_ask(args: argparse.Namespace) -> int:
    p = platform(args, "knowledge-agent")
    _print(KnowledgeAgent(p).answer(args.question))
    return 0


def cmd_decide(args: argparse.Namespace, approve: bool) -> int:
    p = platform(args, "cli")
    try:
        out = p.decide_approval(args.approval_id, args.approver, approve)
    except PlatformError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _print(out)
    if approve and args.run and args.inproc and "ops" in _AGENTS:
        _print(_AGENTS["ops"].resume(args.run, out["token"]))
    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    from agentgate.toolgateway import approvals

    _print(approvals.pending())
    return 0


def cmd_budget(args: argparse.Namespace) -> int:
    from agentgate.common.ledger import get_ledger

    for t in ("ops-agent", "knowledge-agent", "cli", "evals"):
        _print(get_ledger().summary(t))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    from agentgate.toolgateway.policy import audit_rows

    _print(audit_rows(args.tenant))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentgate")
    ap.add_argument("--inproc", action="store_true", help="run gateways in this process (no servers)")
    ap.add_argument("--api-key")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo")
    d.add_argument("scenario", choices=["cpu", "cost"])
    d.add_argument(
        "--auto-approve", metavar="APPROVER", help="approve as this person without pausing (demos, CI)"
    )
    d.set_defaults(fn=cmd_demo)

    a = sub.add_parser("ask")
    a.add_argument("question")
    a.set_defaults(fn=cmd_ask)

    for name, ok in (("approve", True), ("reject", False)):
        x = sub.add_parser(name)
        x.add_argument("approval_id")
        x.add_argument("--as", dest="approver", required=True)
        x.add_argument("--run")
        x.set_defaults(fn=lambda ns, ok=ok: cmd_decide(ns, ok))

    sub.add_parser("pending").set_defaults(fn=cmd_pending)
    sub.add_parser("budget").set_defaults(fn=cmd_budget)
    au = sub.add_parser("audit")
    au.add_argument("--tenant", default="ops-agent")
    au.set_defaults(fn=cmd_audit)

    ns = ap.parse_args(argv)
    return ns.fn(ns)


if __name__ == "__main__":
    sys.exit(main())
