from agentgate.agents.client import InProcessPlatform
from agentgate.agents.knowledge_agent import KnowledgeAgent
from agentgate.agents.ops_agent import OpsAgent
from agentgate.toolgateway.tools import fakeaws

CPU_TRIGGER = {
    "kind": "alarm",
    "metric": "CPUUtilization",
    "cluster": "demo",
    "service": "orders-api",
    "alarm": "orders-api-cpu-high",
}


def test_ops_agent_pauses_for_approval_and_recovers():
    fakeaws.spike_cpu("demo", "orders-api", 93.0)
    p = InProcessPlatform("ops-agent")
    agent = OpsAgent(p)
    r = agent.start(CPU_TRIGGER, run_id="run-cpu")
    assert r["status"] == "awaiting_approval"
    assert (
        r["approval"]["tool"] == "ecs_update_desired_count"
        and r["approval"]["arguments"]["desired_count"] == 4
    )
    assert fakeaws.service("demo", "orders-api")["desired"] == 2  # paused, nothing changed
    tok = p.decide_approval(r["approval"]["approval_id"], "amol", True)["token"]
    r2 = agent.resume("run-cpu", tok)
    rep = r2["report"]
    assert r2["status"] == "done" and rep["recovered"] is True
    assert rep["executed"][0]["approvers"] == ["amol"]
    assert rep["model_calls"] == 1 and rep["tool_calls"] >= 4
    assert rep["total_cost_usd"] < 0.01
    assert fakeaws.service("demo", "orders-api")["cpu_pct"] < 80


def test_ops_agent_rejection_replans_and_stops_within_step_limit():
    fakeaws.spike_cpu("demo", "orders-api", 93.0)
    p = InProcessPlatform("ops-agent")
    agent = OpsAgent(p, max_steps=2)
    r = agent.start(CPU_TRIGGER, run_id="run-rej")
    r = agent.resume("run-rej", None)  # approver says no
    # the agent proposes again (same evidence), pauses again; a second rejection hits the step limit
    if r["status"] == "awaiting_approval":
        r = agent.resume("run-rej", "reject")
    assert r["status"] == "done"
    assert r["report"]["recovered"] is False and r["report"]["steps"] <= 2
    assert fakeaws.service("demo", "orders-api")["desired"] == 2


def test_ops_agent_cost_path_is_read_only():
    fakeaws.spike_cost("AmazonBedrock", 6.0)
    p = InProcessPlatform("ops-agent")
    r = OpsAgent(p).start({"kind": "cost", "text": "spend jumped"}, run_id="run-cost")
    assert r["status"] == "done" and r["report"]["recovered"] is True
    assert {e["tool"] for e in r["report"]["executed"]} == {"ce_get_cost"}
    assert r["report"]["executed"][0]["output"]["anomalies"][0]["group"] == "AmazonBedrock"


def test_knowledge_agent_cites_and_refuses():
    p = InProcessPlatform("knowledge-agent")
    agent = KnowledgeAgent(p)
    a = agent.answer("When should an ECS service be scaled out and by how much?")
    assert a["citations"] == ["doc-runbook-cpu"] and a["groundedness"] >= 0.6
    b = agent.answer("What is the capital of France?")
    assert b["answer"].startswith("I don't know") and b["citations"] == []


def test_knowledge_agent_second_answer_is_cached():
    p = InProcessPlatform("knowledge-agent")
    agent = KnowledgeAgent(p)
    agent.answer("When should an ECS service be scaled out?")
    second = agent.answer("When should an ECS service be scaled out?")
    assert second["cached_answer"] is True
