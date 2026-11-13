"""Persist and synchronize exactly one prefix-scoped storage plan per Vault."""
from __future__ import annotations

from typing import Any

from .lifecycle_profiles import (
    LifecycleProfile,
    ProfileValidation,
    profile_from_json,
    profile_to_json,
    validate_lifecycle_profile,
)


def load_vault_plan(connection: Any, vault_id: int) -> LifecycleProfile:
    row = connection.execute(
        "SELECT storage_plan::text AS storage_plan_json FROM vaults WHERE id=%s",
        (vault_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Vault not found")
    return profile_from_json(row["storage_plan_json"])


def set_vault_plan(
    connection: Any, vault_id: int, profile: LifecycleProfile,
) -> ProfileValidation:
    validation = validate_lifecycle_profile(profile)
    if not validation.ok:
        raise ValueError("; ".join(validation.errors))
    connection.execute(
        "UPDATE vaults SET storage_plan=%s::jsonb WHERE id=%s",
        (profile_to_json(profile), vault_id),
    )
    return validation


def sync_lifecycle_rules_for_bucket(connection: Any, client: Any, *, bucket: str) -> int:
    from .s3_lifecycle_rules import (
        build_vault_lifecycle_rule,
        sync_bucket_lifecycle_rules,
        validate_vault_prefixes,
    )

    rows = connection.execute(
        """
        SELECT v.uuid, 'vaults/' || v.uuid::text || '/' AS s3_prefix,
               v.storage_plan::text AS storage_plan_json
        FROM vaults v JOIN vault_targets t ON t.id = v.target_id
        WHERE t.bucket=%s ORDER BY v.uuid
        """,
        (bucket,),
    ).fetchall()
    plans = [(row, profile_from_json(row["storage_plan_json"])) for row in rows]
    if any(plan.transitions for _, plan in plans):
        # Include Standard Vaults: a transitioning ancestor must not capture
        # another Vault which deliberately has no transition rules.
        validate_vault_prefixes([row["s3_prefix"] for row, _ in plans])
    rules = [
        build_vault_lifecycle_rule(str(row["uuid"]), row["s3_prefix"], plan)
        for row, plan in plans if plan.transitions
    ]
    sync_bucket_lifecycle_rules(client, bucket=bucket, app_rules=rules)
    return len(rules)
