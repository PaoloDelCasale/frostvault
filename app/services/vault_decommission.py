"""Auditable, restart-safe Vault decommission and root release (issue #153).

Disabling a Vault is deliberately unrelated to this lifecycle.  A root remains
occupied until ``vaults.root_released_at`` is written by the final transaction.
The Vault row, memberships, Vault Files, Path History, Jobs, encrypted recovery
material, audit events, and notifications remain as a tombstone/history.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..catalog import ArchiveCatalog
from ..database import db
from . import cloud_deletion, source_areas, source_layout, vault_relocation
from .audit_events import record_audit_event
from .notifications import enqueue_notification

LOCAL_DISPOSITIONS = frozenset({"retain", "remove"})
CLOUD_DISPOSITIONS = frozenset({"retain", "purge"})
TERMINAL_JOB_STATUSES = ("completed", "failed", "cancelled")
TERMINAL_OPERATION_STATES = ("completed",)
LOCAL_TERMINAL = frozenset({"retained", "removed"})
CLOUD_TERMINAL = frozenset({"retained", "purged"})

# Closes the in-process handoff window before the persistent lifecycle state is
# committed.  Persisted state remains authoritative after a restart.
_runtime_suspended_vaults: set[int] = set()


class VaultDecommissionError(Exception):
    """A stable machine-readable decommission failure."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason


