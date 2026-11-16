"""ECS DeleteService. Tier 3: irreversible. Policy forbids it in prod outright."""

from __future__ import annotations

from typing import Any

from agentgate.toolgateway.tools import fakeaws


def run(session: Any, args: dict[str, Any], run_id: str) -> dict[str, Any]:
    if session is None:
        key = f"{args['cluster']}/{args['service']}"
        existed = fakeaws.WORLD["ecs"].pop(key, None) is not None
        fakeaws.record("ecs.delete_service", run_id=run_id, **args)
        return {"ok": existed, "deleted": key if existed else None}
    ecs = session.client("ecs")
    resp = ecs.delete_service(cluster=args["cluster"], service=args["service"], force=True)
    return {"ok": True, "deleted": resp["service"]["serviceArn"]}
