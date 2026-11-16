"""ECS UpdateService desiredCount. Tier 2: reversible write, needs approval."""

from __future__ import annotations

from typing import Any

from agentgate.toolgateway.tools import fakeaws


def current_state(session: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Read before write so policy can check the scaling delta against the live value."""
    if session is None:
        svc = fakeaws.service(args["cluster"], args["service"])
        return {"current_count": svc["desired"]} if svc else {}
    ecs = session.client("ecs")
    resp = ecs.describe_services(cluster=args["cluster"], services=[args["service"]])
    return {"current_count": resp["services"][0]["desiredCount"]} if resp.get("services") else {}


def run(session: Any, args: dict[str, Any], run_id: str) -> dict[str, Any]:
    if session is None:
        if not fakeaws.service(args["cluster"], args["service"]):
            return {"ok": False, "error": "no such service"}
        out = fakeaws.apply_scale(args["cluster"], args["service"], int(args["desired_count"]))
        fakeaws.record("ecs.update_service", run_id=run_id, **args)
        return {"ok": True, **out}
    ecs = session.client("ecs")
    before = ecs.describe_services(cluster=args["cluster"], services=[args["service"]])["services"][0][
        "desiredCount"
    ]
    resp = ecs.update_service(
        cluster=args["cluster"], service=args["service"], desiredCount=int(args["desired_count"])
    )
    return {
        "ok": True,
        "before": before,
        "after": resp["service"]["desiredCount"],
        "deployment_id": resp["service"]["deployments"][0]["id"],
    }
