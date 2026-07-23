"""Build and synchronize application-owned S3 lifecycle rules."""
from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from .lifecycle_policies import POLICY_TAG_KEY
from .lifecycle_profiles import LifecycleProfile

RULE_ID_PREFIX = "psa-policy-"
MAX_BUCKET_LIFECYCLE_RULES = 1000


class LifecycleRuleLimitExceeded(RuntimeError):
    """Raised when syncing would exceed the S3 bucket lifecycle rule limit."""


def is_application_lifecycle_rule(rule: dict[str, Any]) -> bool:
    rule_id = str(rule.get("ID", ""))
    return rule_id.startswith(RULE_ID_PREFIX)


def lifecycle_rule_id(policy_id: str) -> str:
    return f"{RULE_ID_PREFIX}{policy_id}"


def build_policy_lifecycle_rule(
    policy_id: str,
    profile: LifecycleProfile,
) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "ID": lifecycle_rule_id(policy_id),
        "Status": "Enabled",
        "Filter": {"Tag": {"Key": POLICY_TAG_KEY, "Value": policy_id}},
    }
    if profile.transitions:
        rule["Transitions"] = [
            {
                "Days": transition.days,
                "StorageClass": transition.storage_class,
            }
            for transition in profile.transitions
        ]
    if profile.expiration_days is not None:
        rule["Expiration"] = {"Days": profile.expiration_days}
    if profile.noncurrent_transitions:
        rule["NoncurrentVersionTransitions"] = [
            {
                "NoncurrentDays": transition.days,
                "StorageClass": transition.storage_class,
            }
            for transition in profile.noncurrent_transitions
        ]
    if profile.noncurrent_expiration_days is not None:
        rule["NoncurrentVersionExpiration"] = {
            "NoncurrentDays": profile.noncurrent_expiration_days
        }
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
    merged_rules = merge_lifecycle_rules(existing_rules, app_rules)
    enforce_rule_limit(merged_rules)
    client.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={"Rules": merged_rules},
    )


def build_rules_for_policies(
    policies: list[tuple[str, LifecycleProfile]],
) -> list[dict[str, Any]]:
    return [
        build_policy_lifecycle_rule(policy_id, profile)
        for policy_id, profile in policies
        if profile.transitions
        or profile.noncurrent_transitions
        or profile.expiration_days is not None
        or profile.noncurrent_expiration_days is not None
    ]
