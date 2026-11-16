import subprocess
import sys

from agentgate.common.config import REPO_ROOT
from agentgate.evals.run import main


def test_eval_gate_passes_on_main(tmp_path):
    assert main(["--report", str(tmp_path)]) == 0
    assert (tmp_path / "report.md").read_text().startswith("## AgentGate evaluation gate")


def test_eval_gate_blocks_sabotage(tmp_path):
    cap = REPO_ROOT / "config" / "capabilities.yaml"
    original = cap.read_text()
    try:
        cap.write_text(
            original.replace(
                "propose_action:   { model: sonnet,     fallback: haiku, ",
                "propose_action:   { model: nova-micro, fallback: nova-micro,",
            )
        )
        from agentgate.gateway import routing

        routing.get_router.cache_clear()
        assert main(["--suite", "propose_action"]) == 1
    finally:
        cap.write_text(original)
        routing.get_router.cache_clear()


def test_golden_files_are_valid_jsonl():
    import json

    for f in (REPO_ROOT / "evals" / "golden").glob("*.jsonl"):
        for line in f.read_text().splitlines():
            if line.strip():
                assert "id" in json.loads(line), f


def test_cli_entrypoint_runs():
    out = subprocess.run(
        [sys.executable, "-m", "agentgate.agents.cli", "--inproc", "demo", "cpu", "--auto-approve", "ci"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert '"recovered": true' in out.stdout
