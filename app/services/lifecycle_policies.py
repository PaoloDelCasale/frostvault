"""Lifecycle policy assignment and resolution for Archive Versions.

Policies are identified by immutable UUIDs in both the database and S3 object
tags. Vault defaults and folder overrides determine the effective policy for a
logical path without relying on readable object key prefixes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
import uuid

from .lifecycle_profiles import (
    LifecycleProfile,
    ProfileValidation,
    profile_from_json,
    profile_to_json,
    validate_lifecycle_profile,
)

POLICY_TAG_KEY = "psa:policy-id"


@dataclass(frozen=True)
class PolicyAssignments:
    default_policy_id: str | None
    folder_overrides: tuple[tuple[str, str], ...]


def normalize_logical_path(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def resolve_effective_policy_id(path: str, assignments: PolicyAssignments) -> str | None:
    normalized = normalize_logical_path(path)
    best_match: str | None = None
    best_len = -1
    for folder_prefix, policy_id in assignments.folder_overrides:
        prefix = normalize_logical_path(folder_prefix)
        if normalized == prefix or normalized.startswith(f"{prefix}/"):
            if len(prefix) > best_len:
                best_len = len(prefix)
                best_match = policy_id
    if best_match is not None:
        return best_match
    return assignments.default_policy_id


def policy_object_tags(policy_id: str) -> dict[str, str]:
    return {POLICY_TAG_KEY: policy_id}


def read_policy_id_from_tags(tags: dict[str, str]) -> str | None:
    value = tags.get(POLICY_TAG_KEY)
    if not value:
        return None
    return value


def create_policy(connection: Any, *, vault_id: int, name: str) -> str:
    policy_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO lifecycle_policies(id, vault_id, name, created_at)
        VALUES (%s, %s, %s, %s)
        """,
        (policy_id, vault_id, name, created_at),
    )
    return policy_id


def set_vault_default_policy(connection: Any, vault_id: int, policy_id: str) -> None:
    connection.execute(
        "UPDATE vaults SET default_lifecycle_policy_id=%s WHERE id=%s",
        (policy_id, vault_id),
    )
    refresh_desired_policies(connection, vault_id)


def set_folder_override(
    connection: Any,
    *,
    vault_id: int,
    folder_path: str,
    policy_id: str,
) -> None:
    normalized = normalize_logical_path(folder_path)
    connection.execute(
        """
        INSERT INTO folder_policy_overrides(vault_id, folder_path, policy_id)
        VALUES (%s, %s, %s)
        ON CONFLICT(vault_id, folder_path) DO UPDATE SET policy_id=excluded.policy_id
        """,
        (vault_id, normalized, policy_id),
    )
    refresh_desired_policies(connection, vault_id)


def load_policy_assignments(connection: Any, vault_id: int) -> PolicyAssignments:
    vault = connection.execute(
        "SELECT default_lifecycle_policy_id FROM vaults WHERE id=%s",
        (vault_id,),
    ).fetchone()
    rows = connection.execute(
        """
        SELECT folder_path, policy_id
        FROM folder_policy_overrides
        WHERE vault_id=%s
        ORDER BY folder_path
        """,
        (vault_id,),
    ).fetchall()
    return PolicyAssignments(
        default_policy_id=vault["default_lifecycle_policy_id"] if vault else None,
        folder_overrides=tuple((row["folder_path"], row["policy_id"]) for row in rows),
    )


def refresh_desired_policies(connection: Any, vault_id: int) -> int:
    """Recompute desired_policy_id for every Archive Version in a vault."""
    from .lifecycle_pins import is_path_pinned

    assignments = load_policy_assignments(connection, vault_id)
    versions = connection.execute(
        """
        SELECT av.id, fp.path
        FROM archive_versions av
        JOIN vault_files vf ON vf.id = av.vault_file_id
        JOIN file_paths fp ON fp.vault_file_id = vf.id AND fp.valid_to IS NULL
        WHERE av.vault_id=%s
        """,
        (vault_id,),
    ).fetchall()
    for row in versions:
        if is_path_pinned(connection, vault_id, row["path"]):
            desired = None
        else:
            desired = resolve_effective_policy_id(row["path"], assignments)
        connection.execute(
            "UPDATE archive_versions SET desired_policy_id=%s WHERE id=%s",
            (desired, row["id"]),
        )
    return len(versions)


