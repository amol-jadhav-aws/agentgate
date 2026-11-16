import time

from agentgate.toolgateway import approvals
from agentgate.toolgateway.tools import fakeaws
from tests.conftest import KB, OPS


def inv(tg, tool, args, headers=OPS, **extra):
    return tg.post(
        f"/v1/tools/{tool}/invoke",
        headers=headers,
        json={"arguments": args, "run_id": extra.pop("run_id", "r1"), **extra},
    )


def test_catalogue_hides_role_arns(tg):
    tools = tg.get("/v1/tools", headers=OPS).json()["tools"]
    assert {t["name"] for t in tools} >= {"cw_describe_alarms", "ecs_update_desired_count"}
    assert all("role_arn" not in t for t in tools)


def test_unknown_tool_and_bad_args(tg):
    assert inv(tg, "rm_rf", {}).status_code == 404
    r = inv(tg, "cw_describe_alarms", {"state": "ALARM", "extra": 1})
    assert r.status_code == 400 and "additional" in r.json()["detail"].lower()


def test_tier0_runs_without_approval(tg):
    fakeaws.spike_cpu("demo", "orders-api")
    r = inv(tg, "cw_describe_alarms", {"state": "ALARM"})
    assert r.status_code == 200 and r.json()["decision"] == "allow" and r.json()["output"]["count"] == 1


def test_tier2_requires_approval_then_executes(tg):
    args = {"cluster": "demo", "service": "orders-api", "desired_count": 4}
    r = inv(tg, "ecs_update_desired_count", args, rationale="cpu high")
    assert r.status_code == 202 and r.json()["decision"] == "approve"
    aid = r.json()["approval"]["approval_id"]
    assert fakeaws.service("demo", "orders-api")["desired"] == 2  # nothing happened yet
    d = tg.post(f"/v1/approvals/{aid}/decide", headers=OPS, json={"approver": "amol", "approve": True}).json()
    r2 = inv(tg, "ecs_update_desired_count", args, approval_token=d["token"])
    assert r2.status_code == 200 and r2.json()["output"]["after"] == 4 and r2.json()["approvers"] == ["amol"]
    assert fakeaws.service("demo", "orders-api")["desired"] == 4


def test_approval_token_is_bound_to_args_and_single_use(tg):
    args = {"cluster": "demo", "service": "orders-api", "desired_count": 4}
    aid = inv(tg, "ecs_update_desired_count", args).json()["approval"]["approval_id"]
    tok = tg.post(
        f"/v1/approvals/{aid}/decide", headers=OPS, json={"approver": "amol", "approve": True}
    ).json()["token"]
    changed = inv(tg, "ecs_update_desired_count", {**args, "desired_count": 5}, approval_token=tok)
    assert changed.status_code == 403 and "changed" in changed.json()["detail"]
    other_run = inv(tg, "ecs_update_desired_count", args, approval_token=tok, run_id="r2")
    assert other_run.status_code == 403 and "different run" in other_run.json()["detail"]
    assert inv(tg, "ecs_update_desired_count", args, approval_token=tok).status_code == 200
    reuse = inv(tg, "ecs_update_desired_count", args, approval_token=tok)
    assert reuse.status_code == 403 and "already used" in reuse.json()["detail"]


def test_tampered_token_rejected(tg):
    args = {"cluster": "demo", "service": "orders-api", "desired_count": 4}
    aid = inv(tg, "ecs_update_desired_count", args).json()["approval"]["approval_id"]
    tok = tg.post(
        f"/v1/approvals/{aid}/decide", headers=OPS, json={"approver": "amol", "approve": True}
    ).json()["token"]
    bad = tok.replace('"approvers":["amol"]', '"approvers":["amol","eve"]')
    assert inv(tg, "ecs_update_desired_count", args, approval_token=bad).status_code == 403


def test_approval_expires(tg, monkeypatch):
    from agentgate.common import config

    monkeypatch.setattr(config.get_settings(), "approval_ttl_seconds", 1)
    args = {"cluster": "demo", "service": "orders-api", "desired_count": 4}
    aid = inv(tg, "ecs_update_desired_count", args).json()["approval"]["approval_id"]
    time.sleep(1.2)
    r = tg.post(f"/v1/approvals/{aid}/decide", headers=OPS, json={"approver": "amol", "approve": True})
    assert r.status_code == 409 and "expired" in r.json()["detail"]
    monkeypatch.setattr(config.get_settings(), "approval_ttl_seconds", 600)


def test_rejection_closes_the_request(tg):
    args = {"cluster": "demo", "service": "orders-api", "desired_count": 4}
    aid = inv(tg, "ecs_update_desired_count", args).json()["approval"]["approval_id"]
    r = tg.post(
        f"/v1/approvals/{aid}/decide", headers=OPS, json={"approver": "amol", "approve": False}
    ).json()
    assert r["status"] == "rejected" and r["token"] is None
    assert approvals.get(aid)["status"] == "rejected"


def test_tier3_denied_for_ops_agent_even_with_two_approvals(tg):
    r = inv(tg, "ecs_delete_service", {"cluster": "demo", "service": "orders-api"})
    assert r.status_code == 403 and r.json()["error"] == "policy_denied"
    assert fakeaws.service("demo", "orders-api") is not None


def test_scaling_delta_guard(tg):
    r = inv(tg, "ecs_update_desired_count", {"cluster": "demo", "service": "orders-api", "desired_count": 15})
    assert r.status_code == 403 and "policy5" in r.json()["detail"]


def test_kb_tenant_filter_comes_from_identity(tg):
    r = inv(tg, "kb_search", {"query": "cost allocation finance tags"}, headers=KB)
    assert r.status_code == 200
    assert r.json()["output"]["tenant"] == "knowledge-agent"
    assert all(h["id"] != "doc-fin-tags" for h in r.json()["output"]["hits"])  # finance docs invisible


def test_output_redaction(tg, monkeypatch):
    from agentgate.toolgateway.tools import cw_describe_alarms

    def leaky(session, args, run_id):
        return {
            "alarms": [{"reason": "owner bob@example.com key AKIAABCDEFGHIJKLMNOP account 123456789012"}],
            "count": 1,
        }

    monkeypatch.setattr(cw_describe_alarms, "run", leaky)
    from agentgate.toolgateway import tools as tmod

    tmod._REGISTRY.pop("cw_describe_alarms", None)
    r = inv(tg, "cw_describe_alarms", {"state": "ALARM"}).json()
    text = r["output"]["alarms"][0]["reason"]
    assert "bob@" not in text and "AKIA" not in text and "123456789012" not in text
    assert r["redactions"] == {"aws_access_key": 1, "aws_account_id": 1, "email": 1}
    tmod._REGISTRY.pop("cw_describe_alarms", None)


def test_audit_trail_has_every_decision(tg):
    inv(tg, "cw_describe_alarms", {"state": "OK"})
    inv(tg, "ecs_delete_service", {"cluster": "demo", "service": "orders-api"})
    rows = tg.get("/v1/audit", headers=OPS).json()["rows"]
    assert [r["decision"] for r in rows] == ["allow", "deny"]
