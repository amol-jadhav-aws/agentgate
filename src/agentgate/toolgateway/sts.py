"""One IAM role per tool, assumed per call, with the run id in the session name.

AssumeRole(RoleSessionName="agentgate-<run_id>") means every CloudTrail event the tool
causes carries the run id in `userIdentity.sessionContext`. Trace, ledger and CloudTrail
join on one string. Secrets are fetched from Secrets Manager at call time with that
role, never baked into a task definition.
"""

from __future__ import annotations

import re
from typing import Any

from agentgate.common.config import get_settings


def session_name(run_id: str) -> str:
    # RoleSessionName: 2-64 chars, [\w+=,.@-]
    return ("agentgate-" + re.sub(r"[^\w+=,.@-]", "", run_id))[:64]


def resolve_role_arn(template: str) -> str:
    s = get_settings()
    account = _account_id() if not s.is_local else "000000000000"
    return template.replace("${account_id}", account).replace("${env}", s.env_name)


_account_cache: str | None = None


def _account_id() -> str:
    global _account_cache
    if _account_cache is None:
        import boto3

        _account_cache = boto3.client("sts").get_caller_identity()["Account"]
    return _account_cache


def session_for(role_arn_template: str, run_id: str, duration: int = 900) -> Any:
    """Return a boto3 Session whose credentials belong to the tool's role. None in local mode."""
    if get_settings().is_local:
        return None
    import boto3

    arn = resolve_role_arn(role_arn_template)
    creds = boto3.client("sts").assume_role(
        RoleArn=arn,
        RoleSessionName=session_name(run_id),
        DurationSeconds=duration,
        Tags=[{"Key": "agentgate:run_id", "Value": run_id[:256]}],
    )["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=get_settings().aws_region,
    )


def secret(session: Any, secret_id: str) -> str:
    if session is None:
        return f"local-secret-for-{secret_id}"
    return session.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"]
