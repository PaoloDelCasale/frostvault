"""Create, configure and verify a FrostVault-owned S3 bucket (ADR-0049).

One seam for every bucket FrostVault creates: the System Bucket (#734) and the
Vault Buckets (#735). A ``BucketSpec`` names the bucket, its region, its
``frostvault:*`` tags, its lifecycle rules and an optional Governance
Retention Lock; everything else is the same for every bucket:

- Block Public Access with all four settings on;
- versioning Enabled;
- default encryption SSE-S3 (``AES256``);
- Object Ownership ``BucketOwnerEnforced`` (ACLs off).

``provision_bucket`` is idempotent and resumable: ``BucketAlreadyOwnedByYou``
is not an error, and the configuration is applied again and then verified, so
a run interrupted at any step completes on the next call. ``BucketAlreadyExists``
raises ``BucketNameUnavailable`` and the caller picks a new name; ownership is
decided by these two CreateBucket error codes only (no HeadBucket probe, no
``ExpectedBucketOwner``, so no extra IAM permission). Every other error is an
error, never a reason to rename. ``verify_baseline`` checks an
operator-supplied bucket without changing it; ``verify_bucket`` checks a
bucket FrostVault created against its spec.

Callers inject the SDK client. Messages carry provider error codes only, never
provider messages, which can contain account identifiers.
"""
from __future__ import annotations

import secrets
import string
from dataclasses import dataclass, field
from typing import Any, Mapping

from botocore.exceptions import ClientError

NAME_PREFIX = "frostvault-"
_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits
_PUBLIC_ACCESS_FLAGS = (
    "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets",
)
_LIFECYCLE_ACTIONS = (
    "Expiration", "Transitions", "NoncurrentVersionTransitions",
    "NoncurrentVersionExpiration", "AbortIncompleteMultipartUpload",
)
_ALREADY_OWNED = frozenset({"BucketAlreadyOwnedByYou"})
_NAME_TAKEN = frozenset({"BucketAlreadyExists"})
# GetBucketLocation answers a client of another region with these.
_OTHER_REGION = frozenset({"PermanentRedirect", "AuthorizationHeaderMalformed"})
OWNERSHIP = "BucketOwnerEnforced"


@dataclass(frozen=True)
class RetentionLock:
    """Default Object Lock retention of a bucket: Governance mode only."""

    days: int
    mode: str = "GOVERNANCE"

    def __post_init__(self) -> None:
        if self.mode != "GOVERNANCE":
            raise ValueError("FrostVault only applies a Governance-mode Retention Lock")
        if self.days < 1:
            raise ValueError("A Retention Lock lasts at least one day")


@dataclass(frozen=True)
class BucketSpec:
    """Everything that differs between two FrostVault-owned buckets."""

    name: str
    region: str
    tags: Mapping[str, str] = field(default_factory=dict)
    # The lifecycle configuration FrostVault applies (see ``verify_bucket``).
    lifecycle_rules: tuple[Mapping[str, Any], ...] = ()
    retention_lock: RetentionLock | None = None


@dataclass(frozen=True)
class BucketCheck:
    code: str
    ok: bool
    message: str


