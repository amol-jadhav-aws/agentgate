import threading

import pytest

from agentgate.common.store import BudgetExceeded, MemoryStore


def test_add_number_is_atomic_under_contention():
    s = MemoryStore()
    ok, refused = [], []

    def worker():
        try:
            s.add_number("b", "m", "spent", 1.0, limit=50)
            ok.append(1)
        except BudgetExceeded:
            refused.append(1)

    threads = [threading.Thread(target=worker) for _ in range(200)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == 50 and len(refused) == 150
    assert s.get("b", "m")["spent"] == 50.0


def test_ttl():
    s = MemoryStore()
    s.put("p", "k", {"v": 1}, ttl_seconds=-1)
    assert s.get("p", "k") is None


def test_budget_exceeded_message():
    with pytest.raises(BudgetExceeded):
        MemoryStore().add_number("a", "b", "x", 2.0, limit=1.0)
