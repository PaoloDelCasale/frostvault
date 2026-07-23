"""S3 bucket readiness checks for archive operations.

Framework-agnostic: callers inject a boto3 S3 client so tests can stub AWS
without patching internals.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from botocore.exceptions import ClientError

from ..config import is_placeholder

CheckStatus = Literal["pass", "fail", "warn"]


@dataclass(frozen=True)
class PreflightCheck:
    code: str
    status: CheckStatus
    message: str
    remediation: str | None = None


@dataclass(frozen=True)
class PreflightResult:
    bucket: str
    region: str
    ok: bool
    checks: tuple[PreflightCheck, ...]


def preflight_failure_message(result: PreflightResult) -> str:
    """Return an actionable summary of failed preflight checks."""
    parts: list[str] = []
    for check in result.checks:
        if check.status != "fail":
            continue
        if check.remediation:
            parts.append(f"{check.message} {check.remediation}")
        else:
            parts.append(check.message)
    return "; ".join(parts)


def _normalize_bucket_region(location: str | None) -> str:
    if not location:
        return "us-east-1"
    return location


def _client_error_code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "ClientError")


def _permission_failure(action: str, exc: ClientError) -> PreflightCheck:
    code = _client_error_code(exc)
    return PreflightCheck(
        code="bucket.permissions",
        status="fail",
        message=f"Missing permission for {action} ({code})",
        remediation=f"Grant the archive IAM identity {action} on the bucket",
    )


def check_bucket_readiness(
    bucket: str,
    *,
    region: str,
    client: Any,
) -> PreflightResult:
    checks: list[PreflightCheck] = []

    normalized = bucket.strip()
    if not normalized or is_placeholder(
        normalized,
        "BUCKET-NAME",
        "REPLACE-WITH-BUCKET-NAME",
    ):
        checks.append(
            PreflightCheck(
                code="bucket.configured",
                status="fail",
                message="The S3 bucket name is not configured",
                remediation="Set S3_BUCKET / VAULT_S3_BUCKET to your dedicated archive bucket",
            )
        )
        return PreflightResult(
            bucket=normalized,
            region=region,
            ok=False,
            checks=tuple(checks),
        )

    try:
        client.head_bucket(Bucket=normalized)
    except ClientError as exc:
        checks.append(_permission_failure("s3:HeadBucket", exc))
        return PreflightResult(
            bucket=normalized,
            region=region,
            ok=False,
            checks=tuple(checks),
        )

    try:
        location = client.get_bucket_location(Bucket=normalized).get("LocationConstraint")
    except ClientError as exc:
        checks.append(_permission_failure("s3:GetBucketLocation", exc))
        return PreflightResult(
            bucket=normalized,
            region=region,
            ok=False,
            checks=tuple(checks),
        )

    actual_region = _normalize_bucket_region(location)
    if actual_region != region:
        checks.append(
            PreflightCheck(
                code="bucket.region",
                status="fail",
                message=(
                    f"Bucket region is {actual_region}, but AWS_DEFAULT_REGION is {region}"
                ),
                remediation="Point AWS_DEFAULT_REGION at the bucket region or recreate the bucket",
            )
        )

    try:
        versioning_status = client.get_bucket_versioning(Bucket=normalized).get("Status")
    except ClientError as exc:
        checks.append(_permission_failure("s3:GetBucketVersioning", exc))
        return PreflightResult(
            bucket=normalized,
            region=region,
            ok=False,
            checks=tuple(checks),
        )

    if versioning_status == "Enabled":
        checks.append(
            PreflightCheck(
                code="bucket.versioning",
                status="pass",
                message="Bucket versioning is enabled",
            )
        )
    else:
        state = versioning_status or "disabled"
        checks.append(
            PreflightCheck(
                code="bucket.versioning",
                status="fail",
                message=f"Bucket versioning is {state.lower()}",
                remediation="Enable S3 versioning on the archive bucket before upload or cleanup",
            )
        )

    ok = all(check.status != "fail" for check in checks)
    return PreflightResult(
        bucket=normalized,
        region=region,
        ok=ok,
        checks=tuple(checks),
    )
