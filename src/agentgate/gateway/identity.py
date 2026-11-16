"""Caller identity.

Local mode and service-to-service: API keys from AGENTGATE_GATEWAY_API_KEYS as
`key:tenant:owner,...`. AWS mode also accepts a Cognito/own JWT (HS256 here; swap the
verifier for the Cognito JWKS in production). Every request leaves with a Principal.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException
from jose import JWTError, jwt

from agentgate.common.config import get_settings


@dataclass(frozen=True)
class Principal:
    tenant: str
    owner: str
    subject: str
    via: str  # "api_key" | "jwt"


def _api_keys() -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for entry in get_settings().gateway_api_keys.split(","):
        entry = entry.strip()
        if not entry:
            continue
        key, tenant, owner = entry.split(":")
        out[key] = (tenant, owner)
    return out


def principal_from_headers(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> Principal:
    if x_api_key:
        keys = _api_keys()
        if x_api_key in keys:
            tenant, owner = keys[x_api_key]
            return Principal(tenant=tenant, owner=owner, subject=f"key:{x_api_key[:6]}", via="api_key")
        raise HTTPException(status_code=401, detail="unknown api key")
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1]
        try:
            claims = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
        except JWTError as e:
            raise HTTPException(status_code=401, detail=f"invalid token: {e}") from e
        tenant = claims.get("tenant") or claims.get("custom:tenant")
        if not tenant:
            raise HTTPException(status_code=403, detail="token has no tenant claim")
        return Principal(
            tenant=tenant, owner=claims.get("owner", "unknown"), subject=claims.get("sub", "?"), via="jwt"
        )
    raise HTTPException(status_code=401, detail="missing credentials: X-API-Key or Authorization: Bearer")


def mint_jwt(tenant: str, subject: str, owner: str = "platform", ttl_seconds: int = 3600) -> str:
    """Dev helper for issuing a tenant token (tests, CLI)."""
    import time

    return jwt.encode(
        {"tenant": tenant, "sub": subject, "owner": owner, "exp": int(time.time()) + ttl_seconds},
        get_settings().jwt_secret,
        algorithm="HS256",
    )
