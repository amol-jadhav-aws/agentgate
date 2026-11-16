"""CloudWatch GetMetricStatistics for one metric over the last N minutes. Tier 0."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from agentgate.toolgateway.tools import fakeaws


def run(session: Any, args: dict[str, Any], run_id: str) -> dict[str, Any]:
    minutes = int(args.get("minutes", 15))
    stat = args.get("stat", "Average")
    dims = args["dimensions"]
    if session is None:
        svc = fakeaws.service(dims.get("ClusterName", ""), dims.get("ServiceName", ""))
        if not svc:
            return {"datapoints": [], "summary": "no such service"}
        value = svc["cpu_pct"] if args["metric_name"] == "CPUUtilization" else 0.0
        points = [{"minute": -m, "value": value} for m in range(minutes, 0, -5)]
        return {
            "metric": args["metric_name"],
            "stat": stat,
            "window_minutes": minutes,
            "datapoints": points,
            "latest": value,
            "summary": f"{args['metric_name']} {stat} {value} over {minutes} min",
        }
    cw = session.client("cloudwatch")
    end = datetime.now(UTC)
    resp = cw.get_metric_statistics(
        Namespace=args["namespace"],
        MetricName=args["metric_name"],
        Dimensions=[{"Name": k, "Value": v} for k, v in dims.items()],
        StartTime=end - timedelta(minutes=minutes),
        EndTime=end,
        Period=60,
        Statistics=[stat] if stat != "p99" else [],
        ExtendedStatistics=["p99"] if stat == "p99" else [],
    )
    pts = sorted(resp.get("Datapoints", []), key=lambda d: d["Timestamp"])
    values = [p.get(stat, p.get("ExtendedStatistics", {}).get("p99")) for p in pts]
    latest = values[-1] if values else None
    return {
        "metric": args["metric_name"],
        "stat": stat,
        "window_minutes": minutes,
        "datapoints": [
            {"ts": p["Timestamp"].isoformat(), "value": v} for p, v in zip(pts, values, strict=False)
        ],
        "latest": latest,
        "summary": f"{args['metric_name']} {stat} latest={latest} over {minutes} min",
    }
