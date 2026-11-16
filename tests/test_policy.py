import pytest

from agentgate.toolgateway.manifest import get_manifest
from agentgate.toolgateway.policy import Decision, audit_rows, evaluate

m = get_manifest()


@pytest.mark.parametrize(
    "tenant,tool,args,kw,expected",
    [
        ("ops-agent", "cw_describe_alarms", {"state": "ALARM"}, {}, Decision.ALLOW),
        (
            "ops-agent",
            "ecs_update_desired_count",
            {"cluster": "demo", "service": "x", "desired_count": 4},
            {"current_state": {"current_count": 2}},
            Decision.APPROVE,
        ),
        (
            "ops-agent",
            "ecs_update_desired_count",
            {"cluster": "demo", "service": "x", "desired_count": 4},
            {"current_state": {"current_count": 2}, "approved": True, "approver_count": 1},
            Decision.ALLOW,
        ),
        (
            "ops-agent",
            "ecs_update_desired_count",
            {"cluster": "demo", "service": "x", "desired_count": 12},
            {"current_state": {"current_count": 2}},
            Decision.DENY,
        ),
        (
            "ops-agent",
            "ecs_update_desired_count",
            {"cluster": "demo", "service": "x", "desired_count": 0},
            {"current_state": {"current_count": 2}, "env": "prod"},
            Decision.DENY,
        ),
        ("ops-agent", "ecs_delete_service", {"cluster": "demo", "service": "x"}, {}, Decision.DENY),
        ("knowledge-agent", "cw_describe_alarms", {"state": "ALARM"}, {}, Decision.DENY),
        ("knowledge-agent", "kb_search", {"query": "hello world"}, {}, Decision.ALLOW),
        (
            "cli",
            "ecs_delete_service",
            {"cluster": "demo", "service": "x"},
            {"approved": True, "approver_count": 2},
            Decision.ALLOW,
        ),
        (
            "cli",
            "ecs_delete_service",
            {"cluster": "demo", "service": "x"},
            {"approved": True, "approver_count": 1},
            Decision.APPROVE,
        ),
        (
            "cli",
            "ecs_delete_service",
            {"cluster": "demo", "service": "x"},
            {"approved": True, "approver_count": 2, "env": "prod"},
            Decision.DENY,
        ),
    ],
)
def test_decisions(tenant, tool, args, kw, expected):
    r = evaluate(tenant, m.get(tool), args, run_id="t", **kw)
    assert r.decision is expected, r.reasons
    assert r.errors == []


def test_audit_row_written_before_decision_returned():
    evaluate(
        "ops-agent", m.get("ecs_delete_service"), {"cluster": "demo", "service": "x"}, run_id="run-audit"
    )
    rows = audit_rows("ops-agent")
    assert rows and rows[-1]["decision"] == "deny" and rows[-1]["run_id"] == "run-audit"


def test_policy_files_parse():
    from agentgate.toolgateway.policy import load_policies

    assert "forbid" in load_policies() and "permit" in load_policies()
