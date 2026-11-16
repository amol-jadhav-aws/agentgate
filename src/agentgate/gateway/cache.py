"""Semantic cache.

Key = tenant | capability | prompt_version | model | context_hash. Inside that bucket,
the prompt is embedded and compared by cosine similarity; a hit needs >= threshold
(0.95 by default). The bucket key guarantees that a cache entry can never cross
tenants, never survive a prompt or model change, and never outlive the retrieved
context it was answered from. The similarity threshold only decides how close two
phrasings of the same question must be.

Local mode: an in-memory list per bucket with a deterministic hashing embedder.
AWS mode: Redis (a hash per entry under the bucket key) with Titan embeddings via
the Bedrock client passed in.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

from agentgate.common.config import get_settings


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """Deterministic bag-of-words hashing embedder for local mode and tests.

    Similar sentences share tokens, so they land close; it is not a semantic model and
    is never used in aws mode."""

    def __init__(self, dims: int = 256):
        self.dims = dims

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dims
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vec[h % self.dims] += 1.0 if (h >> 8) % 2 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def bucket_key(
    tenant: str,
    capability: str,
    prompt_version: str,
    model_id: str,
    context_hash: str | None,
    messages: list[dict[str, Any]] | None = None,
) -> str:
    """Everything that must match EXACTLY goes in the key: tenant, capability, prompt version, model,
    retrieved-context hash, and a hash of every message except the final user turn (system prompt,
    few-shot examples, earlier turns). Only the final user turn is matched by similarity."""
    fixed = "-"
    if messages:
        head = "\n".join(f"{m['role']}: {m['content']}" for m in messages[:-1])
        fixed = hashlib.sha256(head.encode()).hexdigest()[:16]
    return "|".join([tenant, capability, prompt_version, model_id, context_hash or "-", fixed])


def prompt_text(messages: list[dict[str, Any]]) -> str:
    """The text that is embedded: the final user turn only. Embedding the whole prompt lets a long
    shared system prompt or context dominate the vector, so two different questions over the same
    document look 'similar' and the cache returns the wrong answer (found by the eval gate, see ADR-0003)."""
    if not messages:
        return ""
    return messages[-1]["content"]


@dataclass
class CacheHit:
    content: str
    similarity: float
    model_id: str
    usage: dict[str, int]


class SemanticCache:
    def __init__(self, embedder: Embedder, threshold: float | None = None, ttl: int | None = None):
        s = get_settings()
        self.embedder = embedder
        self.threshold = threshold if threshold is not None else s.cache_similarity_threshold
        self.ttl = ttl or s.cache_ttl_seconds
        self._mem: dict[str, list[dict[str, Any]]] = {}
        self._redis = None
        if not s.is_local:
            import redis

            self._redis = redis.Redis.from_url(s.redis_url, decode_responses=True)

    # -- storage primitives -----------------------------------------------------
    def _entries(self, bucket: str) -> list[dict[str, Any]]:
        if self._redis is None:
            now = time.time()
            live = [e for e in self._mem.get(bucket, []) if e["exp"] > now]
            self._mem[bucket] = live
            return live
        raw = self._redis.hgetall(f"agentgate:cache:{bucket}")
        return [json.loads(v) for v in raw.values()]

    def _store(self, bucket: str, entry: dict[str, Any]) -> None:
        if self._redis is None:
            self._mem.setdefault(bucket, []).append(entry)
            return
        key = f"agentgate:cache:{bucket}"
        self._redis.hset(key, entry["id"], json.dumps(entry))
        self._redis.expire(key, self.ttl)

    # -- API --------------------------------------------------------------------
    def lookup(self, bucket: str, messages: list[dict[str, Any]]) -> CacheHit | None:
        q = self.embedder.embed(prompt_text(messages))
        best, best_sim = None, -1.0
        for e in self._entries(bucket):
            sim = cosine(q, e["vec"])
            if sim > best_sim:
                best, best_sim = e, sim
        if best is not None and best_sim >= self.threshold:
            return CacheHit(
                content=best["content"],
                similarity=round(best_sim, 4),
                model_id=best["model_id"],
                usage=best["usage"],
            )
        return None

    def store(
        self, bucket: str, messages: list[dict[str, Any]], content: str, model_id: str, usage: dict[str, int]
    ) -> None:
        text = prompt_text(messages)
        self._store(
            bucket,
            {
                "id": hashlib.sha256(text.encode()).hexdigest()[:16],
                "vec": self.embedder.embed(text),
                "content": content,
                "model_id": model_id,
                "usage": usage,
                "exp": time.time() + self.ttl,
            },
        )

    def clear(self) -> None:
        self._mem.clear()
        if self._redis is not None:
            for k in self._redis.scan_iter("agentgate:cache:*"):
                self._redis.delete(k)
