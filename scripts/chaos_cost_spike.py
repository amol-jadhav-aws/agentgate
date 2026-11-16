#!/usr/bin/env python3
"""Chaos: make yesterday's Bedrock cost look 6x normal so the FinOps path has something to find.

Local mode only (Cost Explorer cannot be faked in AWS); in AWS the equivalent demo is a
Budgets alert wired to EventBridge -> the Ops agent's `cost` trigger.
"""

from agentgate.toolgateway.tools import fakeaws

fakeaws.spike_cost("AmazonBedrock", 6.0)
print("[local] AmazonBedrock daily cost x6")
