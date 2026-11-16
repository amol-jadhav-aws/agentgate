"""Shared fixtures: every test gets a fresh in-memory store, cache and fake AWS world."""

import pytest
from fastapi.testclient import TestClient

from agentgate.common.ledger import get_ledger
from agentgate.common.store import reset_store_for_tests
from agentgate.gateway.bedrock import FakeBedrockClient, set_model_client
from agentgate.gateway.service import reset_cache_for_tests
from agentgate.toolgateway.tools import fakeaws


@pytest.fixture(autouse=True)
def fresh_platform(monkeypatch):
    monkeypatch.setenv("AGENTGATE_MODE", "local")
    reset_store_for_tests()
    reset_cache_for_tests()
    set_model_client(FakeBedrockClient())
    fakeaws.reset()
    get_ledger()  # rebind to the fresh store
    yield


@pytest.fixture
def gw():
    from agentgate.gateway.app import app

    return TestClient(app)


@pytest.fixture
def tg():
    from agentgate.toolgateway.app import app

    return TestClient(app)


OPS = {"X-API-Key": "dev-key-ops"}
KB = {"X-API-Key": "dev-key-kb"}
CLI = {"X-API-Key": "dev-key-cli"}
