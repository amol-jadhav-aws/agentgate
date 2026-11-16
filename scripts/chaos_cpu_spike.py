#!/usr/bin/env python3
"""Chaos: push an ECS service's CPU to 93% and raise its alarm.

Local mode mutates the in-memory fake world. AWS mode runs a CPU burner task in the
demo service (`stress-ng`) for the given minutes so the real alarm fires and EventBridge
triggers the Ops agent. Either way, the agent must bring CPU back under 80% through the
tool gateway, with a human approval on the scale-out.
"""

import argparse
import subprocess
import sys

from agentgate.common.config import get_settings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cluster", default="demo")
    ap.add_argument("--service", default="orders-api")
    ap.add_argument("--minutes", type=int, default=15)
    a = ap.parse_args()
    s = get_settings()
    if s.is_local:
        from agentgate.toolgateway.tools import fakeaws

        fakeaws.spike_cpu(a.cluster, a.service, 93.0)
        print(f"[local] {a.cluster}/{a.service} CPU -> 93%, alarm {a.service}-cpu-high in ALARM")
        return 0
    cmd = [
        "aws",
        "ecs",
        "run-task",
        "--cluster",
        a.cluster,
        "--launch-type",
        "FARGATE",
        "--task-definition",
        "agentgate-chaos-stress",
        "--overrides",
        f'{{"containerOverrides":[{{"name":"stress","command":["stress-ng","--cpu","0","--timeout","{a.minutes}m"]}}]}}',
        "--region",
        s.aws_region,
    ]
    print(" ".join(cmd))
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
