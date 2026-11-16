"""In-memory stand-in for the AWS resources the tools touch, used in local mode.

The chaos scripts mutate this world (CPU spike, cost spike) and the Ops agent repairs it
through the tool gateway, so the whole incident loop runs offline. State is process-wide;
`reset()` puts the demo environment back.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()


def _default_world() -> dict[str, Any]:
    return {
        "ecs": {
            "demo/orders-api": {
                "cluster": "demo",
                "service": "orders-api",
                "desired": 2,
                "running": 2,
                "task_def": "orders-api:17",
                "cpu_pct": 32.0,
            },
            "demo/payments": {
                "cluster": "demo",
                "service": "payments",
                "desired": 3,
                "running": 3,
                "task_def": "payments:9",
                "cpu_pct": 41.0,
            },
        },
        "alarms": [],
        "cost_daily": {
            "AmazonECS": [1.9, 2.0, 1.95, 2.1, 2.0, 2.05, 2.0],
            "AmazonBedrock": [0.4, 0.5, 0.45, 0.5, 0.6, 0.55, 0.5],
            "AmazonDynamoDB": [0.1] * 7,
        },
        "kb": {
            "knowledge-agent": [
                {
                    "id": "doc-runbook-cpu",
                    "text": "Runbook: when CPUUtilization stays above 80% for 15 minutes on an ECS service, scale out by two tasks and re-check after five minutes. Never scale beyond 20 tasks without a capacity review.",
                },
                {
                    "id": "doc-budget-policy",
                    "text": "Budget policy: each agent tenant has a monthly token budget. A refused call costs nothing. Budgets are raised by pull request to config/budgets.yaml.",
                },
                {
                    "id": "doc-approvals",
                    "text": "Approval policy: tier 2 actions need one human approval within ten minutes. Tier 3 actions are forbidden in prod and need two approvers elsewhere.",
                },
            ],
            "finance-agent": [
                {
                    "id": "doc-fin-tags",
                    "text": "Cost allocation: every Bedrock call carries tenant and feature tags. Finance reports spend per feature weekly.",
                },
            ],
        },
        "events": [],
    }


WORLD: dict[str, Any] = _default_world()


def reset() -> None:
    with _lock:
        WORLD.clear()
        WORLD.update(_default_world())


def record(event: str, **data: Any) -> None:
    with _lock:
        WORLD["events"].append({"ts": time.time(), "event": event, **data})


def service(cluster: str, name: str) -> dict[str, Any] | None:
    return WORLD["ecs"].get(f"{cluster}/{name}")


def spike_cpu(cluster: str, name: str, pct: float = 93.0) -> None:
    with _lock:
        svc = WORLD["ecs"][f"{cluster}/{name}"]
        svc["cpu_pct"] = pct
        WORLD["alarms"] = [a for a in WORLD["alarms"] if a["name"] != f"{name}-cpu-high"] + [
            {
                "name": f"{name}-cpu-high",
                "state": "ALARM",
                "metric": "CPUUtilization",
                "namespace": "AWS/ECS",
                "dimensions": {"ClusterName": cluster, "ServiceName": name},
                "threshold": 80,
                "reason": f"CPUUtilization {pct}% > 80% for 15 datapoints",
            }
        ]
    record("chaos.cpu_spike", cluster=cluster, service=name, pct=pct)


def spike_cost(service_name: str = "AmazonBedrock", factor: float = 6.0) -> None:
    with _lock:
        series = WORLD["cost_daily"][service_name]
        series[-1] = round(series[-2] * factor, 2)
    record("chaos.cost_spike", service=service_name, factor=factor)


def apply_scale(cluster: str, name: str, desired: int) -> dict[str, Any]:
    """Scaling spreads load: cpu falls roughly in proportion to task count; alarm clears below 80."""
    with _lock:
        svc = WORLD["ecs"][f"{cluster}/{name}"]
        before = svc["desired"]
        svc["desired"] = desired
        svc["running"] = desired
        if desired > 0:
            svc["cpu_pct"] = round(svc["cpu_pct"] * before / desired, 1)
        if svc["cpu_pct"] < 80:
            WORLD["alarms"] = [a for a in WORLD["alarms"] if a["name"] != f"{name}-cpu-high"] + (
                [
                    {
                        "name": f"{name}-cpu-high",
                        "state": "OK",
                        "metric": "CPUUtilization",
                        "namespace": "AWS/ECS",
                        "dimensions": {"ClusterName": cluster, "ServiceName": name},
                        "threshold": 80,
                        "reason": f"CPUUtilization {svc['cpu_pct']}% < 80%",
                    }
                ]
            )
        return {"before": before, "after": desired, "cpu_pct": svc["cpu_pct"]}
