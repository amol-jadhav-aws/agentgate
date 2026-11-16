"""Knowledge agent: grounded Q&A over the tenant's knowledge base.

    question -> rewrite (model, cached) -> retrieve (kb_search via tool gateway, tenant filter)
             -> answer (model, grounded, context_hash in cache key) -> self-check (judge)

Two properties the eval gate enforces: the answer cites a document id, and with no
retrieved context the agent says it does not know instead of inventing an answer.
"""

from __future__ import annotations

import uuid
from typing import Any

from agentgate.agents.client import Platform
from agentgate.gateway.service import context_hash


class KnowledgeAgent:
    def __init__(self, platform: Platform, top_k: int = 4, min_groundedness: float = 0.6):
        self.p = platform
        self.top_k = top_k
        self.min_groundedness = min_groundedness

    def answer(self, question: str, run_id: str | None = None) -> dict[str, Any]:
        run_id = run_id or uuid.uuid4().hex
        # 1. rewrite the question into a retrieval query (cheap, cached)
        rw = self.p.chat(
            "summarize",
            [
                {
                    "role": "system",
                    "content": "Rewrite the user's question as a short keyword search query. Reply with the query only.",
                },
                {"role": "user", "content": question},
            ],
            run_id=run_id,
            feature="rewrite",
        )
        query = rw["content"].strip() or question

        # 2. retrieve through the tool gateway; the tenant filter is applied from identity
        hits = self.p.invoke(
            "kb_search", {"query": query, "top_k": self.top_k}, run_id=run_id, feature="retrieve"
        )["output"]["hits"]
        context = "\n".join(f"[{h['id']}] {h['text']}" for h in hits)
        ch = context_hash(context)

        # 3. answer, grounded only in the context; cache key includes the context hash
        ans = self.p.chat(
            "answer_grounded",
            [
                {
                    "role": "system",
                    "content": "Answer ONLY from the documents in <context>. Cite the document id in square brackets after the sentence it supports. If the context does not contain the answer, reply exactly: I don't know based on the provided documents.",
                },
                {
                    "role": "user",
                    "content": f"<context>\n{context}\n</context>\n<question>{question}</question>",
                },
            ],
            run_id=run_id,
            feature="answer",
            context_hash=ch,
        )
        text = ans["content"].strip()

        # 4. self-check with the judge capability; low groundedness downgrades to a refusal
        judge = self.p.chat(
            "judge",
            [
                {
                    "role": "system",
                    "content": 'Score 0..1 how well the answer is supported by the context. Reply as JSON {"score": number, "reason": string}.',
                },
                {"role": "user", "content": f"<context>\n{context}\n</context>\n<answer>{text}</answer>"},
            ],
            run_id=run_id,
            feature="self_check",
            output_schema={
                "type": "object",
                "properties": {"score": {"type": "number", "minimum": 0, "maximum": 1}},
                "required": ["score"],
            },
        )
        score = float(judge["parsed"]["score"])
        grounded = score >= self.min_groundedness or text.lower().startswith("i don't know")
        if not grounded:
            text = "I don't know based on the provided documents."
        cost = self.p.run_cost(run_id)
        return {
            "run_id": run_id,
            "question": question,
            "query": query,
            "answer": text,
            "citations": [h["id"] for h in hits if f"[{h['id']}]" in text],
            "retrieved": [h["id"] for h in hits],
            "groundedness": score,
            "cached_answer": ans.get("cached", False),
            "total_cost_usd": cost["total_cost_usd"],
        }
