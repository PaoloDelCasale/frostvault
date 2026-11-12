"""One fixed, cancellable seven-day cloud deletion workflow (#428).

Admission captures exact VersionIds once. The worker never expands that consent.
Protection labels are not deletion authority: source identity, actual absence,
immutable targets, catalog state and concurrent work are checked independently.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any

from . import source_layout, vault_relocation
from .audit_events import record_audit_event
from .component_health import load_vault_health
from .directory_aggregates import invalidate_for_archive_version_ids, invalidate_for_vault_file
from .protection_states import observation_is_unconfirmed

DELETION_DELAY_SECONDS = 7 * 24 * 60 * 60
TERMINAL = ("completed", "failed", "cancelled")


class CloudDeletionError(ValueError):
    """The request no longer proves safe, deliberate deletion authority."""


class ConfirmationRequired(CloudDeletionError):
    pass


@dataclass(frozen=True)
class DeletionPreview:
    object_count: int
    version_count: int
    delete_marker_count: int
    byte_count: int

    def as_dict(self) -> dict[str, int]:
        return dict(object_count=self.object_count, version_count=self.version_count,
                    delete_marker_count=self.delete_marker_count, byte_count=self.byte_count)


@dataclass(frozen=True)
class ScheduledDeletion:
    group_id: str
    job_ids: list[int]
    preview: DeletionPreview
    pending_until: str


@dataclass(frozen=True)
class CancelledDeletion:
    cancelled_count: int
    group_id: str


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def lock_vault(connection: Any, vault_id: int) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM vaults WHERE id=%s FOR UPDATE", (vault_id,)
    ).fetchone()
    if row is None:
        raise CloudDeletionError("Vault not found")
    return row


def require_local_absence(vault: dict[str, Any], paths: list[str]) -> None:
    """Prove absence through an enrolled root, without following symlinks.

    ENOENT below a verified existing parent proves absence. Permission errors,
    replaced roots, missing mounts, symlinks and nested mounts are not absence.
    No local file (even an unsupported entry) can authorize a cloud deletion.
    """
    access = source_layout.vault_local_access(vault["source_root"])
    if not access.local_operations_allowed or vault.get("relocation_state", "ready") != "ready":
        raise CloudDeletionError("Local absence cannot be confirmed: reconnect the Source Volume")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptors: list[int] = []
    try:
        root_path = PurePosixPath(vault["source_root"])
        if not root_path.is_absolute():
            raise CloudDeletionError("Vault root is not absolute")
        root = os.open("/", flags)
        descriptors.append(root)
        for part in root_path.parts[1:]:
            root = os.open(part, flags, dir_fd=root)
            descriptors.append(root)
        if (vault.get("root_identity_version") != vault_relocation.ROOT_IDENTITY_VERSION
                or not vault.get("root_identity")
                or vault_relocation.opened_root_identity(root) != vault["root_identity"]):
            raise CloudDeletionError("Vault root identity changed")
        for path in paths:
            relative = PurePosixPath(path)
            if not path or relative.is_absolute() or ".." in relative.parts or str(relative) != path:
                raise CloudDeletionError("Invalid file path")
            parent = root
            children: list[int] = []
            try:
                for part in relative.parts[:-1]:
                    try:
                        child = os.open(part, flags, dir_fd=parent)
                    except FileNotFoundError:
                        break
                    children.append(child)
                    if (os.fstat(child).st_dev != os.fstat(root).st_dev
                            or source_layout.path_is_mount(f"/proc/self/fd/{child}")):
                        raise CloudDeletionError("Nested mount prevents confirming absence")
                    parent = child
                else:
                    try:
                        os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    raise CloudDeletionError("A selected file is present on the server")
            finally:
                for child in reversed(children):
                    os.close(child)
        # Do not accept an unlinked/replaced open directory as the configured root.
        if vault_relocation.root_identity(vault["source_root"]) != vault["root_identity"]:
            raise CloudDeletionError("Vault root changed during absence check")
        if not source_layout.vault_local_access(vault["source_root"]).local_operations_allowed:
            raise CloudDeletionError("Source Volume changed during absence check")
    except (OSError, vault_relocation.VaultRelocationError) as exc:
        raise CloudDeletionError("Local absence cannot be confirmed") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _selected_files(connection: Any, *, vault_id: int, paths: list[str],
                    is_directory: bool, whole_vault: bool = False) -> list[dict[str, Any]]:
    params: list[Any] = [vault_id]
    if whole_vault:
        selection = "av.id IS NOT NULL"
    else:
        if not paths or any(not p or PurePosixPath(p).is_absolute() or ".." in PurePosixPath(p).parts
                            or str(PurePosixPath(p)) != p for p in paths):
            raise CloudDeletionError("Select valid relative file paths")
        clauses = []
        for path in paths:
            if is_directory:
                escaped = path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                clauses.append("(fp.path=%s OR fp.path LIKE %s ESCAPE '\\')")
                params.extend((path, escaped + "/%"))
            else:
                clauses.append("fp.path=%s")
                params.append(path)
        selection = "(" + " OR ".join(clauses) + ")"
    return connection.execute(f"""
        SELECT vf.id AS vault_file_id, fp.path, lc.presence,
               lc.cloud_only_version_id, av.id AS archive_version_id,
               av.integrity, av.availability
        FROM vault_files vf
        LEFT JOIN file_paths fp ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
        LEFT JOIN local_copies lc ON lc.vault_file_id=vf.id
        LEFT JOIN archive_versions av ON av.id=(
            SELECT latest.id FROM archive_versions latest
            WHERE latest.vault_file_id=vf.id AND latest.availability<>'purged'
            ORDER BY latest.version_number DESC LIMIT 1)
        WHERE vf.vault_id=%s AND {selection}
        ORDER BY fp.path, vf.id
    """, params).fetchall()


def _versions(connection: Any, file_id: str) -> list[dict[str, Any]]:
    return connection.execute("""
        SELECT * FROM archive_versions WHERE vault_file_id=%s AND availability<>'purged'
        ORDER BY version_number
    """, (file_id,)).fetchall()


def _markers(connection: Any, file_id: str) -> list[dict[str, Any]]:
    return connection.execute("""
        SELECT dm.* FROM delete_markers dm WHERE vault_file_id=%s AND NOT EXISTS (
            SELECT 1 FROM cloud_deletion_items prior
            WHERE prior.delete_marker_id=dm.id AND prior.status='deleted') ORDER BY dm.id
    """, (file_id,)).fetchall()


def _validate_files(connection: Any, vault: dict[str, Any], files: list[dict[str, Any]],
                    *, own_group: str | None = None) -> None:
    if observation_is_unconfirmed(load_vault_health(connection, int(vault["id"]))):
        raise CloudDeletionError("Protection observations are not confirmed")
    for row in files:
        if (row["presence"] not in {None, "missing"} or row["cloud_only_version_id"] is not None
                or row["integrity"] != "verified" or row["availability"] != "available"
                or not row["path"]):
            raise CloudDeletionError("Only Cloud-only files can be deleted from the cloud")
        pending = connection.execute("""
            SELECT 1 FROM jobs WHERE vault_file_id=%s
              AND status NOT IN ('completed','failed','cancelled')
              AND (CAST(%s AS TEXT) IS NULL OR group_id IS DISTINCT FROM %s) LIMIT 1
        """, (row["vault_file_id"], own_group, own_group)).fetchone()
        if pending:
            raise CloudDeletionError("Selected file has pending work")
        for item in [*_versions(connection, row["vault_file_id"]), *_markers(connection, row["vault_file_id"])]:
            if (not item["object_key"] or not item["provider_version_id"]
                    or item["provider_version_id"] == "null"
                    or not str(item["object_key"]).startswith(str(vault["s3_prefix"]).rstrip("/") + "/")):
                raise CloudDeletionError("An exact cloud VersionId in this Vault is required")
    require_local_absence(vault, [r["path"] for r in files])


def preview_selection(connection: Any, *, vault_id: int, paths: list[str],
                      is_directory: bool = False, whole_vault: bool = False) -> DeletionPreview:
    vault = lock_vault(connection, vault_id)
    files = _selected_files(connection, vault_id=vault_id, paths=paths,
                            is_directory=is_directory, whole_vault=whole_vault)
    if not files:
        raise CloudDeletionError("No Cloud-only files selected")
    _validate_files(connection, vault, files)
    versions = [v for f in files for v in _versions(connection, f["vault_file_id"])]
    return DeletionPreview(len(files), len(versions),
                           sum(len(_markers(connection, f["vault_file_id"])) for f in files),
                           sum(int(v["size"] or 0) for v in versions))


def schedule_cloud_purge(connection: Any, *, vault_id: int, paths: list[str],
                         is_directory: bool, actor_user_id: int, requested_at: str,
                         confirmation: str, reason: str,
                         whole_vault: bool = False, origin: str = "manual") -> ScheduledDeletion:
    vault = lock_vault(connection, vault_id)
    if vault["decommission_state"] != "active" or vault.get("root_released_at"):
        raise CloudDeletionError("Folder protection has already stopped")
    if origin not in {"manual", "stop-protection"}:
        raise CloudDeletionError("Unsupported deletion origin")
    if confirmation != vault["name"]:
        raise ConfirmationRequired("Type the exact Vault name to confirm")
    if not (reason or "").strip() or len(reason) > 500:
        raise CloudDeletionError("A deletion reason is required")
    preview = preview_selection(connection, vault_id=vault_id, paths=paths,
                                is_directory=is_directory, whole_vault=whole_vault)
    files = _selected_files(connection, vault_id=vault_id, paths=paths,
                            is_directory=is_directory, whole_vault=whole_vault)
    requested = datetime.fromisoformat(requested_at)
    if requested.tzinfo is None:
        raise CloudDeletionError("A timezone-aware request time is required")
    pending_until = (requested + timedelta(seconds=DELETION_DELAY_SECONDS)).isoformat()
    group_id = str(uuid.uuid4())
    job_ids = []
    for row in files:
        job_id = int(connection.execute("""
            INSERT INTO jobs(vault_id,vault_file_id,path,action,status,
                             requested_at,updated_at,group_id,group_path,pending_until,
                             reason,confirmation_phrase,origin,total_bytes,transferred_bytes)
            VALUES (%s,%s,%s,'cloud-purge','pending_delay',%s,%s,%s,%s,%s,%s,%s,%s,0,0)
            RETURNING id
        """, (vault_id, row["vault_file_id"], row["path"], requested_at,
              requested_at, group_id, "" if whole_vault else paths[0], pending_until,
              reason.strip(), confirmation, origin)).fetchone()["id"])
        job_ids.append(job_id)
        total = 0
        for kind, versions in (("version", _versions(connection, row["vault_file_id"])),
                               ("delete_marker", _markers(connection, row["vault_file_id"]))):
            for version in versions:
                size = int(version.get("size") or 0)
                total += size
                connection.execute("""
                    INSERT INTO cloud_deletion_items(job_id,vault_id,vault_file_id,kind,
                        archive_version_id,delete_marker_id,object_key,provider_version_id,
                        size_bytes,status,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s)
                """, (job_id, vault_id, row["vault_file_id"], kind,
                      version["id"] if kind == "version" else None,
                      version["id"] if kind == "delete_marker" else None,
                      version["object_key"], version["provider_version_id"], size, requested_at))
        connection.execute("UPDATE jobs SET total_bytes=%s WHERE id=%s", (total, job_id))
        record_audit_event(connection, event="cloud_deletion.requested", actor=("owner" if actor_user_id is not None else "system"),
                           vault_id=vault_id, job_id=job_id, outcome="requested",
                           path=row["path"], reason=reason.strip(), group_id=group_id,
                           pending_until=pending_until, preview=preview.as_dict())
    return ScheduledDeletion(group_id, job_ids, preview, pending_until)


def cancel_cloud_deletion(connection: Any, *, vault_id: int, group_id: str,
                          actor_user_id: int | None, cancelled_at: str) -> CancelledDeletion:
    lock_vault(connection, vault_id)
    rows = connection.execute("""
        SELECT * FROM jobs WHERE vault_id=%s AND group_id=%s AND action='cloud-purge'
        ORDER BY requested_at,id FOR UPDATE
    """, (vault_id, group_id)).fetchall()
    if not rows or any(r["status"] != "pending_delay" or r["claim_token"] for r in rows):
        raise CloudDeletionError("Deletion has started or is no longer cancellable")
    _cancel_rows(connection, rows, at=cancelled_at, actor=actor_user_id,
                 reason="Cancelled before deletion started")
    return CancelledDeletion(len(rows), group_id)


def _cancel_rows(connection: Any, rows: list[dict[str, Any]], *, at: str,
                 actor: int | None, reason: str) -> None:
    for row in rows:
        connection.execute("""UPDATE jobs SET status='cancelled', message=%s, updated_at=%s,
            claim_token=NULL,claimed_at=NULL,claim_expires_at=NULL WHERE id=%s""", (reason, at, row["id"]))
        connection.execute("""UPDATE cloud_deletion_items SET status='skipped',updated_at=%s
            WHERE job_id=%s AND status IN ('pending','failed')""", (at, row["id"]))
        record_audit_event(connection, event="cloud_deletion.cancelled", actor=("owner" if actor is not None else "system"),
                           vault_id=int(row["vault_id"]), job_id=int(row["id"]), outcome="cancelled",
                           group_id=row["group_id"], reason=reason)


def validate_execution(connection: Any, *, vault_id: int, group_id: str, at: str) -> bool:
    """Under the Vault lock, revoke stale consent before *any* S3 delete call.

    Exact targets already deleted by an interrupted request remain evidence;
    retries never widen consent to newly discovered/uploaded cloud versions.
    """
    vault = lock_vault(connection, vault_id)
    rows = connection.execute("""SELECT * FROM jobs WHERE vault_id=%s AND group_id=%s
        AND action='cloud-purge' ORDER BY requested_at,id FOR UPDATE""", (vault_id, group_id)).fetchall()
    active = [r for r in rows if r["status"] not in TERMINAL]
    try:
        if not active or vault.get("root_released_at"):
            raise CloudDeletionError("Deletion authority is no longer active")
        if vault["decommission_state"] == "active":
            if not vault["enabled"] or any(r["origin"] != "manual" for r in active):
                raise CloudDeletionError("Deletion authority is no longer active")
        else:
            stop = connection.execute("""SELECT group_id FROM protection_stops
                WHERE vault_id=%s AND cloud_disposition='delete'""", (vault_id,)).fetchone()
            if (vault["decommission_state"] != "decommissioning" or not stop
                    or stop["group_id"] != group_id or any(r["origin"] != "stop-protection" for r in active)):
                raise CloudDeletionError("Stopped folder deletion authority changed")
        for row in active:
            earliest = datetime.fromisoformat(row["requested_at"]) + timedelta(seconds=DELETION_DELAY_SECONDS)
            if (not row["pending_until"] or datetime.fromisoformat(at) < earliest
                    or datetime.fromisoformat(at) < datetime.fromisoformat(row["pending_until"])):
                raise CloudDeletionError("The seven-day delay has not elapsed")
        files = _selected_files(connection, vault_id=vault_id, paths=[r["path"] for r in active], is_directory=False)
        if {f["vault_file_id"] for f in files} != {r["vault_file_id"] for r in active}:
            raise CloudDeletionError("Selected file identity changed")
        # Restart reconciliation may already have confirmed every target gone.
        # Such a Job still finalizes, but cannot authorize deleting new content.
        require_local_absence(vault, [f["path"] for f in files])
        _validate_files(connection, vault, [f for f in files if f["archive_version_id"]], own_group=group_id)
        for row in active:
            items = connection.execute("SELECT * FROM cloud_deletion_items WHERE job_id=%s", (row["id"],)).fetchall()
            authorized = {(i["object_key"], i["provider_version_id"]) for i in items}
            current = [*_versions(connection, row["vault_file_id"]), *_markers(connection, row["vault_file_id"])]
            if not current and any(i["status"] != "deleted" for i in items):
                raise CloudDeletionError("Cloud absence has not been reconciled for every exact target")
            if not items or any((i["object_key"], i["provider_version_id"]) not in authorized for i in current):
                raise CloudDeletionError("Cloud content changed after confirmation")
            for item in items:
                if not item["provider_version_id"] or item["provider_version_id"] == "null":
                    raise CloudDeletionError("Exact cloud version identity is missing")
                if item["kind"] == "version":
                    version = connection.execute("SELECT * FROM archive_versions WHERE id=%s", (item["archive_version_id"],)).fetchone()
                else:
                    version = connection.execute("SELECT * FROM delete_markers WHERE id=%s", (item["delete_marker_id"],)).fetchone()
                if (not version or version["vault_file_id"] != row["vault_file_id"]
                        or (version["object_key"], version["provider_version_id"]) != (item["object_key"], item["provider_version_id"])):
                    raise CloudDeletionError("Cloud target identity changed")
    except CloudDeletionError as exc:
        _cancel_rows(connection, active, at=at, actor=None, reason=str(exc))
        return False
    return True


def mark_item_deleted(connection: Any, *, item_id: int, updated_at: str) -> None:
    mark_items_deleted(connection, item_ids=[item_id], updated_at=updated_at)


def mark_items_deleted(connection: Any, *, item_ids: list[int], updated_at: str) -> None:
    """Persist provider batches in bounded queries, retaining the existing scale seam."""
    for offset in range(0, len(item_ids), 500):
        batch = item_ids[offset:offset + 500]
        placeholders = ','.join('%s' for _ in batch)
        items = connection.execute(f"""SELECT archive_version_id,vault_id,vault_file_id
            FROM cloud_deletion_items WHERE id IN ({placeholders})""", batch).fetchall()
        connection.execute(f"""UPDATE archive_versions SET availability='purged',availability_checked_at=%s
            WHERE id IN (SELECT archive_version_id FROM cloud_deletion_items
                         WHERE id IN ({placeholders}) AND kind='version')""", (updated_at, *batch))
        connection.execute(f"""UPDATE cloud_deletion_items SET status='deleted',error_message=NULL,updated_at=%s
            WHERE id IN ({placeholders})""", (updated_at, *batch))
        invalidate_for_archive_version_ids(connection, [str(i["archive_version_id"])
                                                       for i in items if i["archive_version_id"]])
        for vault_id, file_id in {(int(i["vault_id"]), str(i["vault_file_id"]))
                                  for i in items if not i["archive_version_id"]}:
            invalidate_for_vault_file(connection, vault_id, file_id)


def mark_items_failed(connection: Any, *, failures: list[tuple[int, str]], updated_at: str) -> None:
    for offset in range(0, len(failures), 250):
        batch = failures[offset:offset + 250]
        cases = ' '.join('WHEN %s THEN %s' for _ in batch)
        placeholders = ','.join('%s' for _ in batch)
        case_params = [value for pair in batch for value in pair]
        connection.execute(f"""UPDATE cloud_deletion_items SET status='failed',
            error_message=CASE id {cases} ELSE error_message END,updated_at=%s
            WHERE id IN ({placeholders})""", (*case_params, updated_at, *(i for i, _ in batch)))


def finalize_purge_job(connection: Any, *, job_id: int, vault_file_id: str,
                       updated_at: str) -> str:
    items = connection.execute("SELECT status FROM cloud_deletion_items WHERE job_id=%s", (job_id,)).fetchall()
    completed = bool(items) and all(i["status"] == "deleted" for i in items)
    status = "completed" if completed else "failed"
    job = connection.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
    connection.execute("""UPDATE jobs SET status=%s,message=%s,updated_at=%s,
        transferred_bytes=CASE WHEN %s THEN total_bytes ELSE transferred_bytes END WHERE id=%s""",
                       (status, "Cloud deletion completed" if completed else "Cloud deletion partially failed",
                        updated_at, completed, job_id))
    if completed and not _versions(connection, vault_file_id):
        connection.execute("UPDATE vault_files SET status='purged',retired_at=COALESCE(retired_at,%s) WHERE id=%s",
                           (updated_at, vault_file_id))
        invalidate_for_vault_file(connection, int(job["vault_id"]), vault_file_id)
    from ..owner import job_actor
    record_audit_event(connection, event="cloud_deletion.completed" if completed else "cloud_deletion.partial_failure",
                       actor=job_actor(job), vault_id=int(job["vault_id"]), job_id=job_id,
                       outcome="success" if completed else "partial_failure",
                       deleted=sum(i["status"] == "deleted" for i in items))
    return status
