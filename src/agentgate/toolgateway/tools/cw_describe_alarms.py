"""CloudWatch DescribeAlarms, filtered by state. Tier 0."""

from __future__ import annotations

from typing import Any

from agentgate.toolgateway.tools import fakeaws


def run(session: Any, args: dict[str, Any], run_id: str) -> dict[str, Any]:
    state, prefix = args["state"], args.get("name_prefix", "")
    if session is None:
        alarms = [a for a in fakeaws.WORLD["alarms"] if a["state"] == state and a["name"].startswith(prefix)]
        return {"alarms": alarms, "count": len(alarms)}
    cw = session.client("cloudwatch")
    kwargs: dict[str, Any] = {"StateValue": state, "MaxRecords": 50}
    if prefix:
        kwargs["AlarmNamePrefix"] = prefix
    resp = cw.describe_alarms(**kwargs)
    alarms = [
        {
            "name": a["AlarmName"],
            "state": a["StateValue"],
            "metric": a.get("MetricName"),
            "namespace": a.get("Namespace"),
            "dimensions": {d["Name"]: d["Value"] for d in a.get("Dimensions", [])},
            "threshold": a.get("Threshold"),
            "reason": a.get("StateReason"),
        }
        for a in resp.get("MetricAlarms", [])
    ]
    return {"alarms": alarms, "count": len(alarms)}