_BLOCKER_MESSAGES = {
    "active_jobs": "Vault has active or pending Jobs",
    "pending_destructive_actions": "Vault has a pending destructive action",
    "scan_active": "A Vault scan is running",
    "relocation_in_progress": "Vault root relocation still requires recovery",
    "decommission_in_progress": "Vault decommission is already in progress",
    "already_decommissioned": "Vault root has already been released",
    "source_unavailable": "Source Volume and Vault root must be healthy",
    "root_identity_ambiguous": "Vault root identity is not enrolled",
    "root_identity_mismatch": "Vault root no longer matches its enrolled identity",
    "filesystem_unreadable": "Vault root could not be inventoried safely",
    "local_catalog_stale": "Local catalog does not match the current filesystem tree",
    "local_unsupported_entries": "Local removal cannot delete symlinks, special files, or nested mounts",
    "local_delete_disabled": "Local Copy deletion is disabled by system policy",
    "local_copy_unprotected": "Every Local Copy must match a verified available Archive Version",
    "cloud_deletion_disabled": "Permanent cloud deletion is disabled for this Vault",
    "cloud_identity_incomplete": "Cloud history contains an item without an exact provider version identity",
    "recovery_custody_unconfirmed": "Crypt recovery custody must be confirmed before retaining cloud history",
    "owner_missing": "Vault has no primary owner",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _blocker(code: str, *, count: int | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "code": code,
        "message": _BLOCKER_MESSAGES[code],
        "message_key": f"decommission.blocker.{code}",
    }
    if count is not None:
        item["count"] = int(count)
    return item


def _append_blocker(
    blockers: list[dict[str, Any]], code: str, *, count: int | None = None
) -> None:
    if any(item["code"] == code for item in blockers):
        return
    blockers.append(_blocker(code, count=count))


def _clean_reason(reason: str) -> str:
    cleaned = (reason or "").strip()
    if len(cleaned) < 3 or len(cleaned) > 500:
        raise VaultDecommissionError(
            "reason_required", "A reason between 3 and 500 characters is required"
        )
    return cleaned


def _validate_dispositions(local: str, cloud: str) -> tuple[str, str]:
    local_value = (local or "").strip().lower()
    cloud_value = (cloud or "").strip().lower()
    if local_value not in LOCAL_DISPOSITIONS:
        raise VaultDecommissionError(
            "invalid_disposition", "Local disposition must be retain or remove"
        )
    if cloud_value not in CLOUD_DISPOSITIONS:
        raise VaultDecommissionError(
            "invalid_disposition", "Cloud disposition must be retain or purge"
        )
    return local_value, cloud_value


def _filesystem_snapshot(vault: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic, non-secret root inventory for the fingerprint."""
    snapshot: dict[str, Any] = {
        "healthy": False,
        "identity": None,
        "entries": [],
        "data_paths": [],
        "regular_paths": [],
        "unsupported_paths": [],
        "file_count": 0,
        "byte_count": 0,
        "error": None,
    }
    access = source_layout.vault_local_access(vault["source_root"])
    snapshot["volume_alias"] = access.volume_alias
    snapshot["volume_health"] = access.volume_health
    if not access.local_operations_allowed:
        snapshot["error"] = "source_unavailable"
        return snapshot
    if (
        vault.get("root_identity_version")
        != vault_relocation.ROOT_IDENTITY_VERSION
        or not vault.get("root_identity")
    ):
        snapshot["error"] = "root_identity_ambiguous"
        return snapshot
    try:
        observed_identity = vault_relocation.root_identity(vault["source_root"])
    except vault_relocation.VaultRelocationError:
        snapshot["error"] = "filesystem_unreadable"
        return snapshot
    snapshot["identity"] = observed_identity
    if observed_identity != vault.get("root_identity"):
        snapshot["error"] = "root_identity_mismatch"
        return snapshot

    root = Path(str(vault["source_root"]))

    def _walk_error(error: OSError) -> None:
        raise error

    try:
        for directory, names, filenames in os.walk(
            root, topdown=True, followlinks=False, onerror=_walk_error
        ):
            directory_path = Path(directory)
            names.sort()
            filenames.sort()
            retained_names: list[str] = []
            for name in names:
                entry = directory_path / name
                relative = entry.relative_to(root).as_posix()
                info = entry.lstat()
                if stat.S_ISLNK(info.st_mode):
                    kind = "symlink"
                    snapshot["data_paths"].append(relative)
                    snapshot["unsupported_paths"].append(relative)
                    snapshot["file_count"] += 1
                    snapshot["byte_count"] += int(info.st_size)
                elif source_layout.path_is_mount(entry):
                    kind = "nested_mount"
                    snapshot["data_paths"].append(relative)
                    snapshot["unsupported_paths"].append(relative)
                    snapshot["file_count"] += 1
                elif stat.S_ISDIR(info.st_mode):
                    kind = "directory"
                    retained_names.append(name)
                else:
                    kind = "special"
                    snapshot["data_paths"].append(relative)
                    snapshot["unsupported_paths"].append(relative)
                    snapshot["file_count"] += 1
                    snapshot["byte_count"] += int(info.st_size)
                snapshot["entries"].append(
                    [relative, kind, int(info.st_size), int(info.st_mtime_ns), int(info.st_ino)]
                )
            names[:] = retained_names
            for name in filenames:
                entry = directory_path / name
                relative = entry.relative_to(root).as_posix()
                info = entry.lstat()
                snapshot["data_paths"].append(relative)
                snapshot["file_count"] += 1
                snapshot["byte_count"] += int(info.st_size)
                if stat.S_ISREG(info.st_mode):
                    kind = "regular"
                    snapshot["regular_paths"].append(relative)
                else:
                    kind = "symlink" if stat.S_ISLNK(info.st_mode) else "special"
                    snapshot["unsupported_paths"].append(relative)
                snapshot["entries"].append(
                    [relative, kind, int(info.st_size), int(info.st_mtime_ns), int(info.st_ino)]
                )
    except (OSError, ValueError):
        snapshot["error"] = "filesystem_unreadable"
        return snapshot
    snapshot["entries"].sort(key=lambda item: item[0])
    snapshot["data_paths"].sort()
    snapshot["regular_paths"].sort()
    snapshot["unsupported_paths"].sort()
    snapshot["healthy"] = True
    return snapshot


def _local_catalog_rows(connection: Any, vault_id: int) -> list[dict[str, Any]]:
    return connection.execute(
        """
        SELECT vf.id AS vault_file_id, vf.status, fp.path,
               lc.presence, lc.file_type, lc.size, lc.mtime_ns,
               lc.plaintext_sha256 AS local_sha256,
               lc.matched_archive_version_id,
               av.id AS archive_version_id,
               av.size AS cloud_size,
               av.plaintext_sha256 AS version_sha256,
               av.integrity, av.availability,
               av.object_key, av.provider_version_id
        FROM vault_files vf
        JOIN file_paths fp
          ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
        JOIN local_copies lc ON lc.vault_file_id=vf.id
        LEFT JOIN archive_versions av
          ON av.id=(
              SELECT latest.id FROM archive_versions latest
              WHERE latest.vault_file_id=vf.id
                AND latest.availability NOT IN ('missing', 'purged')
              ORDER BY latest.version_number DESC
              LIMIT 1
          )
        WHERE vf.vault_id=%s
          AND lc.presence IN ('present', 'unsupported')
        ORDER BY fp.path, vf.id
        """,
        (vault_id,),
    ).fetchall()


def _cloud_snapshot(connection: Any, vault_id: int) -> dict[str, Any]:
    versions = connection.execute(
        """
        SELECT id, vault_file_id, object_key, provider_version_id,
               size, integrity, availability
        FROM archive_versions
        WHERE vault_id=%s AND availability <> 'purged'
        ORDER BY id
        """,
        (vault_id,),
    ).fetchall()
    markers = connection.execute(
        """
        SELECT dm.id, dm.vault_file_id, dm.object_key, dm.provider_version_id
        FROM delete_markers dm
        WHERE vault_id=%s
          AND NOT EXISTS (
              SELECT 1 FROM cloud_deletion_items prior
              WHERE prior.delete_marker_id=dm.id AND prior.status='deleted'
          )
        ORDER BY dm.id
        """,
        (vault_id,),
    ).fetchall()
    invalid = sum(
        1
        for row in [*versions, *markers]
        if not row.get("object_key") or not row.get("provider_version_id")
    )
    return {
        "versions": [
            [
                row["id"],
                row["vault_file_id"],
                row.get("object_key"),
                row.get("provider_version_id"),
                int(row.get("size") or 0),
                row.get("integrity"),
                row.get("availability"),
            ]
            for row in versions
        ],
        "markers": [
            [
                row["id"],
                row["vault_file_id"],
                row.get("object_key"),
                row.get("provider_version_id"),
            ]
            for row in markers
        ],
        "version_count": len(versions),
        "delete_marker_count": len(markers),
        "byte_count": sum(int(row.get("size") or 0) for row in versions),
        "invalid_identity_count": invalid,
    }


def build_preview(
    connection: Any,
    *,
    vault_id: int,
    local_disposition: str,
    cloud_disposition: str,
    local_delete_enabled: bool,
    runtime_scan_active: bool = False,
) -> dict[str, Any]:
    """Build the authoritative disposition snapshot and confirmation fingerprint."""
    local_value, cloud_value = _validate_dispositions(
        local_disposition, cloud_disposition
    )
    vault = connection.execute(
        "SELECT * FROM vaults WHERE id=%s", (vault_id,)
    ).fetchone()
    if vault is None:
        raise VaultDecommissionError("not_found", "Vault not found")

    blockers: list[dict[str, Any]] = []
    if vault.get("root_released_at") or vault.get("decommission_state") == "decommissioned":
        _append_blocker(blockers, "already_decommissioned")
    elif vault.get("decommission_state") != "active":
        _append_blocker(blockers, "decommission_in_progress")
    if vault.get("relocation_state") != "ready":
        _append_blocker(blockers, "relocation_in_progress")
    if runtime_scan_active:
        _append_blocker(blockers, "scan_active")

    active_jobs = connection.execute(
        """
        SELECT action, status FROM jobs
        WHERE vault_id=%s AND status NOT IN ('completed', 'failed', 'cancelled')
        ORDER BY id
        """,
        (vault_id,),
    ).fetchall()
    if active_jobs:
        _append_blocker(blockers, "active_jobs", count=len(active_jobs))
    pending_destructive = sum(
        1
        for row in active_jobs
        if row["action"] in {"free-space", "cloud-archive", "cloud-purge"}
    )
    if pending_destructive:
        _append_blocker(
            blockers, "pending_destructive_actions", count=pending_destructive
        )

    owner = connection.execute(
        "SELECT user_id FROM vault_members WHERE vault_id=%s AND role='owner'",
        (vault_id,),
    ).fetchone()
    if owner is None:
        _append_blocker(blockers, "owner_missing")

    filesystem = _filesystem_snapshot(vault)
    if filesystem["error"]:
        _append_blocker(blockers, str(filesystem["error"]))

    local_rows = _local_catalog_rows(connection, vault_id)
    local_by_path = {str(row["path"]): row for row in local_rows}
    physical_paths = set(filesystem["data_paths"])
    physical_regular_paths = set(filesystem["regular_paths"])
    catalog_paths = {
        str(row["path"])
        for row in local_rows
        if row.get("presence") in {"present", "unsupported"}
    }
    if filesystem["healthy"] and physical_paths != catalog_paths:
        _append_blocker(blockers, "local_catalog_stale")
    physical_metadata = {
        str(entry[0]): entry for entry in filesystem["entries"] if entry[1] == "regular"
    }
    for path in sorted(physical_regular_paths & catalog_paths):
        row = local_by_path[path]
        entry = physical_metadata[path]
        if (
            row.get("file_type") != "regular"
            or int(row.get("size") or 0) != int(entry[2])
            or int(row.get("mtime_ns") or 0) != int(entry[3])
        ):
            _append_blocker(blockers, "local_catalog_stale")
            break

    unprotected = 0
    for path in sorted(physical_regular_paths):
        row = local_by_path.get(path)
        if (
            row is None
            or row.get("status") != "active"
            or row.get("presence") != "present"
            or row.get("file_type") != "regular"
            or row.get("archive_version_id") is None
            or row.get("integrity") != "verified"
            or row.get("availability") != "available"
            or row.get("matched_archive_version_id") != row.get("archive_version_id")
            or not row.get("local_sha256")
            or row.get("local_sha256") != row.get("version_sha256")
        ):
            unprotected += 1
    if local_value == "remove":
        if not local_delete_enabled:
            _append_blocker(blockers, "local_delete_disabled")
        if filesystem["unsupported_paths"]:
            _append_blocker(
                blockers,
                "local_unsupported_entries",
                count=len(filesystem["unsupported_paths"]),
            )
        if unprotected:
            _append_blocker(blockers, "local_copy_unprotected", count=unprotected)

    cloud = _cloud_snapshot(connection, vault_id)
    if cloud_value == "purge":
        if not bool(vault.get("cloud_deletion_enabled")):
            _append_blocker(blockers, "cloud_deletion_disabled")
        if cloud["invalid_identity_count"]:
            _append_blocker(
                blockers,
                "cloud_identity_incomplete",
                count=cloud["invalid_identity_count"],
            )
    elif (
        vault.get("encryption_mode") == "crypt"
        and not vault.get("recovery_custody_confirmed_at")
    ):
        _append_blocker(blockers, "recovery_custody_unconfirmed")

    file_count = int(
        connection.execute(
            "SELECT COUNT(*) AS total FROM vault_files WHERE vault_id=%s",
            (vault_id,),
        ).fetchone()["total"]
        or 0
    )
    membership_count = int(
        connection.execute(
            "SELECT COUNT(*) AS total FROM vault_members WHERE vault_id=%s",
            (vault_id,),
        ).fetchone()["total"]
        or 0
    )
    job_count = int(
        connection.execute(
            "SELECT COUNT(*) AS total FROM jobs WHERE vault_id=%s", (vault_id,)
        ).fetchone()["total"]
        or 0
    )

    counts = {
        "vault_files": file_count,
        "local_files": int(filesystem["file_count"]),
        "local_bytes": int(filesystem["byte_count"]),
        "archive_versions": int(cloud["version_count"]),
        "cloud_bytes": int(cloud["byte_count"]),
        "delete_markers": int(cloud["delete_marker_count"]),
        "jobs": job_count,
        "memberships": membership_count,
    }
    public = {
        "vault_id": int(vault["id"]),
        "vault_name": vault["name"],
        "enabled": bool(vault["enabled"]),
        "decommission_state": vault.get("decommission_state") or "active",
        "local_disposition": local_value,
        "cloud_disposition": cloud_value,
        "counts": counts,
        "blockers": blockers,
        "can_start": not blockers,
        "root_identity_version": vault.get("root_identity_version"),
        "root_identity_fingerprint": vault.get("root_identity"),
        "recovery_material": {
            "encryption_mode": vault.get("encryption_mode") or "plain",
            "custody_confirmed": bool(vault.get("recovery_custody_confirmed_at")),
            "disposition": "retained_encrypted_tombstone",
        },
        "records": {
            "vault_tombstone": "retained",
            "memberships": "retained_as_history",
            "jobs": "retained_as_history",
            "path_history": "retained",
            "audit_history": "retained",
        },
    }
    fingerprint_payload = {
        "vault": {
            "id": int(vault["id"]),
            "uuid": vault.get("uuid"),
            "name": vault["name"],
            "source_root": vault["source_root"],
            "s3_bucket": vault.get("s3_bucket"),
            "s3_prefix": vault.get("s3_prefix"),
            "enabled": bool(vault["enabled"]),
            "decommission_state": vault.get("decommission_state") or "active",
            "root_released_at": vault.get("root_released_at"),
            "relocation_state": vault.get("relocation_state"),
            "root_identity": vault.get("root_identity"),
            "recovery_custody_confirmed_at": vault.get(
                "recovery_custody_confirmed_at"
            ),
            "cloud_deletion_enabled": bool(vault.get("cloud_deletion_enabled")),
        },
        "dispositions": [local_value, cloud_value],
        "counts": counts,
        "blockers": [[item["code"], item.get("count")] for item in blockers],
        "filesystem": filesystem,
        "local_catalog": [
            [
                row.get("vault_file_id"),
                row.get("status"),
                row.get("path"),
                row.get("presence"),
                row.get("file_type"),
                row.get("size"),
                row.get("mtime_ns"),
                row.get("local_sha256"),
                row.get("matched_archive_version_id"),
                row.get("archive_version_id"),
                row.get("cloud_size"),
                row.get("version_sha256"),
                row.get("integrity"),
                row.get("availability"),
                row.get("object_key"),
                row.get("provider_version_id"),
            ]
            for row in local_rows
        ],
        "cloud": cloud,
        "active_jobs": [[row["action"], row["status"]] for row in active_jobs],
    }
    encoded = json.dumps(
        fingerprint_payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    public["fingerprint"] = hashlib.sha256(encoded).hexdigest()
    return public


def _notify_members(
    connection: Any,
    *,
    vault_id: int,
    event: str,
    title: str,
    body: str,
) -> None:
    rows = connection.execute(
        "SELECT user_id FROM vault_members WHERE vault_id=%s ORDER BY user_id",
        (vault_id,),
    ).fetchall()
    for row in rows:
        enqueue_notification(
            connection,
            user_id=int(row["user_id"]),
            vault_id=vault_id,
            event=event,
            title=title,
            body=body,
            channels=("in_app",),
        )


def _queue_local_removal(
    connection: Any,
    *,
    vault_id: int,
    actor_user_id: int,
    expected_count: int,
    requested_at: str,
) -> str | None:
    if expected_count == 0:
        return None
    group_id = str(uuid.uuid4())
    job_ids, _, eligible = ArchiveCatalog(connection).queue_jobs(
        vault_id=vault_id,
        path="",
        action="free-space",
        requested_by=actor_user_id,
        requested_at=requested_at,
        group_id=group_id,
        is_directory=False,
        whole_vault=True,
        origin="decommission",
    )
    if eligible != expected_count or len(job_ids) != expected_count:
        raise VaultDecommissionError(
            "stale_preview", "Local Copy eligibility changed; request a new preview"
        )
    return group_id


def start_decommission(
    connection: Any,
    *,
    vault_id: int,
    actor_user_id: int,
    actor_is_admin: bool,
    local_disposition: str,
    cloud_disposition: str,
    confirmation: str,
    reason: str,
    preview_fingerprint: str,
    local_delete_enabled: bool,
    purge_delay_seconds: int,
    runtime_scan_active: bool = False,
) -> dict[str, Any]:
    """Quiesce the Vault and persist the selected, fingerprinted dispositions."""
    del purge_delay_seconds  # consumed by reconciliation after local cleanup
    cleaned_reason = _clean_reason(reason)
    local_value, cloud_value = _validate_dispositions(
        local_disposition, cloud_disposition
    )
    _runtime_suspended_vaults.add(int(vault_id))
    succeeded = False
    try:
        source_areas._lock_source_area_mutations(connection)
        # PostgreSQL row lock / SQLite write lock: operation admission and root
        # release share the Source Area/adoption serialization boundary.
        connection.execute("UPDATE vaults SET name=name WHERE id=%s", (vault_id,))
        vault = connection.execute(
            "SELECT * FROM vaults WHERE id=%s", (vault_id,)
        ).fetchone()
        if vault is None:
            raise VaultDecommissionError("not_found", "Vault not found")
        if not actor_is_admin:
            owner = connection.execute(
                """
                SELECT 1 FROM vault_members
                WHERE vault_id=%s AND user_id=%s AND role='owner'
                """,
                (vault_id, actor_user_id),
            ).fetchone()
            if owner is None:
                raise VaultDecommissionError(
                    "owner_required", "Only the primary owner can decommission this Vault"
                )
        if confirmation != str(vault["name"]):
            raise VaultDecommissionError(
                "confirmation_required", "Type the exact Vault name to confirm"
            )
        existing = connection.execute(
            "SELECT * FROM vault_decommissions WHERE vault_id=%s", (vault_id,)
        ).fetchone()
        if existing is not None:
            same_request = (
                existing["local_disposition"] == local_value
                and existing["cloud_disposition"] == cloud_value
                and existing["reason"] == cleaned_reason
                and existing["preview_fingerprint"]
                == (preview_fingerprint or "").strip()
            )
            if same_request:
                # The durable operation is also the idempotency record when a
                # client retries after losing the original response.
                succeeded = True
                return operation_status(connection, vault_id=vault_id)
            if existing["state"] == "completed":
                raise VaultDecommissionError(
                    "already_decommissioned",
                    "Vault root was released by a different decommission request",
                )
            raise VaultDecommissionError(
                "decommission_in_progress", "Vault decommission is already in progress"
            )
        preview = build_preview(
            connection,
            vault_id=vault_id,
            local_disposition=local_value,
            cloud_disposition=cloud_value,
            local_delete_enabled=local_delete_enabled,
            runtime_scan_active=runtime_scan_active,
        )
        if preview["fingerprint"] != (preview_fingerprint or "").strip():
            raise VaultDecommissionError(
                "stale_preview", "Vault contents changed; request a new preview"
            )
        if preview["blockers"]:
            raise VaultDecommissionError(
                "blocked", "Vault decommission is blocked by the current preview"
            )

        stamp = now_iso()
        local_status = "retained" if local_value == "retain" else "pending"
        cloud_status = "retained" if cloud_value == "retain" else "pending"
        operation = connection.execute(
            """
            INSERT INTO vault_decommissions(
                vault_id, requested_by, requested_at, updated_at, state,
                local_disposition, cloud_disposition, local_status, cloud_status,
                reason, preview_fingerprint, preview_json
            ) VALUES (
                %s, %s, %s, %s, 'quiescing',
                %s, %s, %s, %s,
                %s, %s, %s
            )
            RETURNING *
            """,
            (
                vault_id,
                actor_user_id,
                stamp,
                stamp,
                local_value,
                cloud_value,
                local_status,
                cloud_status,
                cleaned_reason,
                preview["fingerprint"],
                json.dumps(preview, sort_keys=True, separators=(",", ":")),
            ),
        ).fetchone()
        updated = connection.execute(
            """
            UPDATE vaults
            SET decommission_state='decommissioning'
            WHERE id=%s AND decommission_state='active' AND root_released_at IS NULL
            RETURNING id
            """,
            (vault_id,),
        ).fetchone()
        if updated is None:
            raise VaultDecommissionError(
                "state_changed", "Vault lifecycle changed; request a new preview"
            )

        if local_value == "remove":
            group_id = _queue_local_removal(
                connection,
                vault_id=vault_id,
                actor_user_id=actor_user_id,
                expected_count=int(preview["counts"]["local_files"]),
                requested_at=stamp,
            )
            local_status = "removing" if group_id else "removed"
            connection.execute(
                """
                UPDATE vault_decommissions
                SET local_status=%s, local_job_group_id=%s,
                    state=%s, updated_at=%s
                WHERE id=%s
                """,
                (
                    local_status,
                    group_id,
                    "local_cleanup" if group_id else "quiescing",
                    stamp,
                    operation["id"],
                ),
            )

        record_audit_event(
            connection,
            event="vault_decommission.requested",
            actor_user_id=actor_user_id,
            vault_id=vault_id,
            outcome="requested",
            visibility="owner",
            reason=cleaned_reason,
            admin_override=bool(actor_is_admin),
            local_disposition=local_value,
            cloud_disposition=cloud_value,
            preview_fingerprint=preview["fingerprint"],
            counts=preview["counts"],
            root_release_pending=True,
        )
        _notify_members(
            connection,
            vault_id=vault_id,
            event="vault_decommission.requested",
            title="Vault decommission requested",
            body=(
                f"Local Copies: {local_value}; cloud history: {cloud_value}. "
                f"Reason: {cleaned_reason}"
            ),
        )
        succeeded = True
        return operation_status(connection, vault_id=vault_id)
    finally:
        if not succeeded:
            _runtime_suspended_vaults.discard(int(vault_id))


def _group_summary(connection: Any, vault_id: int, group_id: str | None) -> dict[str, int]:
    if not group_id:
        return {"total": 0, "completed": 0, "failed": 0, "cancelled": 0, "active": 0}
    rows = connection.execute(
        """
        SELECT status, COUNT(*) AS total FROM jobs
        WHERE vault_id=%s AND group_id=%s AND origin='decommission'
        GROUP BY status
        """,
        (vault_id, group_id),
    ).fetchall()
    counts = {str(row["status"]): int(row["total"] or 0) for row in rows}
    total = sum(counts.values())
    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0)
    cancelled = counts.get("cancelled", 0)
    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "cancelled": cancelled,
        "active": total - completed - failed - cancelled,
    }


def operation_status(connection: Any, *, vault_id: int) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT vd.*, v.name AS vault_name, v.enabled,
               v.decommission_state, v.decommissioned_at, v.root_released_at
        FROM vault_decommissions vd
        JOIN vaults v ON v.id=vd.vault_id
        WHERE vd.vault_id=%s
        """,
        (vault_id,),
    ).fetchone()
    if row is None:
        raise VaultDecommissionError("not_started", "Vault decommission has not started")
    local_jobs = _group_summary(connection, vault_id, row.get("local_job_group_id"))
    cloud_jobs = _group_summary(connection, vault_id, row.get("cloud_job_group_id"))
    cloud_cancellable = False
    if row.get("cloud_job_group_id") and row.get("cloud_status") == "purging":
        cancellable = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='pending_delay' THEN 1 ELSE 0 END) AS delayed
            FROM jobs
            WHERE vault_id=%s AND group_id=%s AND origin='decommission'
            """,
            (vault_id, row["cloud_job_group_id"]),
        ).fetchone()
        cloud_cancellable = bool(
            int(cancellable["total"] or 0) > 0
            and int(cancellable["total"] or 0) == int(cancellable["delayed"] or 0)
        )
    preview: dict[str, Any]
    try:
        preview = json.loads(row.get("preview_json") or "{}")
    except json.JSONDecodeError:
        preview = {}
    local_total = max(int(preview.get("counts", {}).get("local_files", 0)), local_jobs["total"])
    cloud_total = max(
        int(preview.get("counts", {}).get("archive_versions", 0))
        + int(preview.get("counts", {}).get("delete_markers", 0)),
        cloud_jobs["total"],
    )
    local_done = local_total if row["local_status"] in LOCAL_TERMINAL else local_jobs["completed"]
    cloud_done = cloud_total if row["cloud_status"] in CLOUD_TERMINAL else cloud_jobs["completed"]
    denominator = max(1, local_total + cloud_total + 1)
    released_step = 1 if row.get("root_released_at") else 0
    percent = round(100 * (local_done + cloud_done + released_step) / denominator)
    if row["state"] == "completed":
        percent = 100
    return {
        "id": int(row["id"]),
        "vault_id": int(row["vault_id"]),
        "vault_name": row["vault_name"],
        "state": row["state"],
        "decommission_state": row["decommission_state"],
        "enabled": bool(row["enabled"]),
        "local_disposition": row["local_disposition"],
        "cloud_disposition": row["cloud_disposition"],
        "local_status": row["local_status"],
        "cloud_status": row["cloud_status"],
        "requested_at": row["requested_at"],
        "updated_at": row["updated_at"],
        "completed_at": row.get("completed_at"),
        "decommissioned_at": row.get("decommissioned_at"),
        "root_released_at": row.get("root_released_at"),
        "root_released": bool(row.get("root_released_at")),
        "error_code": row.get("error_code"),
        "error_message": row.get("error_message"),
        "preview": preview,
        "jobs": {"local": local_jobs, "cloud": cloud_jobs},
        "cloud_cancellable": cloud_cancellable,
        "progress_percent": max(0, min(100, percent)),
    }


def cancel_pending_cloud_purge(
    connection: Any,
    *,
    vault_id: int,
    actor_user_id: int,
) -> dict[str, Any]:
    """Cancel only the pre-deletion delay; never claim deletion can be undone."""
    source_areas._lock_source_area_mutations(connection)
    operation = connection.execute(
        "SELECT * FROM vault_decommissions WHERE vault_id=%s", (vault_id,)
    ).fetchone()
    if operation is None or operation.get("cloud_status") != "purging":
        raise VaultDecommissionError(
            "cloud_purge_not_cancellable", "No delayed decommission purge is pending"
        )
    group_id = operation.get("cloud_job_group_id")
    statuses = connection.execute(
        """
        SELECT status FROM jobs
        WHERE vault_id=%s AND group_id=%s AND origin='decommission'
        """,
        (vault_id, group_id),
    ).fetchall()
    if not statuses or any(row["status"] != "pending_delay" for row in statuses):
        raise VaultDecommissionError(
            "cloud_purge_not_cancellable",
            "Permanent deletion has started or the delay is no longer cancellable",
        )
    cloud_deletion.cancel_cloud_deletion(
        connection,
        vault_id=vault_id,
        group_id=str(group_id),
        actor_user_id=actor_user_id,
        cancelled_at=now_iso(),
    )
    stamp = now_iso()
    connection.execute(
        """
        UPDATE vault_decommissions
        SET state='blocked', error_code='cloud_purge_cancelled',
            error_message='Permanent cloud purge was cancelled during its delay; root remains occupied',
            updated_at=%s
        WHERE id=%s
        """,
        (stamp, operation["id"]),
    )
    record_audit_event(
        connection,
        event="vault_decommission.cloud_purge_cancelled",
        actor_user_id=actor_user_id,
        vault_id=vault_id,
        outcome="cancelled",
        visibility="owner",
        job_group_id=group_id,
        root_released=False,
    )
    return operation_status(connection, vault_id=vault_id)


def _mark_blocked(
    connection: Any,
    *,
    operation: dict[str, Any],
    code: str,
    message: str,
) -> None:
    changed = operation.get("state") != "blocked" or operation.get("error_code") != code
    stamp = now_iso()
    connection.execute(
        """
        UPDATE vault_decommissions
        SET state='blocked', error_code=%s, error_message=%s, updated_at=%s
        WHERE id=%s AND state<>'completed'
        """,
        (code, message, stamp, operation["id"]),
    )
    if changed:
        record_audit_event(
            connection,
            event="vault_decommission.blocked",
            actor_user_id=operation.get("requested_by"),
            vault_id=int(operation["vault_id"]),
            outcome="blocked",
            visibility="owner",
            reason=operation.get("reason"),
            blocker=code,
        )
        _notify_members(
            connection,
            vault_id=int(operation["vault_id"]),
            event="vault_decommission.blocked",
            title="Vault decommission needs attention",
            body=message,
        )


def _prune_empty_directories(root: Path) -> None:
    """Remove only empty descendant directories; never remove the Vault root."""
    directories: list[Path] = []
    for directory, names, _ in os.walk(root, topdown=True, followlinks=False):
        base = Path(directory)
        retained: list[str] = []
        for name in names:
            entry = base / name
            try:
                if entry.is_symlink() or source_layout.path_is_mount(entry):
                    continue
            except OSError:
                continue
            retained.append(name)
            directories.append(entry)
        names[:] = retained
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


def _schedule_cloud_purge(
    connection: Any,
    *,
    operation: dict[str, Any],
    vault: dict[str, Any],
    purge_delay_seconds: int,
) -> str | None:
    cloud = _cloud_snapshot(connection, int(vault["id"]))
    if cloud["version_count"] == 0 and cloud["delete_marker_count"] == 0:
        return None
    scheduled = cloud_deletion.schedule_cloud_purge(
        connection,
        vault_id=int(vault["id"]),
        paths=[],
        is_directory=False,
        whole_vault=True,
        actor_user_id=int(operation["requested_by"]),
        requested_at=now_iso(),
        confirmation=str(vault["name"]),
        reason=str(operation["reason"]),
        generated_phrase=str(vault["name"]),
        delay_seconds=purge_delay_seconds,
        origin="decommission",
    )
    return scheduled.group_id


def reconcile_one(
    connection: Any,
    *,
    vault_id: int,
    local_delete_enabled: bool,
    purge_delay_seconds: int,
) -> dict[str, Any]:
    """Advance one persisted operation by idempotent, compare-before-write steps."""
    del local_delete_enabled  # admission was fingerprinted; workers re-check policy
    source_areas._lock_source_area_mutations(connection)
    connection.execute("UPDATE vaults SET name=name WHERE id=%s", (vault_id,))
    operation = connection.execute(
        "SELECT * FROM vault_decommissions WHERE vault_id=%s", (vault_id,)
    ).fetchone()
    vault = connection.execute(
        "SELECT * FROM vaults WHERE id=%s", (vault_id,)
    ).fetchone()
    if operation is None or vault is None:
        raise VaultDecommissionError("not_started", "Vault decommission has not started")
    if operation["state"] == "completed":
        _runtime_suspended_vaults.discard(int(vault_id))
        return operation_status(connection, vault_id=vault_id)
    if vault.get("decommission_state") != "decommissioning":
        _mark_blocked(
            connection,
            operation=operation,
            code="state_changed",
            message="Vault lifecycle changed before root release",
        )
        return operation_status(connection, vault_id=vault_id)

    # Local removal uses ordinary free-space Jobs, including their exact cloud
    # version HEAD check and local digest/rename salvage protections.
    if operation["local_status"] == "removing":
        jobs = _group_summary(connection, vault_id, operation.get("local_job_group_id"))
        if jobs["failed"] or jobs["cancelled"]:
            _mark_blocked(
                connection,
                operation=operation,
                code="local_cleanup_failed",
                message="One or more Local Copy removal Jobs did not complete",
            )
            return operation_status(connection, vault_id=vault_id)
        if jobs["active"]:
            if operation["state"] != "local_cleanup":
                connection.execute(
                    "UPDATE vault_decommissions SET state='local_cleanup', updated_at=%s WHERE id=%s",
                    (now_iso(), operation["id"]),
                )
            return operation_status(connection, vault_id=vault_id)
        if jobs["total"] == 0:
            _mark_blocked(
                connection,
                operation=operation,
                code="local_cleanup_missing",
                message="Local Copy removal Jobs are missing",
            )
            return operation_status(connection, vault_id=vault_id)
        snapshot = _filesystem_snapshot(vault)
        if not snapshot["healthy"]:
            _mark_blocked(
                connection,
                operation=operation,
                code=str(snapshot["error"] or "source_unavailable"),
                message="Vault root cannot be verified after Local Copy removal",
            )
            return operation_status(connection, vault_id=vault_id)
        _prune_empty_directories(Path(str(vault["source_root"])))
        snapshot = _filesystem_snapshot(vault)
        if snapshot["file_count"] or snapshot["unsupported_paths"]:
            _mark_blocked(
                connection,
                operation=operation,
                code="local_data_remains",
                message="Local data remains; the root was not released",
            )
            return operation_status(connection, vault_id=vault_id)
        stamp = now_iso()
        connection.execute(
            """
            UPDATE vault_decommissions
            SET local_status='removed', state='quiescing',
                error_code=NULL, error_message=NULL, updated_at=%s
            WHERE id=%s AND local_status='removing'
            """,
            (stamp, operation["id"]),
        )
        record_audit_event(
            connection,
            event="vault_decommission.local_removed",
            actor_user_id=operation.get("requested_by"),
            vault_id=vault_id,
            outcome="success",
            visibility="owner",
            job_group_id=operation.get("local_job_group_id"),
        )
        operation = connection.execute(
            "SELECT * FROM vault_decommissions WHERE id=%s", (operation["id"],)
        ).fetchone()

    # Cloud purge begins only after local removal has reached its terminal state,
    # so Local Copies can still rely on the existing verified free-space guard.
    if operation["local_status"] in LOCAL_TERMINAL and operation["cloud_status"] == "pending":
        try:
            group_id = _schedule_cloud_purge(
                connection,
                operation=operation,
                vault=vault,
                purge_delay_seconds=purge_delay_seconds,
            )
        except (cloud_deletion.CloudDeletionError, ValueError) as exc:
            _mark_blocked(
                connection,
                operation=operation,
                code="cloud_purge_admission_failed",
                message=str(exc),
            )
            return operation_status(connection, vault_id=vault_id)
        stamp = now_iso()
        if group_id is None:
            connection.execute(
                """
                UPDATE vault_decommissions
                SET cloud_status='purged', state='quiescing',
                    error_code=NULL, error_message=NULL, updated_at=%s
                WHERE id=%s AND cloud_status='pending'
                """,
                (stamp, operation["id"]),
            )
        else:
            connection.execute(
                """
                UPDATE vault_decommissions
                SET cloud_status='purging', cloud_job_group_id=%s,
                    state='cloud_purge', error_code=NULL, error_message=NULL,
                    updated_at=%s
                WHERE id=%s AND cloud_status='pending'
                """,
                (group_id, stamp, operation["id"]),
            )
        operation = connection.execute(
            "SELECT * FROM vault_decommissions WHERE id=%s", (operation["id"],)
        ).fetchone()

    if operation["cloud_status"] == "purging":
        jobs = _group_summary(connection, vault_id, operation.get("cloud_job_group_id"))
        if jobs["failed"] or jobs["cancelled"]:
            _mark_blocked(
                connection,
                operation=operation,
                code="cloud_purge_failed",
                message="One or more permanent cloud purge Jobs did not complete",
            )
            return operation_status(connection, vault_id=vault_id)
        if jobs["active"]:
            return operation_status(connection, vault_id=vault_id)
        if jobs["total"] == 0:
            _mark_blocked(
                connection,
                operation=operation,
                code="cloud_purge_missing",
                message="Permanent cloud purge Jobs are missing",
            )
            return operation_status(connection, vault_id=vault_id)
        remaining = int(
            connection.execute(
                """
                SELECT COUNT(*) AS total FROM archive_versions
                WHERE vault_id=%s AND availability <> 'purged'
                """,
                (vault_id,),
            ).fetchone()["total"]
            or 0
        )
        unfinished_items = int(
            connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM cloud_deletion_items item
                JOIN jobs j ON j.id=item.job_id
                WHERE j.vault_id=%s AND j.group_id=%s
                  AND item.status <> 'deleted'
                """,
                (vault_id, operation.get("cloud_job_group_id")),
            ).fetchone()["total"]
            or 0
        )
        if remaining or unfinished_items:
            _mark_blocked(
                connection,
                operation=operation,
                code="cloud_purge_unverified",
                message="Cloud purge terminal state could not be verified",
            )
            return operation_status(connection, vault_id=vault_id)
        stamp = now_iso()
        connection.execute(
            """
            UPDATE vault_decommissions
            SET cloud_status='purged', state='quiescing',
                error_code=NULL, error_message=NULL, updated_at=%s
            WHERE id=%s AND cloud_status='purging'
            """,
            (stamp, operation["id"]),
        )
        record_audit_event(
            connection,
            event="vault_decommission.cloud_purged",
            actor_user_id=operation.get("requested_by"),
            vault_id=vault_id,
            outcome="success",
            visibility="owner",
            job_group_id=operation.get("cloud_job_group_id"),
        )
        operation = connection.execute(
            "SELECT * FROM vault_decommissions WHERE id=%s", (operation["id"],)
        ).fetchone()

    if (
        operation["local_status"] in LOCAL_TERMINAL
        and operation["cloud_status"] in CLOUD_TERMINAL
    ):
        # Re-prove the enrolled root immediately before the only statement that
        # releases occupancy.  For remove, also prove that no local data remains.
        snapshot = _filesystem_snapshot(vault)
        if not snapshot["healthy"]:
            _mark_blocked(
                connection,
                operation=operation,
                code=str(snapshot["error"] or "source_unavailable"),
                message="Vault root health could not be verified for release",
            )
            return operation_status(connection, vault_id=vault_id)
        if operation["local_disposition"] == "remove" and (
            snapshot["file_count"] or snapshot["unsupported_paths"]
        ):
            _mark_blocked(
                connection,
                operation=operation,
                code="local_data_remains",
                message="Local data remains; the root was not released",
            )
            return operation_status(connection, vault_id=vault_id)
        stamp = now_iso()
        connection.execute(
            "UPDATE vault_decommissions SET state='finalizing', updated_at=%s WHERE id=%s",
            (stamp, operation["id"]),
        )
        released = connection.execute(
            """
            UPDATE vaults
            SET decommission_state='decommissioned',
                decommissioned_at=%s,
                root_released_at=%s
            WHERE id=%s AND decommission_state='decommissioning'
              AND root_released_at IS NULL
            RETURNING id
            """,
            (stamp, stamp, vault_id),
        ).fetchone()
        if released is None:
            _mark_blocked(
                connection,
                operation=operation,
                code="root_release_conflict",
                message="Root occupancy changed before terminal release",
            )
            return operation_status(connection, vault_id=vault_id)
        connection.execute(
            """
            UPDATE vault_decommissions
            SET state='completed', completed_at=%s, updated_at=%s,
                error_code=NULL, error_message=NULL
            WHERE id=%s AND state='finalizing'
            """,
            (stamp, stamp, operation["id"]),
        )
        record_audit_event(
            connection,
            event="vault_decommission.completed",
            actor_user_id=operation.get("requested_by"),
            vault_id=vault_id,
            outcome="success",
            visibility="owner",
            reason=operation.get("reason"),
            local_disposition=operation["local_disposition"],
            cloud_disposition=operation["cloud_disposition"],
            root_released_at=stamp,
            tombstone_retained=True,
        )
        _notify_members(
            connection,
            vault_id=vault_id,
            event="vault_decommission.completed",
            title="Vault decommission completed",
            body=(
                f"The local root was released. Local Copies: "
                f"{operation['local_disposition']}; cloud history: "
                f"{operation['cloud_disposition']}."
            ),
        )
    return operation_status(connection, vault_id=vault_id)