def set_policy_profile(
    connection: Any,
    policy_id: str,
    profile: LifecycleProfile,
) -> ProfileValidation:
    validation = validate_lifecycle_profile(profile)
    if not validation.ok:
        return validation
    connection.execute(
        "UPDATE lifecycle_policies SET profile_json=%s WHERE id=%s",
        (profile_to_json(profile), policy_id),
    )
    return validation


def load_bucket_policy_profiles(
    connection: Any,
    bucket: str,
) -> list[tuple[str, LifecycleProfile]]:
    rows = connection.execute(
        """
        SELECT lp.id, lp.profile_json
        FROM lifecycle_policies lp
        JOIN vaults v ON v.id = lp.vault_id
        WHERE v.s3_bucket = %s AND lp.profile_json IS NOT NULL
        ORDER BY lp.id
        """,
        (bucket,),
    ).fetchall()
    policies: list[tuple[str, LifecycleProfile]] = []
    for row in rows:
        profile = profile_from_json(row["profile_json"])
        if profile is not None:
            policies.append((row["id"], profile))
    return policies


def list_vault_policies(connection: Any, vault_id: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT id, name, created_at, profile_json
        FROM lifecycle_policies
        WHERE vault_id=%s
        ORDER BY created_at, id
        """,
        (vault_id,),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        profile = profile_from_json(row["profile_json"])
        items.append(
            {
                "id": row["id"],
                "name": row["name"],
                "created_at": row["created_at"],
                "profile": (
                    {
                        "transitions": [
                            {
                                "days": transition.days,
                                "storage_class": transition.storage_class,
                            }
                            for transition in profile.transitions
                        ],
                        "expiration_days": profile.expiration_days,
                        "noncurrent_expiration_days": profile.noncurrent_expiration_days,
                    }
                    if profile is not None
                    else None
                ),
            }
        )
    return items


def clear_folder_override(
    connection: Any,
    *,
    vault_id: int,
    folder_path: str,
) -> None:
    normalized = normalize_logical_path(folder_path)
    connection.execute(
        "DELETE FROM folder_policy_overrides WHERE vault_id=%s AND folder_path=%s",
        (vault_id, normalized),
    )
    refresh_desired_policies(connection, vault_id)


def apply_guided_profile_to_policy(
    connection: Any,
    *,
    policy_id: str,
    guided_profile: str,
) -> ProfileValidation:
    from .lifecycle_profiles import guided_profile as load_guided

    return set_policy_profile(connection, policy_id, load_guided(guided_profile))


def ensure_default_policy_with_profile(
    connection: Any,
    *,
    vault_id: int,
    name: str,
    guided_profile: str,
) -> tuple[str, ProfileValidation]:
    from .lifecycle_profiles import guided_profile as load_guided

    vault = connection.execute(
        "SELECT default_lifecycle_policy_id FROM vaults WHERE id=%s",
        (vault_id,),
    ).fetchone()
    policy_id = vault["default_lifecycle_policy_id"] if vault else None
    if not policy_id:
        policy_id = create_policy(connection, vault_id=vault_id, name=name)
        set_vault_default_policy(connection, vault_id, policy_id)
    validation = set_policy_profile(connection, policy_id, load_guided(guided_profile))
    return policy_id, validation


def sync_lifecycle_rules_for_bucket(
    connection: Any,
    client: Any,
    *,
    bucket: str,
) -> int:
    from .s3_lifecycle_rules import build_rules_for_policies, sync_bucket_lifecycle_rules

    app_rules = build_rules_for_policies(load_bucket_policy_profiles(connection, bucket))
    sync_bucket_lifecycle_rules(client, bucket=bucket, app_rules=app_rules)
    return len(app_rules)
