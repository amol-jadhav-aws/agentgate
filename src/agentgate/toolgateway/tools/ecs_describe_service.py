"""ECS DescribeServices. Tier 1 (reveals task definitions and deployment state)."""

from __future__ import annotations

from typing import Any

from agentgate.toolgateway.tools import fakeaws


def run(session: Any, args: dict[str, Any], run_id: str) -> dict[str, Any]:
    if session is None:
        svc = fakeaws.service(args["cluster"], args["service"])
        if not svc:
            return {"found": False}
        return {
            "found": True,
            "cluster": svc["cluster"],
            "service": svc["service"],
            "desired_count": svc["desired"],
            "running_count": svc["running"],
            "task_definition": svc["task_def"],
            "cpu_pct": svc["cpu_pct"],
        }
    ecs = session.client("ecs")
    resp = ecs.describe_services(cluster=args["cluster"], services=[args["service"]])
    if not resp.get("services"):
        return {"found": False}
    s = resp["services"][0]
    return {
        "found": True,
        "cluster": args["cluster"],
        "service": s["serviceName"],
        "desired_count": s["desiredCount"],
        "running_count": s["runningCount"],
        "pending_count": s["pendingCount"],
        "task_definition": s["taskDefinition"].rsplit("/", 1)[-1],
        "deployments": len(s.get("deployments", [])),
        "status": s.get("status"),
    }
