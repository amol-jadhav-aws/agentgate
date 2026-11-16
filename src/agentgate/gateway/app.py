"""FastAPI surface of the inference gateway.

POST /v1/chat            the only way to a model
GET  /v1/capabilities    what callers may ask for
GET  /v1/budget          this tenant's spend and limit
GET  /v1/runs/{run_id}   ledger rows for one run (cost timeline)
GET  /healthz
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from agentgate import __version__
from agentgate.common.ledger import get_ledger
from agentgate.common.telemetry import configure
from agentgate.gateway.identity import Principal, principal_from_headers
from agentgate.gateway.models import ChatRequest, ChatResponse, ErrorBody
from agentgate.gateway.routing import get_router
from agentgate.gateway.service import GatewayError, chat

configure("agentgate-gateway")
app = FastAPI(title="AgentGate inference gateway", version=__version__)


@app.exception_handler(GatewayError)
async def _gateway_error(request: Request, exc: GatewayError):
    return JSONResponse(
        status_code=exc.status, content=ErrorBody(error=exc.code, detail=str(exc)).model_dump()
    )


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__}


@app.get("/v1/capabilities")
def capabilities(_: Principal = Depends(principal_from_headers)):
    r = get_router()
    return {
        c: {"model": r.route(c).primary.name, "fallback": r.route(c).fallback.name, "cache": r.route(c).cache}
        for c in r.capabilities
    }


@app.post(
    "/v1/chat",
    response_model=ChatResponse,
    responses={429: {"model": ErrorBody}, 400: {"model": ErrorBody}, 502: {"model": ErrorBody}},
)
def chat_endpoint(req: ChatRequest, principal: Principal = Depends(principal_from_headers)):
    return chat(principal, req)


@app.get("/v1/budget")
def budget(principal: Principal = Depends(principal_from_headers)):
    return get_ledger().summary(principal.tenant)


@app.get("/v1/runs/{run_id}")
def run_rows(run_id: str, principal: Principal = Depends(principal_from_headers)):
    rows = [r for r in get_ledger().rows_for_run(run_id) if r["tenant"] == principal.tenant]
    return {
        "run_id": run_id,
        "rows": rows,
        "total_cost_usd": round(sum(float(r["cost_usd"]) for r in rows), 6),
    }
