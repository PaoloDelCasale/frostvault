"""Build and synchronize application-owned S3 lifecycle rules.

Two layouts (ADR-0049). A shared bucket (the bucket of older Vaults) holds one
prefix-filtered plan rule per Vault, next to rules FrostVault does not own, and
``sync_bucket_lifecycle_rules`` merges them. A Vault Bucket holds the Vault at
its root: ``vault_bucket_lifecycle_rules`` is its whole, unfiltered lifecycle.

Every lifecycle FrostVault writes sets ``TransitionDefaultMinimumObjectSize``
to ``all_storage_classes_128K``: objects smaller than 128 KiB never
transition and stay in Standard, as the Storage Plan documents.
"""
from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from .bucket_provisioning import TRANSITION_MINIMUM_OBJECT_SIZE
from .lifecycle_profiles import LifecycleProfile
from .s3 import error_code

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


def vault_bucket_lifecycle_rules(
    vault_uuid: str, profile: LifecycleProfile,
) -> tuple[dict[str, Any], ...]:
    """The whole lifecycle of a Vault Bucket: its Storage Plan, then the abort rule.

    No filter: the bucket holds only this Vault. Same rule IDs as on a shared
    bucket, so a rule always names its Vault.
    """
    from .lifecycle_profiles import validate_lifecycle_profile

    validation = validate_lifecycle_profile(profile)
    if not validation.ok:
        raise ValueError("; ".join(validation.errors))
    rules: list[dict[str, Any]] = []
    if profile.transitions:
        rules.append({
            "ID": f"{RULE_ID_PREFIX}{vault_uuid}", "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "Transitions": [{"Days": transition.days, "StorageClass": transition.storage_class}
                            for transition in profile.transitions],
        })
    rules.append({**incomplete_upload_rule(), "Filter": {"Prefix": ""}})
    return tuple(rules)


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
    prefix = vault_object_prefix(prefix)
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


def vault_object_prefix(prefix: str) -> str:
    # Match the existing storage reader/writer normalization. Preserve legacy
    # namespaces rather than inventing UUID paths or moving cloud content.
    normalized = prefix.strip("/")
    if not normalized:
        raise ValueError("A storage plan cannot target an empty Vault prefix")
    return f"{normalized}/"


def validate_vault_prefixes(prefixes: list[str]) -> None:
    normalized = sorted(vault_object_prefix(prefix) for prefix in prefixes)
    for previous, current in zip(normalized, normalized[1:]):
        if current.startswith(previous):
            raise ValueError("Overlapping Vault prefixes cannot safely receive storage plans")


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
        if error_code(exc) == "NoSuchLifecycleConfiguration":
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
        TransitionDefaultMinimumObjectSize=TRANSITION_MINIMUM_OBJECT_SIZE,
    )
