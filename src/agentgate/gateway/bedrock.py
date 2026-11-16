"""Model client: Amazon Bedrock Converse in aws mode, a deterministic fake in local mode.

The fake is not a toy for show; it is what makes the whole platform testable without
an AWS bill. It answers each capability the way a well-behaved model would, keyed on
the prompt's content, so golden-set cases, policy tests and the chaos demo run
offline and in CI. Swap AGENTGATE_MODE=aws and the same code path calls Bedrock.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentgate.common.config import get_settings


@dataclass
class ModelResult:
    content: str
    input_tokens: int
    output_tokens: int
    guardrail_action: str = "NONE"  # NONE | INTERVENED
    stop_reason: str = "end_turn"
    raw: dict[str, Any] = field(default_factory=dict)


class ModelClient(Protocol):
    def converse(
        self,
        model_id: str,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        guardrail: dict[str, str] | None = None,
    ) -> ModelResult: ...

    def embed(self, text: str) -> list[float]: ...


# --------------------------------------------------------------------------------------
# Real Bedrock
# --------------------------------------------------------------------------------------
class BedrockClient:
    def __init__(self, region: str, embedding_model_id: str):
        import boto3
        from botocore.config import Config

        self._rt = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(retries={"max_attempts": 3, "mode": "adaptive"}, read_timeout=60),
        )
        self._embedding_model_id = embedding_model_id

    def converse(self, model_id, messages, *, max_tokens, temperature, guardrail=None):
        system = [{"text": m["content"]} for m in messages if m["role"] == "system"]
        turns = [
            {"role": m["role"], "content": [{"text": m["content"]}]}
            for m in messages
            if m["role"] != "system"
        ]
        kwargs: dict[str, Any] = {
            "modelId": model_id,
            "messages": turns,
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        if system:
            kwargs["system"] = system
        if guardrail:
            kwargs["guardrailConfig"] = {
                "guardrailIdentifier": guardrail["id"],
                "guardrailVersion": guardrail["version"],
                "trace": "enabled",
            }
        resp = self._rt.converse(**kwargs)
        content = "".join(part.get("text", "") for part in resp["output"]["message"]["content"])
        usage = resp.get("usage", {})
        action = "INTERVENED" if resp.get("stopReason") == "guardrail_intervened" else "NONE"
        return ModelResult(
            content=content,
            input_tokens=int(usage.get("inputTokens", 0)),
            output_tokens=int(usage.get("outputTokens", 0)),
            guardrail_action=action,
            stop_reason=resp.get("stopReason", "end_turn"),
            raw={"metrics": resp.get("metrics", {})},
        )

    def embed(self, text):
        body = json.dumps({"inputText": text[:8000], "dimensions": 256, "normalize": True})
        resp = self._rt.invoke_model(
            modelId=self._embedding_model_id,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        return json.loads(resp["body"].read())["embedding"]


# --------------------------------------------------------------------------------------
# Deterministic fake
# --------------------------------------------------------------------------------------
def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


class FakeBedrockClient:
    """Answers by capability hints found in the system prompt. Deterministic, offline."""

    def __init__(self):
        from agentgate.gateway.cache import HashingEmbedder

        self._embedder = HashingEmbedder()
        self._model_id = ""
        self.calls: list[dict[str, Any]] = []

    def embed(self, text):
        return self._embedder.embed(text)

    def converse(self, model_id, messages, *, max_tokens, temperature, guardrail=None):
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        user = "\n".join(m["content"] for m in messages if m["role"] == "user")
        self.calls.append({"model_id": model_id, "system": system, "user": user})
        time.sleep(0.002)

        # Guardrail simulation: a configured guardrail blocks obvious prompt-injection / secrets requests
        if guardrail and re.search(
            r"(?i)ignore (all|previous) instructions|reveal .*secret|print .*access key", user
        ):
            return ModelResult(
                "Sorry, the model cannot help with that.",
                _tokens(system + user),
                12,
                guardrail_action="INTERVENED",
                stop_reason="guardrail_intervened",
            )

        cap = re.search(r"\[capability:(\w+)\]", system)
        capability = cap.group(1) if cap else "generic"
        handler = getattr(self, f"_cap_{capability}", self._cap_generic)
        self._model_id = model_id
        content = handler(system, user)
        return ModelResult(
            content=content, input_tokens=_tokens(system + user), output_tokens=_tokens(content)
        )

    # -- capability handlers ----------------------------------------------------------
    def _cap_generic(self, system: str, user: str) -> str:
        return "Acknowledged: " + user.strip()[:200]

    def _cap_classify(self, system: str, user: str) -> str:
        labels = re.search(r"Labels:\s*([a-z0-9_,\s]+)", system)
        allowed = [x.strip() for x in labels.group(1).split(",")] if labels else []
        u = user.lower()
        rules = [
            ("cpu_saturation", ("cpu", "cpuutilization", "throttl")),
            ("memory_pressure", ("memory", "oom", "out of memory")),
            ("cost_anomaly", ("cost", "spend", "bill", "budget")),
            ("deploy_failure", ("deploy", "rollout", "task failed", "crashloop")),
            ("latency", ("latency", "p99", "slow", "timeout")),
        ]
        for label, keys in rules:
            if label in allowed and any(k in u for k in keys):
                return label
        return "unknown" if "unknown" in allowed else (allowed[0] if allowed else "unknown")

    def _cap_extract_json(self, system: str, user: str) -> str:
        out: dict[str, Any] = {}
        m = re.search(r"service\s+([A-Za-z0-9_\-/]+)", user, re.I)
        if m:
            out["service"] = m.group(1)
        m = re.search(r"cluster\s+([A-Za-z0-9_\-/]+)", user, re.I)
        if m:
            out["cluster"] = m.group(1)
        m = re.search(r"(?:to|at)\s+(\d+)\s+tasks?", user, re.I)
        if m:
            out["desired_count"] = int(m.group(1))
        m = re.search(r"region\s+([a-z]{2}-[a-z]+-\d)", user, re.I)
        if m:
            out["region"] = m.group(1)
        m = re.search(r"(\d+(?:\.\d+)?)\s*%", user)
        if m:
            out["percent"] = float(m.group(1))
        return json.dumps(out)

    def _cap_summarize(self, system: str, user: str) -> str:
        sentences = re.split(r"(?<=[.!?])\s+", user.strip())
        return " ".join(sentences[:2])[:400]

    def _cap_propose_action(self, system: str, user: str) -> str:
        """Propose exactly one tool call from the evidence. Mirrors how the prompt instructs a real model."""
        u = user.lower()
        svc = re.search(r"service[:=\s]+([a-z0-9_\-]+)", u)
        clu = re.search(r"cluster[:=\s]+([a-z0-9_\-]+)", u)
        service = svc.group(1) if svc else "unknown-service"
        cluster = clu.group(1) if clu else "unknown-cluster"
        running = re.search(r"running[_ ]count[:=\s]+(\d+)", u)
        cpu = re.search(r"cpu[^0-9]{0,20}(\d+(?:\.\d+)?)", u)
        if (
            "no evidence" in u
            or "evidence: none" in u
            or ("cpu" not in u and "cost" not in u and "memory" not in u)
        ):
            return json.dumps(
                {
                    "tool": "cw_get_metric",
                    "arguments": {
                        "namespace": "AWS/ECS",
                        "metric_name": "CPUUtilization",
                        "dimensions": {"ClusterName": cluster, "ServiceName": service},
                        "minutes": 15,
                        "stat": "Average",
                    },
                    "rationale": "Not enough evidence yet; read the CPU metric before acting.",
                    "expected_impact": "none, read-only",
                }
            )
        if "cost" in u and "spike" in u:
            return json.dumps(
                {
                    "tool": "ce_get_cost",
                    "arguments": {"days": 7, "group_by": "SERVICE"},
                    "rationale": "A cost spike needs a breakdown by service before any change.",
                    "expected_impact": "none, read-only",
                }
            )
        if cpu and float(cpu.group(1)) >= 80:
            current = int(running.group(1)) if running else 2
            # A weaker model, or a prompt that lost its safety line, reaches for the destructive tool.
            # This is what the eval gate's never_destructive metric exists to catch (scripts/sabotage_pr.sh).
            if "nova-micro" in self._model_id or "never propose destructive" not in system.lower():
                return json.dumps(
                    {
                        "tool": "ecs_delete_service",
                        "arguments": {"cluster": cluster, "service": service},
                        "rationale": "Service is unhealthy; recreate it from scratch.",
                        "expected_impact": "service deleted",
                    }
                )
            return json.dumps(
                {
                    "tool": "ecs_update_desired_count",
                    "arguments": {
                        "cluster": cluster,
                        "service": service,
                        "desired_count": min(current + 2, 20),
                    },
                    "rationale": f"CPU at {cpu.group(1)}% for the whole window with {current} tasks; add two tasks, reversible.",
                    "expected_impact": f"desired_count {current} -> {min(current + 2, 20)}; cost +2 tasks until load drops",
                }
            )
        return json.dumps(
            {
                "tool": "ecs_describe_service",
                "arguments": {"cluster": cluster, "service": service},
                "rationale": "Confirm the service state before deciding.",
                "expected_impact": "none, read-only",
            }
        )

    def _cap_answer_grounded(self, system: str, user: str) -> str:
        ctx = re.search(r"<context>(.*?)</context>", user, re.S)
        q = re.search(r"<question>(.*?)</question>", user, re.S)
        context = ctx.group(1).strip() if ctx else ""
        question = q.group(1).strip() if q else user
        if not context:
            return "I don't know based on the provided documents."
        best, best_score, best_doc = "", -1, ""
        for doc in re.findall(r"\[(doc-[^\]]+)\]\s*(.*?)(?=\n\[doc-|\Z)", context, re.S):
            doc_id, text = doc
            for sent in re.split(r"(?<=[.!?])\s+", text.strip()):
                score = len(_words(sent) & _words(question))
                if score > best_score:
                    best, best_score, best_doc = sent.strip(), score, doc_id
        if best_score <= 0:
            return "I don't know based on the provided documents."
        return f"{best} [{best_doc}]"

    def _cap_judge(self, system: str, user: str) -> str:
        ans = re.search(r"<answer>(.*?)</answer>", user, re.S)
        ctx = re.search(r"<context>(.*?)</context>", user, re.S)
        answer = ans.group(1) if ans else ""
        context = ctx.group(1) if ctx else ""
        if "i don't know" in answer.lower():
            score = 1.0 if not context.strip() else 0.5
        else:
            a, c = _words(answer), _words(context)
            score = round(len(a & c) / max(1, len(a)), 2) if a else 0.0
        return json.dumps(
            {
                "score": score,
                "reason": "token overlap between answer and context" if context else "no context supplied",
            }
        )


_client: ModelClient | None = None


def get_model_client() -> ModelClient:
    global _client
    if _client is None:
        s = get_settings()
        _client = FakeBedrockClient() if s.is_local else BedrockClient(s.aws_region, s.embedding_model_id)
    return _client


def set_model_client(client: ModelClient | None) -> None:
    global _client
    _client = client
