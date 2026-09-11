"""Cloud History Policy: Archive History vs Current Snapshot (ADR-0016)."""

from __future__ import annotations

from typing import Any, Mapping

ARCHIVE_HISTORY = "archive_history"
CURRENT_SNAPSHOT = "current_snapshot"
CLOUD_HISTORY_POLICIES = frozenset({ARCHIVE_HISTORY, CURRENT_SNAPSHOT})


class InvalidCloudHistoryPolicy(ValueError):
    """The supplied Cloud History Policy is missing or not a known value."""


def normalize_cloud_history_policy(
    value: str | None,
    *,
    required: bool = False,
) -> str:
    """Return a canonical policy, or Archive History when omitted.

    Interactive creation passes ``required=True`` so the form cannot silently
    pick Archive History. Bootstrap and existing rows use the Archive History
    default.
    """
    if value is None or not str(value).strip():
        if required:
            raise InvalidCloudHistoryPolicy("cloud_history_policy is required")
        return ARCHIVE_HISTORY
    policy = str(value).strip().lower()
    if policy not in CLOUD_HISTORY_POLICIES:
        raise InvalidCloudHistoryPolicy(
            "cloud_history_policy must be 'archive_history' or 'current_snapshot'"
        )
    return policy


def is_current_snapshot(vault: Mapping[str, Any] | None) -> bool:
    if not vault:
        return False
    return (
        str(vault.get("cloud_history_policy") or ARCHIVE_HISTORY)
        == CURRENT_SNAPSHOT
    )


def keep_discovered_snapshot_version(
    *,
    provider_version_id: str,
    catalog_keep_provider_version_id: str | None,
    already_kept_provider_version_id: str | None,
) -> bool:
    """Return whether a listed S3 version should stay the Current Snapshot.

    A path already assigned in this scan keeps only that VersionId. A Vault File
    that already has a recoverable Archive Version keeps that exact VersionId.
    Otherwise the first candidate (callers pass newest first) is kept.
    """
    if already_kept_provider_version_id is not None:
        return provider_version_id == already_kept_provider_version_id
    if catalog_keep_provider_version_id is not None:
        return provider_version_id == catalog_keep_provider_version_id
    return True
