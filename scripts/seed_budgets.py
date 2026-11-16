#!/usr/bin/env python3
"""Create the DynamoDB budget rows for the current month from config/budgets.yaml (aws mode).

Idempotent: existing spend is kept, limits live in the YAML and are read at request time.
"""

from agentgate.common.ledger import get_ledger, month_key

ledger = get_ledger()
for tenant, cfg in ledger.budgets.get("tenants", {}).items():
    row = ledger.store.get(f"budget#{tenant}", month_key()) or {}
    ledger.store.put(
        f"budget#{tenant}",
        month_key(),
        {
            **row,
            "limit_usd": cfg["monthly_usd"],
            "owner": cfg.get("owner"),
            "cost_center": cfg.get("cost_center"),
        },
    )
    print(f"{tenant:18s} limit {cfg['monthly_usd']:>6} USD  spent {float(row.get('spent_usd', 0)):.4f}")
