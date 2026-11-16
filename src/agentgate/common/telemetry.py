"""OpenTelemetry setup: one trace per agent run, one span per model call and tool call.

The run id is carried in the `agentgate.run_id` attribute on every span and in the
STS session name of every AssumeRole, so a CloudTrail event, a Jaeger trace and a
ledger row for the same action all share one id.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from agentgate.common.config import get_settings

_configured = False
log = logging.getLogger("agentgate")


def configure(service_name: str | None = None) -> None:
    global _configured
    if _configured:
        return
    s = get_settings()
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": service_name or s.service_name, "deployment.environment": s.env_name}
        )
    )
    if s.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=s.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces")
            )
        )
    trace.set_tracer_provider(provider)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _configured = True


def tracer(name: str = "agentgate"):
    configure()
    return trace.get_tracer(name)


@contextmanager
def span(name: str, **attrs: Any):
    with tracer().start_as_current_span(name) as sp:
        for k, v in attrs.items():
            if v is not None:
                sp.set_attribute(f"agentgate.{k}", v if isinstance(v, (str, int, float, bool)) else str(v))
        yield sp


def current_trace_id() -> str:
    ctx = trace.get_current_span().get_span_context()
    return f"{ctx.trace_id:032x}" if ctx and ctx.trace_id else ""
