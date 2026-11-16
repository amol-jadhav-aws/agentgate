"""Ops / FinOps agent: a LangGraph graph with deterministic gates around every model decision.

    alarm/request -> triage (code) -> gather (tools) -> propose (model, JSON) -> act (tool gateway)
                                                            ^                         |
                                                            |         approve? -> interrupt (checkpoint, Slack) -> resume
                                                            |                         |
                                                            +------ verify (code) <---+   -> report

Filled boxes in the architecture diagram = the deterministic nodes here. Only `propose`
calls a model, and it must return JSON that matches ACTION_SCHEMA or the gateway rejects it.
The agent never names a model, never holds AWS credentials and never bypasses policy.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agentgate.agents.client import Platform, PlatformError
from agentgate.common.config import get_settings

ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool": {"type": "string"},
        "arguments": {"type": "object"},
        "rationale": {"type": "string", "minLength": 10},
        "expected_impact": {"type": "string"},
    },
    "required": ["tool", "arguments", "rationale"],
    "additionalProperties": True,
}

RUNBOOK_FAMILIES = {
    "cpu_saturation": {"metric": "CPUUtilization", "verify_below": 80.0},
    "memory_pressure": {"metric": "MemoryUtilization", "verify_below": 85.0},
    "cost_anomaly": {"metric": None, "verify_below": None},
}


class OpsState(TypedDict, total=False):
    run_id: str
    trigger: dict[str, Any]  # {"kind": "alarm"|"question", "cluster", "service", "text"}
    family: str
    evidence: list[str]
    proposal: dict[str, Any] | None
    last_result: dict[str, Any] | None
    executed: list[dict[str, Any]]
    steps: int
    recovered: bool
    denied: list[str]
    report: dict[str, Any] | None
    halt_reason: str | None


class OpsAgent:
    def __init__(self, platform: Platform, checkpointer: Any | None = None, max_steps: int | None = None):
        self.p = platform
        self.max_steps = max_steps or get_settings().max_agent_steps
        self.graph = self._build(checkpointer or MemorySaver())

    # ------------------------------------------------------------------ nodes
    def triage(self, s: OpsState) -> OpsState:
        """Deterministic: pick the runbook family from the alarm metric; a model is only used for free text."""
        trig = s["trigger"]
        family = None
        metric = (trig.get("metric") or "").lower()
        if "cpu" in metric:
            family = "cpu_saturation"
        elif "memory" in metric:
            family = "memory_pressure"
        elif trig.get("kind") == "cost":
            family = "cost_anomaly"
        if family is None:
            r = self.p.chat(
                "classify",
                [
                    {
                        "role": "system",
                        "content": "Classify the incident. Labels: cpu_saturation, memory_pressure, cost_anomaly, deploy_failure, latency, unknown. Reply with the label only.",
                    },
                    {"role": "user", "content": trig.get("text", json.dumps(trig))},
                ],
                run_id=s["run_id"],
                feature="triage",
            )
            family = r["content"].strip()
        return {
            "family": family,
            "evidence": [f"trigger: {json.dumps(trig, sort_keys=True)}", f"runbook family: {family}"],
            "steps": 0,
            "executed": [],
            "denied": [],
            "recovered": False,
        }

    def gather(self, s: OpsState) -> OpsState:
        """Read-only evidence through the tool gateway. Tier 0/1 only; no approval needed."""
        trig, ev = s["trigger"], list(s["evidence"])
        try:
            alarms = self.p.invoke(
                "cw_describe_alarms", {"state": "ALARM"}, run_id=s["run_id"], feature="gather"
            )
            ev.append(
                f"alarms in ALARM: {alarms['output']['count']} -> "
                + ", ".join(a["name"] for a in alarms["output"]["alarms"])
            )
            if s["family"] == "cost_anomaly":
                cost = self.p.invoke(
                    "ce_get_cost", {"days": 7, "group_by": "SERVICE"}, run_id=s["run_id"], feature="gather"
                )
                ev.append(f"cost spike: {json.dumps(cost['output']['anomalies'])}")
            elif trig.get("cluster") and trig.get("service"):
                svc = self.p.invoke(
                    "ecs_describe_service",
                    {"cluster": trig["cluster"], "service": trig["service"]},
                    run_id=s["run_id"],
                    feature="gather",
                )
                o = svc["output"]
                ev.append(
                    f"service: {trig['service']} cluster: {trig['cluster']} desired_count: {o.get('desired_count')} running_count: {o.get('running_count')} task_def: {o.get('task_definition')}"
                )
                fam = RUNBOOK_FAMILIES.get(s["family"], {})
                if fam.get("metric"):
                    m = self.p.invoke(
                        "cw_get_metric",
                        {
                            "namespace": "AWS/ECS",
                            "metric_name": fam["metric"],
                            "dimensions": {"ClusterName": trig["cluster"], "ServiceName": trig["service"]},
                            "minutes": 15,
                            "stat": "Average",
                        },
                        run_id=s["run_id"],
                        feature="gather",
                    )
                    ev.append(
                        f"{fam['metric']} avg {m['output'].get('latest')}% over 15 min (cpu {m['output'].get('latest')})"
                    )
        except PlatformError as e:
            ev.append(f"gather failed: {e.code} {e.detail}")
        return {"evidence": ev}

    def propose(self, s: OpsState) -> OpsState:
        """The one model decision: exactly one next tool call, as schema-checked JSON."""
        catalogue = (
            "cw_describe_alarms, cw_get_metric, ecs_describe_service, ecs_update_desired_count, ce_get_cost"
        )
        denied = (
            f"\nActions already denied by policy (do not repeat): {s['denied']}" if s.get("denied") else ""
        )
        r = self.p.chat(
            "propose_action",
            [
                {
                    "role": "system",
                    "content": f"You are an SRE assistant. Propose exactly ONE next tool call as JSON with keys tool, arguments, rationale, expected_impact. Tools: {catalogue}. Prefer read-only tools until evidence is sufficient. Never propose destructive actions. Scale by at most 2 tasks per step.{denied}",
                },
                {"role": "user", "content": "Evidence so far:\n" + "\n".join(s["evidence"])},
            ],
            run_id=s["run_id"],
            feature="propose",
            output_schema=ACTION_SCHEMA,
        )
        return {"proposal": r["parsed"], "steps": s.get("steps", 0) + 1}

    def act(self, s: OpsState) -> OpsState:
        """Send the proposal through the tool gateway. APPROVE -> pause the graph at a checkpoint."""
        prop = s["proposal"]
        if not prop:
            return {"halt_reason": "no proposal"}
        kwargs = {
            "run_id": s["run_id"],
            "feature": "act",
            "rationale": prop.get("rationale", ""),
            "expected_impact": prop.get("expected_impact", ""),
            "evidence": s["evidence"][-6:],
        }
        try:
            res = self.p.invoke(prop["tool"], prop["arguments"], **kwargs)
        except PlatformError as e:
            if e.code == "policy_denied":
                return {
                    "denied": s.get("denied", [])
                    + [f"{prop['tool']} {json.dumps(prop['arguments'], sort_keys=True)}: {e.detail}"],
                    "last_result": {"decision": "deny", "detail": e.detail},
                    "evidence": s["evidence"] + [f"policy denied {prop['tool']}: {e.detail}"],
                }
            return {
                "halt_reason": f"{e.code}: {e.detail}",
                "last_result": {"decision": "error", "detail": e.detail},
            }

        if res["decision"] == "approve":
            approval = res["approval"]
            # Pause here. State is checkpointed; a human resumes with the approval token (or "reject").
            resume_value = interrupt(
                {
                    "approval_id": approval["approval_id"],
                    "tool": prop["tool"],
                    "arguments": prop["arguments"],
                    "rationale": prop.get("rationale"),
                    "expected_impact": prop.get("expected_impact"),
                    "evidence": s["evidence"][-6:],
                }
            )
            if not resume_value or resume_value == "reject":
                return {
                    "denied": s.get("denied", []) + [f"{prop['tool']} rejected by approver"],
                    "last_result": {"decision": "rejected"},
                    "evidence": s["evidence"] + [f"approver rejected {prop['tool']}"],
                }
            try:
                res = self.p.invoke(prop["tool"], prop["arguments"], approval_token=resume_value, **kwargs)
            except PlatformError as e:
                return {
                    "halt_reason": f"{e.code}: {e.detail}",
                    "last_result": {"decision": "error", "detail": e.detail},
                }

        executed = s.get("executed", []) + [
            {
                "tool": prop["tool"],
                "arguments": prop["arguments"],
                "output": res.get("output"),
                "approvers": res.get("approvers", []),
                "audit_id": res.get("audit_id"),
            }
        ]
        ev = s["evidence"] + [
            f"executed {prop['tool']} {json.dumps(prop['arguments'], sort_keys=True)} -> {json.dumps(res.get('output'))[:300]}"
        ]
        return {"executed": executed, "last_result": res, "evidence": ev}

    def verify(self, s: OpsState) -> OpsState:
        """Deterministic: re-read the metric that triggered the runbook. No model involved."""
        fam = RUNBOOK_FAMILIES.get(s.get("family", ""), {})
        trig = s["trigger"]
        last = s.get("last_result") or {}
        if s.get("family") == "cost_anomaly" and s.get("executed"):
            # FinOps path: a cost breakdown is the deliverable; recovered when anomalies are identified.
            out = s["executed"][-1].get("output") or {}
            return {
                "recovered": bool(out.get("anomalies")),
                "evidence": s["evidence"] + [f"anomalies identified: {out.get('anomalies')}"],
            }
        if not fam.get("metric") or not trig.get("service") or last.get("decision") != "allow":
            return {"recovered": False}
        if s["executed"] and s["executed"][-1]["tool"] in (
            "cw_get_metric",
            "ecs_describe_service",
            "cw_describe_alarms",
            "ce_get_cost",
        ):
            return {"recovered": False}  # read-only step; nothing to verify yet
        m = self.p.invoke(
            "cw_get_metric",
            {
                "namespace": "AWS/ECS",
                "metric_name": fam["metric"],
                "dimensions": {"ClusterName": trig["cluster"], "ServiceName": trig["service"]},
                "minutes": 5,
                "stat": "Average",
            },
            run_id=s["run_id"],
            feature="verify",
        )
        latest = m["output"].get("latest")
        ok = latest is not None and float(latest) < float(fam["verify_below"])
        return {
            "recovered": ok,
            "evidence": s["evidence"]
            + [
                f"verify: {fam['metric']} now {latest}% (target < {fam['verify_below']}) -> {'recovered' if ok else 'not yet'} (cpu {latest})"
            ],
        }

    def report(self, s: OpsState) -> OpsState:
        cost = self.p.run_cost(s["run_id"])
        rep = {
            "run_id": s["run_id"],
            "family": s.get("family"),
            "recovered": s.get("recovered", False),
            "steps": s.get("steps", 0),
            "executed": s.get("executed", []),
            "denied": s.get("denied", []),
            "halt_reason": s.get("halt_reason"),
            "total_cost_usd": cost["total_cost_usd"],
            "model_calls": sum(1 for r in cost["rows"] if r["kind"] == "model"),
            "tool_calls": sum(1 for r in cost["rows"] if r["kind"] == "tool"),
            "timeline": s.get("evidence", []),
        }
        return {"report": rep}

    # ------------------------------------------------------------------ routing
    def after_act(self, s: OpsState) -> Literal["verify", "propose", "report"]:
        if s.get("halt_reason"):
            return "report"
        last = s.get("last_result") or {}
        if last.get("decision") in ("deny", "rejected"):
            return "propose" if s.get("steps", 0) < self.max_steps else "report"
        return "verify"

    def after_verify(self, s: OpsState) -> Literal["propose", "report"]:
        if s.get("recovered") or s.get("steps", 0) >= self.max_steps:
            return "report"
        return "propose"

    def _build(self, checkpointer: Any):
        g = StateGraph(OpsState)
        for name in ("triage", "gather", "propose", "act", "verify", "report"):
            g.add_node(name, getattr(self, name))
        g.add_edge(START, "triage")
        g.add_edge("triage", "gather")
        g.add_edge("gather", "propose")
        g.add_edge("propose", "act")
        g.add_conditional_edges(
            "act", self.after_act, {"verify": "verify", "propose": "propose", "report": "report"}
        )
        g.add_conditional_edges("verify", self.after_verify, {"propose": "propose", "report": "report"})
        g.add_edge("report", END)
        return g.compile(checkpointer=checkpointer)

    # ------------------------------------------------------------------ API
    @staticmethod
    def _config(run_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": run_id}}

    def start(self, trigger: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
        """Run until done or until an approval is needed. Returns {"status": "done"|"awaiting_approval", ...}."""
        run_id = run_id or uuid.uuid4().hex
        out = self.graph.invoke({"run_id": run_id, "trigger": trigger}, self._config(run_id))
        return self._shape(run_id, out)

    def resume(self, run_id: str, approval_token: str | None) -> dict[str, Any]:
        """Continue a paused run with an approval token, or None/"reject" to refuse."""
        out = self.graph.invoke(Command(resume=approval_token or "reject"), self._config(run_id))
        return self._shape(run_id, out)

    def _shape(self, run_id: str, out: dict[str, Any]) -> dict[str, Any]:
        if "__interrupt__" in out and out["__interrupt__"]:
            return {
                "status": "awaiting_approval",
                "run_id": run_id,
                "approval": out["__interrupt__"][0].value,
            }
        return {"status": "done", "run_id": run_id, "report": out.get("report")}
