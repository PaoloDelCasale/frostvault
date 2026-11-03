"""Persist and synchronize exactly one prefix-scoped storage plan per Vault."""
from __future__ import annotations

from typing import Any

from .lifecycle_profiles import profile_from_json, profile_to_json, validate_lifecycle_profile


def load_vault_plan(connection: Any, vault_id: int):
    row = connection.execute("SELECT storage_plan_json FROM vaults WHERE id=%s", (vault_id,)).fetchone()
    if row is None:
        raise ValueError("Vault not found")
    return profile_from_json(row["storage_plan_json"])


def set_vault_plan(connection: Any, vault_id: int, profile):
    validation = validate_lifecycle_profile(profile)
    if not validation.ok:
        raise ValueError("; ".join(validation.errors))
    connection.execute("UPDATE vaults SET storage_plan_json=%s WHERE id=%s", (profile_to_json(profile), vault_id))
    return validation


def sync_lifecycle_rules_for_bucket(connection: Any, client: Any, *, bucket: str) -> int:
    from .s3_lifecycle_rules import build_vault_lifecycle_rule, sync_bucket_lifecycle_rules

    rows = connection.execute(
        "SELECT uuid, s3_prefix, storage_plan_json FROM vaults WHERE s3_bucket=%s ORDER BY uuid",
        (bucket,),
    ).fetchall()
    rules = []
    for row in rows:
        plan = profile_from_json(row["storage_plan_json"])
        if plan.transitions:
            rules.append(build_vault_lifecycle_rule(str(row["uuid"]), row["s3_prefix"], plan))
    sync_bucket_lifecycle_rules(client, bucket=bucket, app_rules=rules)
    return len(rules)
