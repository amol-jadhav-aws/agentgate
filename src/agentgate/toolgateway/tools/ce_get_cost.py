"""Cost Explorer GetCostAndUsage, daily, grouped by SERVICE or a tag. Tier 1 (billed API)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from agentgate.toolgateway.tools import fakeaws


def _anomalies(groups: dict[str, list[float]]) -> list[dict[str, Any]]:
    out = []
    for name, series in groups.items():
        if len(series) < 3:
            continue
        base = sorted(series[:-1])[len(series[:-1]) // 2]  # median of earlier days
        if base > 0 and series[-1] / base >= 2.0:
            out.append(
                {"group": name, "latest": series[-1], "baseline": base, "factor": round(series[-1] / base, 1)}
            )
    return out


def run(session: Any, args: dict[str, Any], run_id: str) -> dict[str, Any]:
    days = int(args["days"])
    if session is None:
        groups = {k: v[-days:] for k, v in fakeaws.WORLD["cost_daily"].items()}
        return {
            "group_by": args["group_by"],
            "days": days,
            "daily": groups,
            "total_usd": round(sum(sum(v) for v in groups.values()), 2),
            "anomalies": _anomalies(groups),
        }
    ce = session.client("ce", region_name="us-east-1")
    end = datetime.now(UTC).date()
    start = end - timedelta(days=days)
    group = (
        {"Type": "DIMENSION", "Key": "SERVICE"}
        if args["group_by"] == "SERVICE"
        else {"Type": "TAG", "Key": args["tag_key"]}
    )
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        GroupBy=[group],
    )
    groups: dict[str, list[float]] = {}
    for day in resp["ResultsByTime"]:
        for g in day["Groups"]:
            groups.setdefault(g["Keys"][0], []).append(
                round(float(g["Metrics"]["UnblendedCost"]["Amount"]), 4)
            )
    return {
        "group_by": args["group_by"],
        "days": days,
        "daily": groups,
        "total_usd": round(sum(sum(v) for v in groups.values()), 2),
        "anomalies": _anomalies(groups),
    }
