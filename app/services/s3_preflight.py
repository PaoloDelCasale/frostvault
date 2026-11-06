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


def _http_status(exc: ClientError) -> int | None:
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status if isinstance(status, int) else None


# Provider error codes proving the request signature or token was rejected,
# i.e. the credentials themselves are wrong or expired, not under-privileged.
CREDENTIAL_ERROR_CODES = frozenset({
    "SignatureDoesNotMatch", "InvalidAccessKeyId", "InvalidClientTokenId",
    "UnrecognizedClientException", "ExpiredToken", "InvalidToken", "TokenRefreshRequired",
})
# A bodyless HEAD 403 reaches botocore as the bare status code.
_FORBIDDEN_CODES = frozenset({"403", "Forbidden", "AccessDenied"})


def _credential_failure(code: str) -> PreflightCheck:
    # Only the allowlisted code is echoed, never the provider message.
    return PreflightCheck(
        code="bucket.credentials",
        status="fail",
        message=f"S3 rejected the AWS credentials ({code})",
        remediation=(
            "Check AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY (wrong or expired "
            "credentials), then retry; this is not a missing IAM permission"
        ),
    )


def _permission_failure(action: str, exc: ClientError) -> PreflightCheck:
    code = _client_error_code(exc)
    if code in CREDENTIAL_ERROR_CODES:
        return _credential_failure(code)
    return PreflightCheck(
        code="bucket.permissions",
        status="fail",
        message=f"Missing permission for {action} ({code})",
        remediation=f"Grant the archive IAM identity {action} on the bucket",
    )


def _head_bucket_failure(client: Any, bucket: str, exc: ClientError) -> PreflightCheck:
    """Explain a failed HeadBucket.

    A HEAD response has no body, so a 403 cannot tell wrong credentials from a
    missing permission. HeadBucket is authorized by s3:ListBucket (there is no
    s3:HeadBucket action). One read-only GetBucketLocation, already part of the
    documented minimum policy and of this preflight, returns an error code.
    """
    code = _client_error_code(exc)
    if code in CREDENTIAL_ERROR_CODES:
        return _credential_failure(code)
    if code not in _FORBIDDEN_CODES and _http_status(exc) != 403:
        return _permission_failure("s3:HeadBucket", exc)
    try:
        client.get_bucket_location(Bucket=bucket)
    except ClientError as probe_exc:
        probe_code = _client_error_code(probe_exc)
        if probe_code in CREDENTIAL_ERROR_CODES:
            return _credential_failure(probe_code)
        if probe_code == "AccessDenied":
            return PreflightCheck(
                code="bucket.permissions",
                status="fail",
                message="S3 denied access to the bucket (AccessDenied)",
                remediation=(
                    "Grant the archive IAM identity s3:ListBucket (required by "
                    "HeadBucket) and s3:GetBucketLocation on the bucket, and check "
                    "the bucket policy and ownership"
                ),
            )
    except Exception:
        pass
    else:
        # The credentials were accepted and the bucket location is readable, so
        # only the list permission that HeadBucket needs is missing.
        return PreflightCheck(
            code="bucket.permissions",
            status="fail",
            message="Missing permission for s3:ListBucket, required by HeadBucket (403)",
            remediation="Grant the archive IAM identity s3:ListBucket on the bucket",
        )
    return PreflightCheck(
        code="bucket.access",
        status="fail",
        message="S3 refused access to the bucket (403) without saying why",
        remediation=(
            "Either the AWS credentials are wrong or expired (check AWS_ACCESS_KEY_ID "
            "and AWS_SECRET_ACCESS_KEY) or the archive IAM identity lacks s3:ListBucket "
            "on the bucket"
        ),
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
        checks.append(_head_bucket_failure(client, normalized, exc))
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

    # Bucket-level BPA is mandatory, including on S3-compatible providers.
    # Account-level policy, AccessDenied, unsupported APIs and absent state
    # cannot prove these four controls. Do not echo provider error payloads.
    flags = (
        "BlockPublicAcls", "IgnorePublicAcls",
        "BlockPublicPolicy", "RestrictPublicBuckets",
    )
    try:
        response = client.get_public_access_block(Bucket=normalized)
        configuration = response.get("PublicAccessBlockConfiguration")
        enabled = isinstance(configuration, dict) and all(
            configuration.get(flag) is True for flag in flags
        )
    except Exception:
        enabled = False
    checks.append(PreflightCheck(
        code="bucket.public_access_block",
        status="pass" if enabled else "fail",
        message=("Bucket Block Public Access is enabled" if enabled else
                 "Bucket Block Public Access could not be confirmed fully enabled"),
        remediation=None if enabled else (
            "Enable all four bucket Block Public Access settings and grant "
            "s3:GetBucketPublicAccessBlock; the provider must support this API"
        ),
    ))

    ok = all(check.status != "fail" for check in checks)
    return PreflightResult(
        bucket=normalized,
        region=region,
        ok=ok,
        checks=tuple(checks),
    )