def reconcile_all(
    *,
    local_delete_enabled: bool,
    purge_delay_seconds: int,
) -> dict[str, int]:
    """Advance every nonterminal operation; safe to call after every restart/poll."""
    with db() as connection:
        rows = connection.execute(
            """
            SELECT vault_id FROM vault_decommissions
            WHERE state <> 'completed'
            ORDER BY id
            """
        ).fetchall()
    summary = {"processed": 0, "completed": 0, "blocked": 0}
    for row in rows:
        vault_id = int(row["vault_id"])
        _runtime_suspended_vaults.add(vault_id)
        try:
            with db() as connection:
                status = reconcile_one(
                    connection,
                    vault_id=vault_id,
                    local_delete_enabled=local_delete_enabled,
                    purge_delay_seconds=purge_delay_seconds,
                )
            summary["processed"] += 1
            if status["state"] == "completed":
                summary["completed"] += 1
                _runtime_suspended_vaults.discard(vault_id)
            elif status["state"] == "blocked":
                summary["blocked"] += 1
        except Exception as exc:
            with db() as connection:
                operation = connection.execute(
                    "SELECT * FROM vault_decommissions WHERE vault_id=%s",
                    (vault_id,),
                ).fetchone()
                if operation and operation["state"] != "completed":
                    _mark_blocked(
                        connection,
                        operation=operation,
                        code="reconciliation_failed",
                        message=f"Decommission reconciliation failed: {exc}",
                    )
            summary["blocked"] += 1
    return summary


def reconcile_interrupted_jobs(connection: Any) -> int:
    """Requeue only resumable decommission worker states after process restart."""
    stamp = now_iso()
    rows = connection.execute(
        """
        UPDATE jobs
        SET status='queued', updated_at=%s,
            message='Decommission work interrupted by restart; safely resumed'
        WHERE origin='decommission'
          AND status IN ('uploading', 'verifying', 'cleaning', 'retrying')
        RETURNING id
        """,
        (stamp,),
    ).fetchall()
    return len(rows)


def local_work_suspended(vault: dict[str, Any]) -> bool:
    vault_id = vault.get("id", vault.get("vault_id"))
    return bool(
        (vault_id is not None and int(vault_id) in _runtime_suspended_vaults)
        or str(vault.get("decommission_state") or "active") != "active"
    )


def release_runtime_gate(vault_id: int) -> None:
    _runtime_suspended_vaults.discard(int(vault_id))
