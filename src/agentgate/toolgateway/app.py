"""FastAPI surface of the tool gateway.

    GET  /v1/tools                      the caller's tool catalogue (schemas, tiers; no ARNs)
    POST /v1/tools/{name}/invoke        validate -> budget -> policy -> execute
    GET  /v1/approvals/{id}             state of one approval
    POST /v1/approvals/{id}/decide      approve or reject (Slack action / CLI)
    GET  /v1/audit                      this tenant's audit rows
    GET  /healthz

It also speaks MCP: `mcp_server.py` wraps the same executor for MCP clients.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agentgate import __version__
from agentgate.common.telemetry import configure
from agentgate.gateway.identity import Principal, principal_from_headers
from agentgate.gateway.models import ErrorBody
from agentgate.toolgateway import approvals
from agentgate.toolgateway.executor import ToolGatewayError, invoke
from agentgate.toolgateway.manifest import get_manifest
from agentgate.toolgateway.policy import audit_rows

configure("agentgate-toolgateway")
app = FastAPI(title="AgentGate tool gateway", version=__version__)


class InvokeBody(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    run_id: str
    feature: str = "default"
    approval_token: str | None = None
    rationale: str = ""
    expected_impact: str = ""
    evidence: list[str] = Field(default_factory=list)


class DecideBody(BaseModel):
    approver: str
    approve: bool
    comment: str | None = None


@app.exception_handler(ToolGatewayError)
async def _err(request: Request, exc: ToolGatewayError):
    return JSONResponse(
        status_code=exc.status, content=ErrorBody(error=exc.code, detail=str(exc)).model_dump()
    )


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__, "tools": len(get_manifest().tools)}


@app.get("/v1/tools")
def tools(principal: Principal = Depends(principal_from_headers)):
    return {"tenant": principal.tenant, "tools": get_manifest().catalogue(principal.tenant)}


@app.post("/v1/tools/{name}/invoke")
def invoke_tool(name: str, body: InvokeBody, principal: Principal = Depends(principal_from_headers)):
    result = invoke(
        principal.tenant,
        name,
        body.arguments,
        run_id=body.run_id,
        feature=body.feature,
        approval_token=body.approval_token,
        rationale=body.rationale,
        expected_impact=body.expected_impact,
        evidence=body.evidence,
    )
    status = 202 if result.decision == "approve" else 200
    return JSONResponse(status_code=status, content=result.to_dict())


@app.get("/v1/approvals/{approval_id}")
def approval_state(approval_id: str, _: Principal = Depends(principal_from_headers)):
    row = approvals.get(approval_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "not_found", "detail": approval_id})
    return {k: v for k, v in row.items() if k not in ("pk", "sk")}


@app.post("/v1/approvals/{approval_id}/decide")
def decide(approval_id: str, body: DecideBody, principal: Principal = Depends(principal_from_headers)):
    try:
        return approvals.decide(approval_id, body.approver, body.approve, body.comment)
    except approvals.ApprovalError as e:
        return JSONResponse(status_code=409, content={"error": "approval_error", "detail": str(e)})


@app.get("/v1/approvals")
def list_pending(_: Principal = Depends(principal_from_headers)):
    return {"pending": [{k: v for k, v in r.items() if k not in ("pk", "sk")} for r in approvals.pending()]}


@app.get("/v1/audit")
def audit(principal: Principal = Depends(principal_from_headers)):
    return {"tenant": principal.tenant, "rows": audit_rows(principal.tenant)}
