"""Key-value store used for budgets, the cost ledger, audit rows and approvals.

One DynamoDB table in aws mode (single-table design: pk / sk), a dict in local mode.
The interface is deliberately tiny so the gateway code never knows which one it has.

Budget enforcement relies on `add_number` being atomic: in DynamoDB that is an
UpdateExpression with ADD plus a ConditionExpression, so two concurrent requests
cannot both pass a budget check that only one of them should.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

from agentgate.common.config import get_settings


class BudgetExceeded(Exception):
    def __init__(self, key: str, limit: float, attempted: float):
        super().__init__(f"budget exceeded for {key}: limit={limit} attempted={attempted}")
        self.key, self.limit, self.attempted = key, limit, attempted


class Store(ABC):
    @abstractmethod
    def get(self, pk: str, sk: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def put(self, pk: str, sk: str, item: dict[str, Any], ttl_seconds: int | None = None) -> None: ...

    @abstractmethod
    def query(self, pk: str, sk_prefix: str = "") -> list[dict[str, Any]]: ...

    @abstractmethod
    def add_number(self, pk: str, sk: str, field: str, amount: float, limit: float | None = None) -> float:
        """Atomically add `amount` to `field`. If `limit` is set and the result would exceed it,
        raise BudgetExceeded and leave the value unchanged."""

    @abstractmethod
    def delete(self, pk: str, sk: str) -> None: ...


class MemoryStore(Store):
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _expired(self, item: dict[str, Any]) -> bool:
        ttl = item.get("_ttl")
        return ttl is not None and ttl < time.time()

    def get(self, pk, sk):
        with self._lock:
            item = self._data.get((pk, sk))
            if item is None or self._expired(item):
                return None
            return {k: v for k, v in item.items() if k != "_ttl"}

    def put(self, pk, sk, item, ttl_seconds=None):
        with self._lock:
            row = dict(item, pk=pk, sk=sk)
            if ttl_seconds:
                row["_ttl"] = time.time() + ttl_seconds
            self._data[(pk, sk)] = row

    def query(self, pk, sk_prefix=""):
        with self._lock:
            return [
                {k: v for k, v in item.items() if k != "_ttl"}
                for (p, s), item in sorted(self._data.items())
                if p == pk and s.startswith(sk_prefix) and not self._expired(item)
            ]

    def add_number(self, pk, sk, field, amount, limit=None):
        with self._lock:
            row = self._data.setdefault((pk, sk), {"pk": pk, "sk": sk})
            current = float(row.get(field, 0.0))
            new = current + amount
            if limit is not None and new > limit:
                raise BudgetExceeded(f"{pk}/{sk}", limit, new)
            row[field] = new
            return new

    def delete(self, pk, sk):
        with self._lock:
            self._data.pop((pk, sk), None)


class DynamoStore(Store):
    """Single-table DynamoDB store. Table has pk (S) and sk (S), TTL attribute `ttl`."""

    def __init__(self, table_name: str, region: str):
        import boto3

        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    @staticmethod
    def _to_py(item: dict[str, Any]) -> dict[str, Any]:
        return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in item.items()}

    @staticmethod
    def _to_ddb(item: dict[str, Any]) -> dict[str, Any]:
        return {k: (Decimal(str(v)) if isinstance(v, float) else v) for k, v in item.items()}

    def get(self, pk, sk):
        resp = self._table.get_item(Key={"pk": pk, "sk": sk})
        item = resp.get("Item")
        return self._to_py(item) if item else None

    def put(self, pk, sk, item, ttl_seconds=None):
        row = dict(item, pk=pk, sk=sk)
        if ttl_seconds:
            row["ttl"] = int(time.time()) + ttl_seconds
        self._table.put_item(Item=self._to_ddb(row))

    def query(self, pk, sk_prefix=""):
        from boto3.dynamodb.conditions import Key

        cond = Key("pk").eq(pk)
        if sk_prefix:
            cond = cond & Key("sk").begins_with(sk_prefix)
        resp = self._table.query(KeyConditionExpression=cond)
        return [self._to_py(i) for i in resp.get("Items", [])]

    def add_number(self, pk, sk, field, amount, limit=None):
        from botocore.exceptions import ClientError

        kwargs: dict[str, Any] = {
            "Key": {"pk": pk, "sk": sk},
            "UpdateExpression": "ADD #f :a",
            "ExpressionAttributeNames": {"#f": field},
            "ExpressionAttributeValues": {":a": Decimal(str(amount))},
            "ReturnValues": "UPDATED_NEW",
        }
        if limit is not None:
            kwargs["ConditionExpression"] = "attribute_not_exists(#f) OR #f <= :room"
            kwargs["ExpressionAttributeValues"][":room"] = Decimal(str(limit - amount))
        try:
            resp = self._table.update_item(**kwargs)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise BudgetExceeded(f"{pk}/{sk}", limit or 0.0, amount) from e
            raise
        return float(resp["Attributes"][field])

    def delete(self, pk, sk):
        self._table.delete_item(Key={"pk": pk, "sk": sk})


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        s = get_settings()
        _store = MemoryStore() if s.is_local else DynamoStore(s.dynamodb_table, s.aws_region)
    return _store


def reset_store_for_tests() -> None:
    global _store
    _store = MemoryStore()
