"""Cost ledger and budgets.

Every model call and every tool call writes one ledger row. Budgets are checked and
consumed atomically before the call is made. Prices are per 1K tokens in USD and
live in config/capabilities.yaml next to the model they describe, so a price change
is a reviewed PR, not a code change.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import yaml

from agentgate.common.config import get_settings
from agentgate.common.store import BudgetExceeded, get_store


@dataclass
class LedgerRow:
    tenant: str
    feature: str
    run_id: str
    kind: str  # "model" | "tool"
    name: str  # model id or tool name
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cached: bool = False
    guardrail_action: str = "NONE"
    latency_ms: int = 0
    ts: float = field(default_factory=time.time)
    row_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    extra: dict[str, Any] = field(default_factory=dict)


def _load_budgets() -> dict[str, Any]:
    path = get_settings().config_dir / "budgets.yaml"
    with open(path) as f:
        return yaml.safe_load(f) or {}


def month_key(ts: float | None = None) -> str:
    return time.strftime("%Y-%m", time.gmtime(ts or time.time()))


def price_for(model_id: str, input_tokens: int, output_tokens: int) -> float:
    from agentgate.gateway.routing import get_router

    model = get_router().model(model_id)
    return round(
        input_tokens / 1000 * model.price_in_per_1k + output_tokens / 1000 * model.price_out_per_1k, 6
    )


class Ledger:
    def __init__(self) -> None:
        self.store = get_store()
        self.budgets = _load_budgets()

    # -- budgets ----------------------------------------------------------------
    def tenant_limit(self, tenant: str) -> float:
        return float(
            self.budgets.get("tenants", {})
            .get(tenant, {})
            .get("monthly_usd", self.budgets.get("default_monthly_usd", 5.0))
        )

    def run_limit(self, tenant: str) -> float:
        return float(
            self.budgets.get("tenants", {})
            .get(tenant, {})
            .get("per_run_usd", get_settings().default_run_budget_usd)
        )

    def reserve(self, tenant: str, run_id: str, estimated_usd: float) -> None:
        """Consume budget before a call. Raises BudgetExceeded; nothing is spent when it does."""
        self.store.add_number(
            f"budget#{tenant}", month_key(), "spent_usd", estimated_usd, limit=self.tenant_limit(tenant)
        )
        try:
            self.store.add_number(
                f"budget#{tenant}", f"run#{run_id}", "spent_usd", estimated_usd, limit=self.run_limit(tenant)
            )
        except BudgetExceeded:
            # roll back the tenant reservation so a refused run does not eat monthly budget
            self.store.add_number(f"budget#{tenant}", month_key(), "spent_usd", -estimated_usd)
            raise

    def settle(self, tenant: str, run_id: str, estimated_usd: float, actual_usd: float) -> None:
        """Replace the estimate with the actual cost once tokens are known."""
        delta = actual_usd - estimated_usd
        if abs(delta) > 1e-9:
            self.store.add_number(f"budget#{tenant}", month_key(), "spent_usd", delta)
            self.store.add_number(f"budget#{tenant}", f"run#{run_id}", "spent_usd", delta)

    def spent(self, tenant: str) -> float:
        row = self.store.get(f"budget#{tenant}", month_key())
        return float(row.get("spent_usd", 0.0)) if row else 0.0

    def run_spent(self, tenant: str, run_id: str) -> float:
        row = self.store.get(f"budget#{tenant}", f"run#{run_id}")
        return float(row.get("spent_usd", 0.0)) if row else 0.0

    # -- ledger -----------------------------------------------------------------
    def record(self, row: LedgerRow) -> None:
        self.store.put(f"ledger#{row.tenant}", f"{int(row.ts * 1000):013d}#{row.row_id}", asdict(row))
        self.store.put(f"run#{row.run_id}", f"ledger#{int(row.ts * 1000):013d}#{row.row_id}", asdict(row))

    def rows_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self.store.query(f"run#{run_id}", "ledger#")

    def rows_for_tenant(self, tenant: str) -> list[dict[str, Any]]:
        return self.store.query(f"ledger#{tenant}")

    def summary(self, tenant: str) -> dict[str, Any]:
        rows = self.rows_for_tenant(tenant)
        by_feature: dict[str, float] = {}
        for r in rows:
            by_feature[r["feature"]] = round(by_feature.get(r["feature"], 0.0) + float(r["cost_usd"]), 6)
        return {
            "tenant": tenant,
            "month": month_key(),
            "spent_usd": round(self.spent(tenant), 6),
            "limit_usd": self.tenant_limit(tenant),
            "calls": len(rows),
            "cache_hits": sum(1 for r in rows if r.get("cached")),
            "by_feature": by_feature,
        }


_ledger: Ledger | None = None


def get_ledger() -> Ledger:
    global _ledger
    if _ledger is None or _ledger.store is not get_store():
        _ledger = Ledger()
    return _ledger
