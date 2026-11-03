"""Build and synchronize application-owned S3 lifecycle rules."""
from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from .lifecycle_profiles import LifecycleProfile

RULE_ID_PREFIX = "frostvault-plan-"
LEGACY_RULE_ID_PREFIX = "psa-policy-"
MAX_BUCKET_LIFECYCLE_RULES = 1000
ABORT_RULE_ID = "frostvault-abort-incomplete-multipart"


def incomplete_upload_rule() -> dict[str, Any]:
    # Prefix-only: AWS forbids a Tag filter on multipart-abort rules. No expiry
    # of completed objects, noncurrent versions or delete markers is allowed.
    return {"ID": ABORT_RULE_ID, "Status": "Enabled",
            "Filter": {"Prefix": "vaults/"},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}


class LifecycleRuleLimitExceeded(RuntimeError):
    """Raised when syncing would exceed the S3 bucket lifecycle rule limit."""


def is_application_lifecycle_rule(rule: dict[str, Any]) -> bool:
    rule_id = str(rule.get("ID", ""))
    return rule_id.startswith((RULE_ID_PREFIX, LEGACY_RULE_ID_PREFIX)) or rule_id == ABORT_RULE_ID


def build_vault_lifecycle_rule(
    vault_uuid: str,
    prefix: str,
    profile: LifecycleProfile,
) -> dict[str, Any]:
    from .lifecycle_profiles import validate_lifecycle_profile

    validation = validate_lifecycle_profile(profile)
    if not validation.ok:
        raise ValueError("; ".join(validation.errors))
    if prefix != f"vaults/{vault_uuid}/":
        raise ValueError("Storage plans require the exact immutable Vault prefix")
    rule: dict[str, Any] = {
        "ID": f"{RULE_ID_PREFIX}{vault_uuid}",
        "Status": "Enabled",
        "Filter": {"Prefix": prefix},
    }
    if profile.transitions:
        rule["Transitions"] = [
            {
                "Days": transition.days,
                "StorageClass": transition.storage_class,
            }
            for transition in profile.transitions
        ]
    return rule


def merge_lifecycle_rules(
    existing_rules: list[dict[str, Any]],
    app_rules: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    preserved = [
        rule for rule in existing_rules if not is_application_lifecycle_rule(rule)
    ]
    return [*preserved, *app_rules]


def enforce_rule_limit(
    rules: list[dict[str, Any]],
    *,
    max_rules: int = MAX_BUCKET_LIFECYCLE_RULES,
) -> None:
    if len(rules) > max_rules:
        raise LifecycleRuleLimitExceeded(
            f"S3 lifecycle configuration would contain {len(rules)} rules; "
            f"the bucket limit is {max_rules}"
        )


def _load_existing_rules(client: Any, bucket: str) -> list[dict[str, Any]]:
    try:
        response = client.get_bucket_lifecycle_configuration(Bucket=bucket)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchLifecycleConfiguration":
            return []
        raise
    return list(response.get("Rules", []))


def sync_bucket_lifecycle_rules(
    client: Any,
    *,
    bucket: str,
    app_rules: list[dict[str, Any]],
) -> None:
    existing_rules = _load_existing_rules(client, bucket)
    merged_rules = merge_lifecycle_rules(existing_rules, [*app_rules, incomplete_upload_rule()])
    enforce_rule_limit(merged_rules)
    client.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={"Rules": merged_rules},
    )
