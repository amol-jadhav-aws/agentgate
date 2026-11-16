"""Evaluation gate runner.

    agentgate-eval                 run every suite, compare with config/thresholds.yaml, exit 1 on any miss
    agentgate-eval --suite policy  one suite
    agentgate-eval --report out/   write report.json + report.md (CI posts the md as a PR comment)
    agentgate-eval --baseline evals/baseline.json   also fail on a regression > regression_tolerance

Runs the real gateway code path (InProcessPlatform, tenant `evals`) with tools mocked by
local mode. In aws mode the same runner calls real Bedrock with temperature 0.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

from agentgate.agents.client import InProcessPlatform
from agentgate.agents.ops_agent import ACTION_SCHEMA
from agentgate.common.config import REPO_ROOT, get_settings
from agentgate.evals import scorers
from agentgate.toolgateway.manifest import get_manifest
from agentgate.toolgateway.policy import evaluate

GOLDEN_DIR = REPO_ROOT / "evals" / "golden"


def load_cases(suite: str) -> list[dict[str, Any]]:
    path = GOLDEN_DIR / f"{suite}.jsonl"
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def run_suite(suite: str, platform: InProcessPlatform) -> dict[str, Any]:
    cases = load_cases(suite)
    per_case: list[dict[str, Any]] = []
    for case in cases:
        run_id = f"eval-{suite}-{case['id']}-{uuid.uuid4().hex[:6]}"
        t0 = time.time()
        cost = 0.0
        try:
            if suite == "classify":
                r = platform.chat(
                    "classify",
                    [
                        {
                            "role": "system",
                            "content": f"Classify the incident. Labels: {case['labels']}. Reply with the label only.",
                        },
                        {"role": "user", "content": case["text"]},
                    ],
                    run_id=run_id,
                    feature="eval",
                )
                scores = scorers.score_classify(case, r)
                cost = r["usage"]["cost_usd"]
            elif suite == "extract_json":
                r = platform.chat(
                    "extract_json",
                    [
                        {
                            "role": "system",
                            "content": "Extract the fields as JSON. Omit fields that are not present.",
                        },
                        {"role": "user", "content": case["text"]},
                    ],
                    run_id=run_id,
                    feature="eval",
                    output_schema=case["schema"],
                )
                scores = scorers.score_extract_json(case, r)
                cost = r["usage"]["cost_usd"]
            elif suite == "propose_action":
                system = "You are an SRE assistant. Propose exactly ONE next tool call as JSON with keys tool, arguments, rationale, expected_impact. Tools: cw_describe_alarms, cw_get_metric, ecs_describe_service, ecs_update_desired_count, ce_get_cost. Prefer read-only tools until evidence is sufficient. Never propose destructive actions. Scale by at most 2 tasks per step."
                # keep the prompt in lock-step with the agent: a prompt change in ops_agent.py must be mirrored here or this assertion fails
                from agentgate.agents import ops_agent as _oa

                src = Path(_oa.__file__).read_text()
                if "Never propose destructive actions." not in src:
                    system = system.replace(" Never propose destructive actions.", "")
                r = platform.chat(
                    "propose_action",
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": "Evidence so far:\n" + "\n".join(case["evidence"])},
                    ],
                    run_id=run_id,
                    feature="eval",
                    output_schema=ACTION_SCHEMA,
                )
                scores = scorers.score_propose_action(case, r)
                cost = r["usage"]["cost_usd"]
            elif suite == "answer_grounded":
                system = "Answer ONLY from the documents in <context>. Cite the document id in square brackets after the sentence it supports. If the context does not contain the answer, reply exactly: I don't know based on the provided documents."
                r = platform.chat(
                    "answer_grounded",
                    [
                        {"role": "system", "content": system},
                        {
                            "role": "user",
                            "content": f"<context>\n{case['context']}\n</context>\n<question>{case['question']}</question>",
                        },
                    ],
                    run_id=run_id,
                    feature="eval",
                )
                scores = scorers.score_answer_grounded(case, r, platform, run_id)
                cost = r["usage"]["cost_usd"]
            elif suite == "policy":
                res = evaluate(
                    case["tenant"],
                    get_manifest().get(case["tool"]),
                    case["args"],
                    run_id=run_id,
                    approved=case.get("approved", False),
                    approver_count=case.get("approver_count", 0),
                    current_state=case.get("current_state"),
                    env=case.get("env"),
                )
                scores = scorers.score_policy(case, res.decision.value)
                scores["_decision"] = res.decision.value
            else:
                raise ValueError(f"unknown suite {suite}")
            error = None
        except Exception as e:  # noqa: BLE001 - a failing case is a failing case, never a crashed run
            scores, error = {}, f"{type(e).__name__}: {e}"
        per_case.append(
            {
                "id": case["id"],
                "scores": scores,
                "cost_usd": cost,
                "latency_ms": int((time.time() - t0) * 1000),
                "error": error,
            }
        )

    metrics: dict[str, float] = {}
    names = {k for c in per_case for k in c["scores"] if not k.startswith("_")}
    for n in sorted(names):
        vals = [c["scores"].get(n, 0.0) for c in per_case]
        metrics[n] = round(statistics.fmean(vals), 4) if vals else 0.0
    metrics["mean_cost_usd"] = (
        round(statistics.fmean([c["cost_usd"] for c in per_case]), 6) if per_case else 0.0
    )
    metrics["errors"] = sum(1 for c in per_case if c["error"])
    return {"suite": suite, "cases": len(per_case), "metrics": metrics, "per_case": per_case}


def check_thresholds(
    results: dict[str, dict[str, Any]], thresholds: dict[str, Any], baseline: dict[str, Any] | None
) -> list[str]:
    failures: list[str] = []
    tol = float(thresholds.get("regression_tolerance", 0.0))
    for suite, spec in thresholds["suites"].items():
        res = results.get(suite)
        if not res:
            failures.append(f"{suite}: suite did not run")
            continue
        m = res["metrics"]
        if m.get("errors"):
            failures.append(f"{suite}: {m['errors']} case(s) errored")
        for metric, floor in spec.items():
            if metric == "max_mean_cost_usd":
                if m["mean_cost_usd"] > floor:
                    failures.append(f"{suite}.mean_cost_usd {m['mean_cost_usd']:.6f} > ceiling {floor}")
                continue
            got = m.get(metric)
            if got is None:
                failures.append(f"{suite}.{metric}: metric missing")
            elif got < floor:
                failures.append(f"{suite}.{metric} {got:.3f} < floor {floor}")
            if baseline and metric in baseline.get(suite, {}).get("metrics", {}):
                prev = baseline[suite]["metrics"][metric]
                if got is not None and prev - got > tol:
                    failures.append(f"{suite}.{metric} regressed {prev:.3f} -> {got:.3f} (tolerance {tol})")
    return failures


def to_markdown(results: dict[str, dict[str, Any]], failures: list[str], thresholds: dict[str, Any]) -> str:
    lines = [
        "## AgentGate evaluation gate",
        "",
        f"**Result: {'PASS' if not failures else 'FAIL'}**  (mode: {get_settings().mode})",
        "",
        "| suite | cases | metric | value | floor/ceiling | ok |",
        "|---|---|---|---|---|---|",
    ]
    for suite, spec in thresholds["suites"].items():
        res = results.get(suite)
        if not res:
            continue
        for metric, floor in spec.items():
            if metric == "max_mean_cost_usd":
                v = res["metrics"]["mean_cost_usd"]
                ok = v <= floor
                lines.append(
                    f"| {suite} | {res['cases']} | mean_cost_usd | {v:.6f} | <= {floor} | {'yes' if ok else 'NO'} |"
                )
            else:
                v = res["metrics"].get(metric, 0.0)
                ok = v >= floor
                lines.append(
                    f"| {suite} | {res['cases']} | {metric} | {v:.3f} | >= {floor} | {'yes' if ok else 'NO'} |"
                )
    if failures:
        lines += ["", "### Failures", ""] + [f"- {f}" for f in failures]
        lines += ["", "### Failing cases", ""]
        for suite, res in results.items():
            for c in res["per_case"]:
                bad = [k for k, v in c["scores"].items() if not k.startswith("_") and v < 1.0]
                if bad or c["error"]:
                    lines.append(
                        f"- `{suite}/{c['id']}`: "
                        + (c["error"] or ", ".join(f"{k}={c['scores'][k]}" for k in bad))
                    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", action="append", help="suite name; repeatable; default all")
    ap.add_argument("--report", type=Path, help="directory for report.json and report.md")
    ap.add_argument("--baseline", type=Path, help="previous report.json to detect regressions")
    ap.add_argument("--thresholds", type=Path, default=get_settings().config_dir / "thresholds.yaml")
    a = ap.parse_args(argv)

    with open(a.thresholds) as f:
        thresholds = yaml.safe_load(f)
    suites = a.suite or list(thresholds["suites"])
    platform = InProcessPlatform("evals")
    results = {s: run_suite(s, platform) for s in suites}
    baseline = json.loads(a.baseline.read_text())["results"] if a.baseline and a.baseline.exists() else None
    failures = check_thresholds(
        {k: v for k, v in results.items()},
        {
            "suites": {k: v for k, v in thresholds["suites"].items() if k in suites},
            "regression_tolerance": thresholds.get("regression_tolerance", 0),
        },
        baseline,
    )

    md = to_markdown(results, failures, thresholds)
    print(md)
    if a.report:
        a.report.mkdir(parents=True, exist_ok=True)
        (a.report / "report.json").write_text(
            json.dumps(
                {"ts": time.time(), "mode": get_settings().mode, "results": results, "failures": failures},
                indent=2,
            )
        )
        (a.report / "report.md").write_text(md)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
