"""MCP (Model Context Protocol) face of the tool gateway.

Exposes the tenant's tool catalogue as MCP tools over stdio or streamable HTTP, so any
MCP-capable agent framework (Strands, LangGraph MCP adapters, Claude Desktop, n8n's MCP
node) can use AgentGate tools and inherits every control: schema check, budget, policy,
approval, per-tool IAM role, redaction, audit.

Run:  python -m agentgate.toolgateway.mcp_server --tenant ops-agent
Requires the optional `mcp` package (pip install "mcp>=1.0"); the REST surface works without it.
"""

from __future__ import annotations

import argparse
import json
import uuid
from typing import Any

from agentgate.toolgateway.executor import ToolGatewayError, invoke
from agentgate.toolgateway.manifest import get_manifest


def build_server(tenant: str):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:  # pragma: no cover
        raise SystemExit("pip install 'mcp>=1.0' to run the MCP server") from e

    mcp = FastMCP(f"agentgate-{tenant}")
    manifest = get_manifest()

    for spec in manifest.granted(tenant):

        def _make(tool_name: str, description: str, risk_tier: int, schema: dict[str, Any]):
            def _tool(
                arguments: dict[str, Any], run_id: str | None = None, approval_token: str | None = None
            ) -> str:
                try:
                    res = invoke(
                        tenant,
                        tool_name,
                        arguments,
                        run_id=run_id or uuid.uuid4().hex,
                        approval_token=approval_token,
                    )
                    return json.dumps(res.to_dict())
                except ToolGatewayError as e:
                    return json.dumps({"error": e.code, "detail": str(e)})

            _tool.__name__ = tool_name
            _tool.__doc__ = (
                f"{description} (risk tier {risk_tier}). arguments must match: {json.dumps(schema)}"
            )
            return _tool

        mcp.tool(name=spec.name)(_make(spec.name, spec.description, spec.risk_tier, spec.input_schema))
    return mcp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http"])
    a = ap.parse_args()
    build_server(a.tenant).run(transport=a.transport)


if __name__ == "__main__":
    main()
