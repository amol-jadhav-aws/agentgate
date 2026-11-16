"""Bedrock Knowledge Base retrieve with a tenant metadata filter. Tier 1.

Tenant isolation is a hard filter on the retrieval call, not a hint in the prompt:
`filter: {equals: {key: tenant, value: <tenant>}}` on S3 Vectors metadata.
"""

from __future__ import annotations

import os
import re
from typing import Any

from agentgate.toolgateway.tools import fakeaws


def _words(t: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", t.lower()))


def run(session: Any, args: dict[str, Any], run_id: str) -> dict[str, Any]:
    tenant = args.get("_tenant", "knowledge-agent")  # injected by the executor, never by the model
    top_k = int(args.get("top_k", 4))
    if session is None:
        q = _words(args["query"])
        docs = fakeaws.WORLD["kb"].get(tenant, [])
        scored = sorted(((len(q & _words(d["text"])), d) for d in docs), key=lambda x: -x[0])
        hits = [
            {"id": d["id"], "text": d["text"], "score": round(s / max(1, len(q)), 2)}
            for s, d in scored[:top_k]
            if s > 0
        ]
        return {"tenant": tenant, "hits": hits}
    kb_id = os.environ["AGENTGATE_KB_ID"]
    rt = session.client("bedrock-agent-runtime")
    resp = rt.retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": args["query"]},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": top_k,
                "filter": {"equals": {"key": "tenant", "value": tenant}},
            }
        },
    )
    hits = [
        {
            "id": r["location"].get("s3Location", {}).get("uri", "?"),
            "text": r["content"]["text"],
            "score": round(r.get("score", 0.0), 3),
        }
        for r in resp.get("retrievalResults", [])
    ]
    return {"tenant": tenant, "hits": hits}
