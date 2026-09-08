"""Weekly catalog audit against S3 object versions.

Compares Archive Versions and delete markers with ``list_object_versions``
metadata (storage class and policy tags) without restoring Glacier content.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .catalog_incidents import (
    remember_missing_archives,
    resolve_archive_version_incidents,
)
from .directory_aggregates import invalidate_for_archive_version_ids
from .s3_object_tags import read_version_policy_tag
from .vault_quotas import TERMINAL_JOB_STATUSES


def _object_prefix(vault: dict[str, Any]) -> str:
    prefix = str(vault.get("s3_prefix") or "").strip("/")
    return f"{prefix}/" if prefix else ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _list_cloud_versions(
    client: Any,
    vault: dict[str, Any],
) -> tuple[dict[tuple[str, str], dict[str, Any]], set[tuple[str, str]]]:
    paginator = client.get_paginator("list_object_versions")
    kwargs: dict[str, Any] = {"Bucket": vault["s3_bucket"]}
    prefix = _object_prefix(vault)
    if prefix:
        kwargs["Prefix"] = prefix
    versions: dict[tuple[str, str], dict[str, Any]] = {}
    markers: set[tuple[str, str]] = set()
    for page in paginator.paginate(**kwargs):
        for item in page.get("Versions", []):
            versions[(item["Key"], item["VersionId"])] = item
        for item in page.get("DeleteMarkers", []):
            markers.add((item["Key"], item["VersionId"]))
    return versions, markers


def _active_upload_file_ids(connection: Any, vault_id: int) -> set[str]:
    placeholders = ",".join(["%s"] * len(TERMINAL_JOB_STATUSES))
    rows = connection.execute(
        f"""
        SELECT DISTINCT vault_file_id
        FROM jobs
        WHERE vault_id=%s
          AND action='upload'
          AND vault_file_id IS NOT NULL
          AND status NOT IN ({placeholders})
        """,
        (vault_id, *TERMINAL_JOB_STATUSES),
    ).fetchall()
    return {str(row["vault_file_id"]) for row in rows if row["vault_file_id"]}


def audit_vault_catalog(
    connection: Any,
    vault: dict[str, Any],
    client: Any,
) -> dict[str, int]:
    """Audit one vault's catalog against live S3 version metadata.

    Updates storage class, applied policy tags, and availability from the
    listing and ``get_object_tagging`` only. Never calls restore or GetObject.
    Incomplete listings raise before any availability mutation so they cannot
    open false missing-archive incidents.
    """
    audit_started = _now_iso()
    cloud_versions, cloud_markers = _list_cloud_versions(client, vault)
    catalog_versions = connection.execute(
        """
        SELECT av.id, av.vault_id, av.vault_file_id, av.object_key,
               av.provider_version_id, av.storage_class, av.desired_policy_id,
               av.applied_policy_id, av.availability, av.discovered_at,
               fp.path
        FROM archive_versions av
        LEFT JOIN file_paths fp
          ON fp.vault_file_id=av.vault_file_id AND fp.valid_to IS NULL
        WHERE av.vault_id=%s AND av.availability != 'purged'
        """,
        (vault["id"],),
    ).fetchall()
    catalog_markers = connection.execute(
        """
        SELECT object_key, provider_version_id
        FROM delete_markers
        WHERE vault_id=%s
        """,
        (vault["id"],),
    ).fetchall()
    active_uploads = _active_upload_file_ids(connection, int(vault["id"]))

    report = {
        "catalog_versions": len(catalog_versions),
        "cloud_versions": len(cloud_versions),
        "missing_in_cloud": 0,
        "missing_in_catalog": 0,
        "storage_class_drift": 0,
        "policy_tag_drift": 0,
        "missing_delete_markers": 0,
        "command_ok": 1,
        "healthy": 1,
        "incidents_opened": 0,
        "incidents_resolved": 0,
    }

    # Aggregate-affecting mutations only (availability / storage_class). Policy
    # tag drift updates applied_policy_id which is not part of the projection.
    dirty_version_ids: list[str] = []
    missing_rows: list[dict[str, Any]] = []
    restored_ids: list[str] = []
    seen: set[tuple[str, str]] = set()
    for row in catalog_versions:
        key = (row["object_key"], row["provider_version_id"])
        seen.add(key)
        cloud = cloud_versions.get(key)
        if cloud is None:
            if str(row["vault_file_id"]) in active_uploads:
                continue
            discovered = str(row["discovered_at"] or "")
            if discovered > audit_started:
                continue
            report["missing_in_cloud"] += 1
            missing_rows.append(dict(row))
            if row["availability"] != "missing":
                connection.execute(
                    "UPDATE archive_versions SET availability='missing' WHERE id=%s",
                    (row["id"],),
                )
                dirty_version_ids.append(str(row["id"]))
            continue

        storage_class = cloud.get("StorageClass") or "STANDARD"
        became_available = False
        if storage_class != (row["storage_class"] or "STANDARD"):
            report["storage_class_drift"] += 1
            source = "policy" if row.get("desired_policy_id") else "discovered"
            connection.execute(
                """
                UPDATE archive_versions
                SET storage_class=%s,
                    storage_class_source=%s,
                    availability='available'
                WHERE id=%s
                """,
                (storage_class, source, row["id"]),
            )
            dirty_version_ids.append(str(row["id"]))
            became_available = True
        elif row["availability"] != "available":
            connection.execute(
                "UPDATE archive_versions SET availability='available' WHERE id=%s",
                (row["id"],),
            )
            dirty_version_ids.append(str(row["id"]))
            became_available = True
        if became_available:
            restored_ids.append(str(row["id"]))

        try:
            applied = read_version_policy_tag(
                client,
                bucket=vault["s3_bucket"],
                key=row["object_key"],
                version_id=row["provider_version_id"],
            )
        except Exception:
            applied = row["applied_policy_id"]
        if applied != row["applied_policy_id"]:
            report["policy_tag_drift"] += 1
            connection.execute(
                "UPDATE archive_versions SET applied_policy_id=%s WHERE id=%s",
                (applied, row["id"]),
            )
        elif (
            row["desired_policy_id"]
            and row["applied_policy_id"]
            and row["desired_policy_id"] != row["applied_policy_id"]
        ):
            report["policy_tag_drift"] += 1

    for key in cloud_versions:
        if key not in seen:
            report["missing_in_catalog"] += 1

    catalog_marker_keys = {
        (row["object_key"], row["provider_version_id"]) for row in catalog_markers
    }
    for key in cloud_markers:
        if key not in catalog_marker_keys:
            report["missing_delete_markers"] += 1

    if dirty_version_ids:
        invalidate_for_archive_version_ids(connection, dirty_version_ids)

    opened = remember_missing_archives(
        connection,
        missing_rows,
        reason="audit_missing_in_cloud",
        observed_at=audit_started,
    )
    resolved = resolve_archive_version_incidents(
        connection, restored_ids, observed_at=audit_started
    )
    report["incidents_opened"] = len(opened)
    report["incidents_resolved"] = resolved
    report["healthy"] = 0 if report["missing_in_cloud"] else 1
    return report


def audit_all_vaults(connection_factory: Any, client_factory: Any) -> list[dict[str, Any]]:
    """Run catalog audits for every enabled vault."""
    with connection_factory() as connection:
        vaults = connection.execute(
            """
            SELECT * FROM vaults
            WHERE enabled=TRUE AND decommission_state='active'
            ORDER BY id
            """
        ).fetchall()
    reports: list[dict[str, Any]] = []
    for vault in vaults:
        with connection_factory() as connection:
            report = audit_vault_catalog(connection, vault, client_factory())
            reports.append({"vault_id": vault["id"], **report})
    return reports
