"""Settings for every AgentGate service.

Two modes:
  local  - no AWS account needed. In-memory store, in-memory cache, a deterministic
           fake Bedrock. This is what `make up`, the tests and the CI eval gate use.
  aws    - DynamoDB, ElastiCache Redis, real Bedrock Converse, STS AssumeRole per tool.

Everything is read from environment variables prefixed AGENTGATE_ (see .env.example).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTGATE_", env_file=".env", extra="ignore")

    mode: Literal["local", "aws"] = "local"
    env_name: str = "dev"
    aws_region: str = "ap-south-1"

    # Config files
    config_dir: Path = REPO_ROOT / "config"
    policy_dir: Path = REPO_ROOT / "policies"

    # Inference gateway
    gateway_url: str = "http://localhost:8080"
    gateway_api_keys: str = (
        "dev-key-ops:ops-agent:platform,dev-key-kb:knowledge-agent:platform,dev-key-cli:cli:platform"
    )
    jwt_secret: str = "change-me-in-aws-mode"
    cache_similarity_threshold: float = 0.95
    cache_ttl_seconds: int = 3600
    guardrail_id: str | None = None
    guardrail_version: str = "DRAFT"
    embedding_model_id: str = "amazon.titan-embed-text-v2:0"

    # Tool gateway
    toolgateway_url: str = "http://localhost:8081"
    approval_ttl_seconds: int = 600
    approval_signing_key: str = "approval-dev-signing-key"
    slack_webhook_url: str | None = None

    # Storage
    dynamodb_table: str = "agentgate-dev"
    redis_url: str = "redis://localhost:6379/0"
    checkpoint_db: str = str(REPO_ROOT / ".state" / "checkpoints.sqlite")

    # Telemetry
    otel_exporter_otlp_endpoint: str | None = Field(default=None, alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    service_name: str = "agentgate"

    # Agent limits
    max_agent_steps: int = 8
    default_run_budget_usd: float = 0.50

    @property
    def is_local(self) -> bool:
        return self.mode == "local"


@lru_cache
def get_settings() -> Settings:
    return Settings()
