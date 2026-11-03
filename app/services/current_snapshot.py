"""Internal Current Snapshot reconciliation (ADR-0016)."""

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