@dataclass(frozen=True)
class BucketVerification:
    bucket: str
    region: str
    checks: tuple[BucketCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def failure_message(self) -> str:
        return "; ".join(check.message for check in self.checks if not check.ok)


class BucketProvisioningError(RuntimeError):
    """A bucket could not be created or configured; the message is safe to store."""


class BucketNameUnavailable(BucketProvisioningError):
    """Another AWS account owns this bucket name."""


class BucketVerificationFailed(BucketProvisioningError):
    def __init__(self, verification: BucketVerification):
        super().__init__(verification.failure_message())
        self.verification = verification


def random_bucket_name(stem: str, *, length: int = 8) -> str:
    """``<stem>-<length random [a-z0-9]>``, e.g. ``frostvault-system-k3x9q0ab``."""
    if not stem.startswith(NAME_PREFIX):
        raise ValueError("FrostVault bucket names start with frostvault-")
    name = f"{stem}-" + "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(length))
    if not 3 <= len(name) <= 63:
        raise ValueError("S3 bucket names are 3 to 63 characters long")
    return name


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code") or "ClientError")


def _failed(action: str, exc: ClientError) -> BucketProvisioningError:
    return BucketProvisioningError(f"S3 refused {action} ({_error_code(exc)})")


def create_bucket(client: Any, spec: BucketSpec) -> None:
    """Create the bucket; ``BucketAlreadyOwnedByYou`` counts as created.

    ``BucketAlreadyExists`` raises ``BucketNameUnavailable``: the name is
    someone else's (a provider answering its owner this way loses one name,
    never adopts a foreign bucket). Any other refusal is an error.
    """
    request: dict[str, Any] = {"Bucket": spec.name}
    if spec.region != "us-east-1":
        request["CreateBucketConfiguration"] = {"LocationConstraint": spec.region}
    if spec.retention_lock is not None:
        request["ObjectLockEnabledForBucket"] = True
    try:
        client.create_bucket(**request)
    except ClientError as exc:
        code = _error_code(exc)
        if code in _ALREADY_OWNED:
            return
        if code in _NAME_TAKEN:
            raise BucketNameUnavailable(
                "The bucket name belongs to another AWS account"
            ) from None
        raise _failed("s3:CreateBucket", exc) from None


def configure_bucket(client: Any, spec: BucketSpec) -> None:
    """Apply the whole configuration again; every step is idempotent."""
    name = spec.name
    steps: list[tuple[str, Any, dict[str, Any]]] = [
        ("s3:PutBucketPublicAccessBlock", client.put_public_access_block, {
            "PublicAccessBlockConfiguration": {flag: True for flag in _PUBLIC_ACCESS_FLAGS},
        }),
        ("s3:PutBucketVersioning", client.put_bucket_versioning, {
            "VersioningConfiguration": {"Status": "Enabled"},
        }),
        ("s3:PutEncryptionConfiguration", client.put_bucket_encryption, {
            "ServerSideEncryptionConfiguration": {"Rules": [{
                "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
            }]},
        }),
        ("s3:PutBucketOwnershipControls", client.put_bucket_ownership_controls, {
            "OwnershipControls": {"Rules": [{"ObjectOwnership": OWNERSHIP}]},
        }),
    ]
    for action, call, arguments in steps:
        try:
            call(Bucket=name, **arguments)
        except ClientError as exc:
            raise _failed(action, exc) from None
    try:
        if spec.lifecycle_rules:
            client.put_bucket_lifecycle_configuration(
                Bucket=name,
                LifecycleConfiguration={"Rules": [dict(rule) for rule in spec.lifecycle_rules]},
            )
        else:
            client.delete_bucket_lifecycle(Bucket=name)
    except ClientError as exc:
        raise _failed("s3:PutLifecycleConfiguration", exc) from None
    if spec.tags:
        # Keep tags someone else added (cost allocation, AWS Organizations).
        tags = {**_bucket_tags(client, name, action="s3:GetBucketTagging"), **spec.tags}
        try:
            client.put_bucket_tagging(Bucket=name, Tagging={"TagSet": [
                {"Key": key, "Value": value} for key, value in sorted(tags.items())
            ]})
        except ClientError as exc:
            raise _failed("s3:PutBucketTagging", exc) from None
    if spec.retention_lock is not None:
        try:
            client.put_object_lock_configuration(Bucket=name, ObjectLockConfiguration={
                "ObjectLockEnabled": "Enabled",
                "Rule": {"DefaultRetention": {
                    "Mode": spec.retention_lock.mode, "Days": spec.retention_lock.days,
                }},
            })
        except ClientError as exc:
            raise _failed("s3:PutBucketObjectLockConfiguration", exc) from None


def provision_bucket(client: Any, spec: BucketSpec) -> BucketVerification:
    """Create, configure and verify; raise unless every check passes."""
    create_bucket(client, spec)
    configure_bucket(client, spec)
    verification = verify_bucket(client, spec)
    if not verification.ok:
        raise BucketVerificationFailed(verification)
    return verification


def verify_baseline(client: Any, name: str, region: str) -> BucketVerification:
    """Region, Block Public Access, versioning and SSE-S3, changing nothing."""
    return BucketVerification(bucket=name, region=region,
                              checks=tuple(_baseline_checks(client, name, region)))


def verify_bucket(client: Any, spec: BucketSpec, *, extra_lifecycle_rules: bool = False,
                  require_tags: bool = True) -> BucketVerification:
    """The baseline plus Object Ownership and the spec's lifecycle, tags and lock.

    By default the lifecycle must be exactly the spec's rules (Vault Buckets,
    #735) and the spec's tags must be present. ``extra_lifecycle_rules``
    accepts rules the Owner added next to the spec's, which must still be
    present unchanged; ``require_tags=False`` leaves the tags to the Owner
    (the System Bucket).
    """
    checks = _baseline_checks(client, spec.name, spec.region)
    if all(check.ok for check in checks):
        checks.append(_ownership_check(client, spec.name))
        checks.append(_lifecycle_check(client, spec, extra_rules=extra_lifecycle_rules))
        if require_tags:
            checks.append(_tags_check(client, spec))
        checks.append(_retention_lock_check(client, spec))
    return BucketVerification(bucket=spec.name, region=spec.region, checks=tuple(checks))


def _baseline_checks(client: Any, name: str, region: str) -> list[BucketCheck]:
    try:
        location = client.get_bucket_location(Bucket=name).get("LocationConstraint")
    except ClientError as exc:
        code = _error_code(exc)
        if code in {"NoSuchBucket", "404", "NotFound"}:
            return [BucketCheck("bucket.exists", False, "The bucket does not exist")]
        if code in _OTHER_REGION:
            return [BucketCheck("bucket.region", False,
                                f"The bucket is in another region, not in {region}")]
        return [BucketCheck("bucket.access", False,
                            f"S3 refused s3:GetBucketLocation ({code})")]
    actual = location or "us-east-1"
    if actual == "EU":  # Legacy alias returned for eu-west-1.
        actual = "eu-west-1"
    return [
        BucketCheck("bucket.region", actual == region,
                    "The bucket is in the expected region" if actual == region
                    else f"The bucket is in {actual}, not in {region}"),
        _versioning_check(client, name),
        _public_access_check(client, name),
        _encryption_check(client, name),
    ]


def _refused(code: str, action: str, exc: ClientError) -> BucketCheck:
    return BucketCheck(code, False, f"S3 refused {action} ({_error_code(exc)})")


def _versioning_check(client: Any, name: str) -> BucketCheck:
    try:
        status = client.get_bucket_versioning(Bucket=name).get("Status")
    except ClientError as exc:
        return _refused("bucket.versioning", "s3:GetBucketVersioning", exc)
    return BucketCheck(
        "bucket.versioning", status == "Enabled",
        "Versioning is enabled" if status == "Enabled"
        else f"Versioning is {(status or 'disabled').lower()}, not enabled",
    )


def _public_access_check(client: Any, name: str) -> BucketCheck:
    try:
        configuration = client.get_public_access_block(Bucket=name).get(
            "PublicAccessBlockConfiguration")
    except ClientError as exc:
        if _error_code(exc) != "NoSuchPublicAccessBlockConfiguration":
            return _refused("bucket.public_access_block",
                            "s3:GetBucketPublicAccessBlock", exc)
        configuration = None
    blocked = isinstance(configuration, dict) and all(
        configuration.get(flag) is True for flag in _PUBLIC_ACCESS_FLAGS)
    return BucketCheck(
        "bucket.public_access_block", blocked,
        "Block Public Access is fully enabled" if blocked
        else "Block Public Access is not fully enabled (all four settings)",
    )


def _encryption_check(client: Any, name: str) -> BucketCheck:
    try:
        rules = client.get_bucket_encryption(Bucket=name).get(
            "ServerSideEncryptionConfiguration", {}).get("Rules") or []
    except ClientError as exc:
        if _error_code(exc) != "ServerSideEncryptionConfigurationNotFoundError":
            return _refused("bucket.encryption", "s3:GetEncryptionConfiguration", exc)
        rules = []
    algorithms = {
        str((rule.get("ApplyServerSideEncryptionByDefault") or {}).get("SSEAlgorithm"))
        for rule in rules
    }
    sse_s3 = algorithms == {"AES256"}
    return BucketCheck(
        "bucket.encryption", sse_s3,
        "Default encryption is SSE-S3" if sse_s3
        else "Default encryption is not SSE-S3 (AES256)",
    )


def _ownership_check(client: Any, name: str) -> BucketCheck:
    try:
        rules = client.get_bucket_ownership_controls(Bucket=name).get(
            "OwnershipControls", {}).get("Rules") or []
    except ClientError as exc:
        if _error_code(exc) != "OwnershipControlsNotFoundError":
            return _refused("bucket.ownership", "s3:GetBucketOwnershipControls", exc)
        rules = []
    enforced = [rule.get("ObjectOwnership") for rule in rules] == [OWNERSHIP]
    return BucketCheck(
        "bucket.ownership", enforced,
        "Object Ownership is BucketOwnerEnforced" if enforced
        else "Object Ownership is not BucketOwnerEnforced (ACLs must be off)",
    )


def _normalized_rule(rule: Mapping[str, Any]) -> dict[str, Any]:
    """The parts of a lifecycle rule that matter, as S3 returns or accepts them."""
    raw_filter = rule.get("Filter")
    if raw_filter is None and "Prefix" in rule:
        raw_filter = {"Prefix": rule["Prefix"]}
    rule_filter = {key: value for key, value in dict(raw_filter or {}).items()
                   if not (key == "Prefix" and value == "")}
    normalized = {"ID": rule.get("ID"), "Status": rule.get("Status"), "Filter": rule_filter}
    for action in _LIFECYCLE_ACTIONS:
        if rule.get(action):
            normalized[action] = rule[action]
    return normalized


def _lifecycle_check(client: Any, spec: BucketSpec, *, extra_rules: bool) -> BucketCheck:
    try:
        actual = client.get_bucket_lifecycle_configuration(Bucket=spec.name).get("Rules") or []
    except ClientError as exc:
        if _error_code(exc) != "NoSuchLifecycleConfiguration":
            return BucketCheck("bucket.lifecycle", False,
                               f"S3 refused s3:GetLifecycleConfiguration ({_error_code(exc)})")
        actual = []
    present = [_normalized_rule(rule) for rule in actual]
    wanted = [_normalized_rule(rule) for rule in spec.lifecycle_rules]
    if extra_rules:
        matches = all(rule in present for rule in wanted)
        return BucketCheck(
            "bucket.lifecycle", matches,
            "FrostVault's lifecycle rules are present" if matches
            else "FrostVault's lifecycle rules are missing or changed",
        )
    key = lambda rule: str(rule.get("ID"))  # noqa: E731
    matches = sorted(present, key=key) == sorted(wanted, key=key)
    return BucketCheck(
        "bucket.lifecycle", matches,
        "The lifecycle rules are FrostVault's" if matches
        else "The lifecycle rules differ from FrostVault's",
    )


def _bucket_tags(client: Any, name: str, *, action: str) -> dict[str, str]:
    try:
        tag_set = client.get_bucket_tagging(Bucket=name).get("TagSet") or []
    except ClientError as exc:
        if _error_code(exc) == "NoSuchTagSet":
            return {}
        raise _failed(action, exc) from None
    return {str(tag["Key"]): str(tag["Value"]) for tag in tag_set}


def _tags_check(client: Any, spec: BucketSpec) -> BucketCheck:
    try:
        tags = _bucket_tags(client, spec.name, action="s3:GetBucketTagging")
    except BucketProvisioningError as exc:
        return BucketCheck("bucket.tags", False, str(exc))
    present = all(tags.get(key) == value for key, value in spec.tags.items())
    return BucketCheck(
        "bucket.tags", present,
        "The frostvault tags are present" if present else "The frostvault tags are missing",
    )


def _retention_lock_check(client: Any, spec: BucketSpec) -> BucketCheck:
    try:
        configuration = client.get_object_lock_configuration(Bucket=spec.name).get(
            "ObjectLockConfiguration") or {}
    except ClientError as exc:
        if _error_code(exc) != "ObjectLockConfigurationNotFoundError":
            return BucketCheck(
                "bucket.retention_lock", False,
                f"S3 refused s3:GetBucketObjectLockConfiguration ({_error_code(exc)})")
        configuration = {}
    enabled = configuration.get("ObjectLockEnabled") == "Enabled"
    default = (configuration.get("Rule") or {}).get("DefaultRetention") or {}
    lock = spec.retention_lock
    if lock is None:
        return BucketCheck(
            "bucket.retention_lock", not enabled,
            "Object Lock is off" if not enabled
            else "Object Lock is enabled on a bucket that must not have it",
        )
    matches = (enabled and default.get("Mode") == lock.mode
               and default.get("Days") == lock.days and not default.get("Years"))
    return BucketCheck(
        "bucket.retention_lock", matches,
        f"The Retention Lock is {lock.mode} for {lock.days} days" if matches
        else f"The Retention Lock is not {lock.mode} for {lock.days} days",
    )
