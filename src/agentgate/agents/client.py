"""Platform client used by every agent: talks to the two gateways.

Two transports behind one interface:
  HttpPlatform    - real HTTP to the running gateway services (docker compose / ECS)
  InProcessPlatform - calls the service functions directly (tests, evals, `agentgate demo --inproc`)

Agents never import boto3 and never see a model id. That is the whole point.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from agentgate.common.config import get_settings


class PlatformError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


class Platform(Protocol):
    tenant: str

    def chat(
        self,
        capability: str,
        messages: list[dict[str, str]],
        *,
        run_id: str,
        feature: str = "default",
        output_schema: dict[str, Any] | None = None,
        context_hash: str | None = None,
    ) -> dict[str, Any]: ...

    def invoke(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        run_id: str,
        feature: str = "default",
        approval_token: str | None = None,
        rationale: str = "",
        expected_impact: str = "",
        evidence: list[str] | None = None,
    ) -> dict[str, Any]: ...

    def decide_approval(self, approval_id: str, approver: str, approve: bool) -> dict[str, Any]: ...

    def run_cost(self, run_id: str) -> dict[str, Any]: ...


class HttpPlatform:
    def __init__(
        self, api_key: str, tenant: str, gateway_url: str | None = None, toolgateway_url: str | None = None
    ):
        s = get_settings()
        self.tenant = tenant
        self._h = {"X-API-Key": api_key}
        self._gw = (gateway_url or s.gateway_url).rstrip("/")
        self._tg = (toolgateway_url or s.toolgateway_url).rstrip("/")
        self._http = httpx.Client(timeout=90)

    def _check(self, r: httpx.Response) -> dict[str, Any]:
        if r.status_code >= 400:
            body = (
                r.json()
                if r.headers.get("content-type", "").startswith("application/json")
                else {"error": "http", "detail": r.text}
            )
            raise PlatformError(r.status_code, body.get("error", "error"), body.get("detail", ""))
        return r.json()

    def chat(self, capability, messages, *, run_id, feature="default", output_schema=None, context_hash=None):
        return self._check(
            self._http.post(
                f"{self._gw}/v1/chat",
                headers=self._h,
                json={
                    "capability": capability,
                    "messages": messages,
                    "run_id": run_id,
                    "feature": feature,
                    "output_schema": output_schema,
                    "context_hash": context_hash,
                },
            )
        )

    def invoke(
        self,
        tool,
        arguments,
        *,
        run_id,
        feature="default",
        approval_token=None,
        rationale="",
        expected_impact="",
        evidence=None,
    ):
        return self._check(
            self._http.post(
                f"{self._tg}/v1/tools/{tool}/invoke",
                headers=self._h,
                json={
                    "arguments": arguments,
                    "run_id": run_id,
                    "feature": feature,
                    "approval_token": approval_token,
                    "rationale": rationale,
                    "expected_impact": expected_impact,
                    "evidence": evidence or [],
                },
            )
        )

    def decide_approval(self, approval_id, approver, approve):
        return self._check(
            self._http.post(
                f"{self._tg}/v1/approvals/{approval_id}/decide",
                headers=self._h,
                json={"approver": approver, "approve": approve},
            )
        )

    def run_cost(self, run_id):
        return self._check(self._http.get(f"{self._gw}/v1/runs/{run_id}", headers=self._h))


class InProcessPlatform:
    def __init__(self, tenant: str, owner: str = "platform"):
        from agentgate.gateway.identity import Principal

        self.tenant = tenant
        self._p = Principal(tenant=tenant, owner=owner, subject="inproc", via="api_key")

    def chat(self, capability, messages, *, run_id, feature="default", output_schema=None, context_hash=None):
        from agentgate.gateway.models import ChatRequest
        from agentgate.gateway.service import GatewayError, chat

        try:
            return chat(
                self._p,
                ChatRequest(
                    capability=capability,
                    messages=messages,
                    run_id=run_id,
                    feature=feature,
                    output_schema=output_schema,
                    context_hash=context_hash,
                ),
            ).model_dump()
        except GatewayError as e:
            raise PlatformError(e.status, e.code, str(e)) from e

    def invoke(
        self,
        tool,
        arguments,
        *,
        run_id,
        feature="default",
        approval_token=None,
        rationale="",
        expected_impact="",
        evidence=None,
    ):
        from agentgate.toolgateway.executor import ToolGatewayError, invoke

        try:
            return invoke(
                self.tenant,
                tool,
                arguments,
                run_id=run_id,
                feature=feature,
                approval_token=approval_token,
                rationale=rationale,
                expected_impact=expected_impact,
                evidence=evidence,
            ).to_dict()
        except ToolGatewayError as e:
            raise PlatformError(e.status, e.code, str(e)) from e

    def decide_approval(self, approval_id, approver, approve):
        from agentgate.toolgateway import approvals

        try:
            return approvals.decide(approval_id, approver, approve)
        except approvals.ApprovalError as e:
            raise PlatformError(409, "approval_error", str(e)) from e

    def run_cost(self, run_id):
        from agentgate.common.ledger import get_ledger

        rows = get_ledger().rows_for_run(run_id)
        return {
            "run_id": run_id,
            "rows": rows,
            "total_cost_usd": round(sum(float(r["cost_usd"]) for r in rows), 6),
        }
