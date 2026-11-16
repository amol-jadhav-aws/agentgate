# One image, three services; the command picks which (gateway | toolgateway | agents).
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN adduser --disabled-password --gecos "" --uid 10001 agentgate
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && pip install --no-cache-dir "mcp>=1.0" || true
COPY config ./config
COPY policies ./policies
COPY evals ./evals
USER agentgate
EXPOSE 8080 8081
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import httpx,os;httpx.get(f'http://localhost:{os.environ.get(\"PORT\",\"8080\")}/healthz').raise_for_status()"
# default: inference gateway
CMD ["uvicorn", "agentgate.gateway.app:app", "--host", "0.0.0.0", "--port", "8080"]
