"""Reconcile desired and applied lifecycle policy tags on Archive Versions."""
from __future__ import annotations

from typing import Any

from .s3_object_tags import apply_version_policy_tag, clear_version_policy_tag


def reconcile_pending_policy_tags(
    connection: Any,
    vault: dict[str, Any],
    client: Any,
    *,
    batch_size: int = 50,
) -> int:
    apply_rows = connection.execute(
        """
        SELECT id, object_key, provider_version_id, desired_policy_id
        FROM archive_versions
        WHERE vault_id=%s
          AND desired_policy_id IS NOT NULL
          AND (applied_policy_id IS NULL OR applied_policy_id != desired_policy_id)
          AND availability != 'purged'
        LIMIT %s
        """,
        (vault["id"], batch_size),
    ).fetchall()
    clear_rows = connection.execute(
        """
        SELECT id, object_key, provider_version_id
        FROM archive_versions
        WHERE vault_id=%s
          AND desired_policy_id IS NULL
          AND applied_policy_id IS NOT NULL
          AND availability != 'purged'
        LIMIT %s
        """,
        (vault["id"], batch_size),
    ).fetchall()
    applied = 0
    for row in apply_rows:
        apply_version_policy_tag(
            client,
            bucket=vault["s3_bucket"],
            key=row["object_key"],
            version_id=row["provider_version_id"],
            policy_id=row["desired_policy_id"],
        )
        connection.execute(
            "UPDATE archive_versions SET applied_policy_id=%s WHERE id=%s",
            (row["desired_policy_id"], row["id"]),
        )
        applied += 1
    for row in clear_rows:
        clear_version_policy_tag(
            client,
            bucket=vault["s3_bucket"],
            key=row["object_key"],
            version_id=row["provider_version_id"],
        )
        connection.execute(
            "UPDATE archive_versions SET applied_policy_id=NULL WHERE id=%s",
            (row["id"],),
        )
        applied += 1
    return applied
