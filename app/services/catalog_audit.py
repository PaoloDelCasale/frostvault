"""Weekly catalog audit against S3 object versions.

Compares Archive Versions and delete markers with ``list_object_versions``
metadata (storage class and availability) without restoring Glacier content.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .catalog_incidents import (
    remember_missing_archives,
    resolve_archive_version_incidents,
)
from .directory_aggregates import invalidate_for_archive_version_ids
from .job_admission import TERMINAL_JOB_STATUSES


def _object_prefix(vault: dict[str, Any]) -> str:
    from .vaults import vault_prefix
    return vault_prefix(vault["uuid"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _list_cloud_versions(
    client: Any,
    vault: dict[str, Any],
) -> tuple[dict[tuple[str, str], dict[str, Any]], set[tuple[str, str]]]:
    paginator = client.get_paginator("list_object_versions")
    kwargs: dict[str, Any] = {"Bucket": vault["s3_bucket"], "Prefix": _object_prefix(vault)}
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
        SELECT DISTINCT file_id
        FROM jobs
        WHERE vault_id=%s
          AND action='upload'
          AND status NOT IN ({placeholders})
        """,
        (vault_id, *TERMINAL_JOB_STATUSES),
    ).fetchall()
    return {str(row["file_id"]) for row in rows}


def audit_vault_catalog(
    connection: Any,
    vault: dict[str, Any],
    client: Any,
) -> dict[str, int]:
    """Audit one vault's catalog against live S3 version metadata.

    Updates storage class and availability from the listing only.
    Never calls restore or GetObject.
    Incomplete listings raise before any availability mutation so they cannot
    open false missing-archive incidents.
    """
    audit_started = datetime.now(timezone.utc)
    cloud_versions, cloud_markers = _list_cloud_versions(client, vault)
    # Only versions the catalog knows exactly (object key + VersionId) are
    # compared; an unknown object or marker is reported, never attributed.
    catalog_versions = connection.execute(
        """
        SELECT av.id, f.vault_id, av.file_id, av.object_key,
               av.provider_version_id, av.storage_class,
               av.availability, av.created_at, f.path
        FROM archive_versions av
        JOIN files f ON f.id = av.file_id
        WHERE f.vault_id=%s
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
        "missing_delete_markers": len(cloud_markers),
        "command_ok": 1,
        "healthy": 1,
        "incidents_opened": 0,
        "incidents_resolved": 0,
    }

    # Aggregate-affecting mutations only (availability / storage_class).
    dirty_version_ids: list[str] = []
    missing_rows: list[dict[str, Any]] = []
    restored_ids: list[str] = []
    seen: set[tuple[str, str]] = set()
    for row in catalog_versions:
        key = (row["object_key"], row["provider_version_id"])
        seen.add(key)
        cloud = cloud_versions.get(key)
        if cloud is None:
            if str(row["file_id"]) in active_uploads:
                continue
            if row["created_at"] > audit_started:
                continue
            report["missing_in_cloud"] += 1
            missing_rows.append({**dict(row), "id": str(row["id"])})
            if row["availability"] != "missing":
                connection.execute(
                    "UPDATE archive_versions SET availability='missing', "
                    "availability_checked_at=%s WHERE id=%s",
                    (audit_started, row["id"]),
                )
                dirty_version_ids.append(str(row["id"]))
            continue

        storage_class = cloud.get("StorageClass") or "STANDARD"
        if storage_class != (row["storage_class"] or "STANDARD"):
            report["storage_class_drift"] += 1
        if (
            storage_class != (row["storage_class"] or "STANDARD")
            or row["availability"] != "available"
        ):
            connection.execute(
                """
                UPDATE archive_versions
                SET storage_class=%s, availability='available',
                    availability_checked_at=%s
                WHERE id=%s
                """,
                (storage_class, audit_started, row["id"]),
            )
            dirty_version_ids.append(str(row["id"]))
            if row["availability"] != "available":
                restored_ids.append(str(row["id"]))

    for key in cloud_versions:
        if key not in seen:
            report["missing_in_catalog"] += 1

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
        from .vaults import VAULT_ROW_SQL
        vaults = connection.execute(
            f"""
            {VAULT_ROW_SQL}
            WHERE v.lifecycle='active'
            ORDER BY v.id
            """
        ).fetchall()
    reports: list[dict[str, Any]] = []
    for vault in vaults:
        with connection_factory() as connection:
            report = audit_vault_catalog(connection, vault, client_factory())
            reports.append({"vault_id": vault["id"], **report})
    return reports
