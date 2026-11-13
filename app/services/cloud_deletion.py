"""Delayed Cloud Deletion: one fixed, cancellable seven-day workflow (#428, #594).

The Owner's consent is a Job group (``job_groups`` + ``cloud_purge_requests``)
with one ``cloud-purge`` Job per Cloud-only file, pinned to the file's exact
Current (``jobs.archive_version_id``) and runnable from ``run_after``. Nothing
else is ever covered: no superseded version, no Delete Marker, no object the
application did not write.

When the delay has elapsed and the scheduler claims the whole group, the
worker revalidates everything under the Vault lock, freezes one cleanup batch
per file (``cleanup_batches.purge_group_id``, ADR-0035) with the consented
Current as its only grant, and deletes it by exact VersionId. Recording the
deletion clears Current and removes the Archive Version row together with the
completed proof (``archive_versions_removed_with_proof``). A grant whose
deletion is not proven is revoked, never left to authorize a later run.

Protection labels are not deletion authority: source identity, actual local
absence, the exact Current, catalog state and concurrent work are checked
independently. A reappearance revokes the whole pending selection
(``ArchiveCatalog._revoke_deletion_consents``).
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
from .directory_aggregates import mark_paths_dirty
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


def _aware(value: Any) -> datetime:
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        raise CloudDeletionError("A timezone-aware time is required")
    return moment


def lock_vault(connection: Any, vault_id: int) -> dict[str, Any]:
    row = connection.execute(
        """SELECT v.*, t.bucket AS s3_bucket FROM vaults v
           JOIN vault_targets t ON t.id = v.target_id
           WHERE v.id=%s FOR UPDATE OF v""", (vault_id,)
    ).fetchone()
    if row is None:
        raise CloudDeletionError("Vault not found")
    return row


def _vault_prefix(vault: dict[str, Any]) -> str:
    return f"vaults/{vault['uuid']}/"


def require_local_absence(vault: dict[str, Any], paths: list[str]) -> None:
    """Prove absence through an enrolled root, without following symlinks.

    ENOENT below a verified existing parent proves absence. Permission errors,
    replaced roots, missing mounts, symlinks and nested mounts are not absence.
    No local file (even an unsupported entry) can authorize a cloud deletion.
    """
    access = source_layout.vault_local_access(vault["source_root"])
    if (not access.local_operations_allowed
            or vault.get("lifecycle") not in {"active", "stopping"}):
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


# Everything a decision about one selected file reads, from its live row.
_FILE_SQL = """
    SELECT f.id AS file_id, f.vault_id, f.path, f.path_since, f.local_presence,
           f.held_version_id, f.replacing_version_id, f.current_version_id,
           cur.integrity, cur.availability, cur.object_key, cur.provider_version_id,
           cur.size, COALESCE(att.outcome, cpy.outcome) AS attempt_outcome,
           COALESCE(att.landed_version_id, cpy.landed_version_id) AS landed_version_id,
           EXISTS (SELECT 1 FROM archive_versions other WHERE other.file_id = f.id
                     AND other.id IS DISTINCT FROM f.current_version_id) AS other_versions,
           (SELECT b.purge_group_id FROM cleanup_batches b WHERE b.file_id = f.id) AS batch_group,
           EXISTS (SELECT 1 FROM cleanup_batches b WHERE b.file_id = f.id) AS batch_open,
           EXISTS (SELECT 1 FROM upload_attempts a WHERE a.file_id = f.id
                     AND a.settled_at IS NULL)
           OR EXISTS (SELECT 1 FROM copy_attempts c WHERE c.file_id = f.id
                     AND c.settled_at IS NULL) AS attempt_open
    FROM files f
    LEFT JOIN archive_versions cur ON cur.id = f.current_version_id
    LEFT JOIN upload_attempts att ON att.id = cur.upload_attempt_id
    LEFT JOIN copy_attempts cpy ON cpy.id = cur.copy_attempt_id
"""


def _require_paths(paths: list[str]) -> None:
    if not paths or any(not p or PurePosixPath(p).is_absolute() or ".." in PurePosixPath(p).parts
                        or str(PurePosixPath(p)) != p for p in paths):
        raise CloudDeletionError("Select valid relative file paths")


def _selected_files(connection: Any, *, vault_id: int, paths: list[str],
                    is_directory: bool, whole_vault: bool = False) -> list[dict[str, Any]]:
    """The files of a selection. A folder or the whole Vault covers what still
    has a copy somewhere; an exact path is always covered (and then validated)."""
    params: list[Any] = [vault_id]
    if whole_vault:
        selection = "(f.current_version_id IS NOT NULL OR f.local_presence IS DISTINCT FROM 'missing')"
    else:
        _require_paths(paths)
        clauses = []
        for path in paths:
            if is_directory:
                escaped = path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                clauses.append(
                    "((f.path=%s OR f.path LIKE %s ESCAPE '\\') AND (f.current_version_id IS NOT NULL"
                    " OR f.local_presence IS DISTINCT FROM 'missing'))")
                params.extend((path, escaped + "/%"))
            else:
                clauses.append("f.path=%s")
                params.append(path)
        selection = "(" + " OR ".join(clauses) + ")"
    return connection.execute(
        f"{_FILE_SQL} WHERE f.vault_id=%s AND {selection} ORDER BY f.path, f.id", params,
    ).fetchall()


def _files_by_id(connection: Any, file_ids: list[str]) -> list[dict[str, Any]]:
    return connection.execute(
        f"{_FILE_SQL} WHERE f.id = ANY(%s::uuid[]) ORDER BY f.path, f.id", (file_ids,),
    ).fetchall()


def _validate_files(connection: Any, vault: dict[str, Any], files: list[dict[str, Any]],
                    *, own_group: str | None = None) -> None:
    """Every selected file is Cloud-only with exactly one consentable version."""
    if observation_is_unconfirmed(load_vault_health(connection, int(vault["id"]))):
        raise CloudDeletionError("Protection observations are not confirmed")
    prefix = _vault_prefix(vault)
    for row in files:
        if (row["local_presence"] not in {None, "missing"} or row["held_version_id"] is not None
                or row["current_version_id"] is None or row["replacing_version_id"] is not None
                or row["integrity"] != "verified" or row["availability"] != "available"
                or not row["path"]):
            raise CloudDeletionError("Only Cloud-only files can be deleted from the cloud")
        if (row["other_versions"] or row["attempt_open"]
                or (row["batch_open"] and (own_group is None
                                           or str(row["batch_group"] or "") != str(own_group)))):
            raise CloudDeletionError("Selected file has pending cloud cleanup")
        if (not row["object_key"] or not row["provider_version_id"]
                or row["provider_version_id"] == "null"
                or not str(row["object_key"]).startswith(prefix)
                or row["attempt_outcome"] != "landed"
                or row["landed_version_id"] != row["provider_version_id"]):
            raise CloudDeletionError("An exact cloud VersionId in this Vault is required")
    if files:
        pending = connection.execute(
            """SELECT 1 FROM jobs WHERE file_id = ANY(%s::uuid[])
                 AND status NOT IN ('completed','failed','cancelled')
                 AND (CAST(%s AS uuid) IS NULL OR group_id IS DISTINCT FROM CAST(%s AS uuid))
               LIMIT 1""",
            ([str(r["file_id"]) for r in files], own_group, own_group)).fetchone()
        if pending:
            raise CloudDeletionError("Selected file has pending work")
    require_local_absence(vault, [r["path"] for r in files])


def _preview(files: list[dict[str, Any]]) -> DeletionPreview:
    return DeletionPreview(len(files), len(files), 0, sum(int(f["size"] or 0) for f in files))


def preview_selection(connection: Any, *, vault_id: int, paths: list[str],
                      is_directory: bool = False, whole_vault: bool = False) -> DeletionPreview:
    vault = lock_vault(connection, vault_id)
    files = _selected_files(connection, vault_id=vault_id, paths=paths,
                            is_directory=is_directory, whole_vault=whole_vault)
    if not files:
        raise CloudDeletionError("No Cloud-only files selected")
    _validate_files(connection, vault, files)
    return _preview(files)


def schedule_cloud_purge(connection: Any, *, vault_id: int, paths: list[str],
                         is_directory: bool, actor: str, requested_at: str,
                         confirmation: str, reason: str,
                         whole_vault: bool = False, origin: str = "manual") -> ScheduledDeletion:
    """Record the consent: one group, its request and one pending Job per file."""
    vault = lock_vault(connection, vault_id)
    if vault["lifecycle"] != "active" or vault.get("root_released_at"):
        raise CloudDeletionError("Folder protection has already stopped")
    if origin not in {"manual", "stop-protection"}:
        raise CloudDeletionError("Unsupported deletion origin")
    if confirmation != vault["name"]:
        raise ConfirmationRequired("Type the exact Vault name to confirm")
    if not (reason or "").strip() or len(reason) > 500:
        raise CloudDeletionError("A deletion reason is required")
    requested = _aware(requested_at)
    files = _selected_files(connection, vault_id=vault_id, paths=paths,
                            is_directory=is_directory, whole_vault=whole_vault)
    if not files:
        raise CloudDeletionError("No Cloud-only files selected")
    _validate_files(connection, vault, files)
    preview = _preview(files)
    pending_until = (requested + timedelta(seconds=DELETION_DELAY_SECONDS)).isoformat()
    group_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO job_groups(id, vault_id, path, created_at) VALUES (%s, %s, %s, %s)",
        (group_id, vault_id, "" if whole_vault else paths[0], requested_at))
    connection.execute(
        "INSERT INTO cloud_purge_requests(group_id, reason, stop_protection) VALUES (%s, %s, %s)",
        (group_id, reason.strip(), origin == "stop-protection"))
    rows = connection.execute(
        """INSERT INTO jobs(vault_id, file_id, archive_version_id, group_id, action, status,
                            origin, total_bytes, requested_at, updated_at, run_after)
           SELECT %s, f.file_id, f.version_id, %s, 'cloud-purge', 'pending_delay',
                  'manual', f.size, %s, %s, %s
           FROM unnest(%s::uuid[], %s::uuid[], %s::bigint[]) AS f(file_id, version_id, size)
           RETURNING id, file_id""",
        (vault_id, group_id, requested_at, requested_at, pending_until,
         [str(f["file_id"]) for f in files], [str(f["current_version_id"]) for f in files],
         [int(f["size"] or 0) for f in files])).fetchall()
    job_ids = sorted(int(r["id"]) for r in rows)
    record_audit_event(connection, event="cloud_deletion.requested", actor=actor,
                       vault_id=vault_id, outcome="requested", group_id=group_id,
                       paths=sorted(f["path"] for f in files)[:100], reason=reason.strip(),
                       pending_until=pending_until, preview=preview.as_dict(),
                       stop_protection=origin == "stop-protection")
    return ScheduledDeletion(group_id, job_ids, preview, pending_until)


def _group_jobs(connection: Any, vault_id: int, group_id: str) -> list[dict[str, Any]]:
    return connection.execute(
        """SELECT j.*, EXISTS (SELECT 1 FROM job_leases l WHERE l.job_id = j.id) AS leased
           FROM jobs j WHERE j.vault_id=%s AND j.group_id=%s AND j.action='cloud-purge'
           ORDER BY j.requested_at, j.id FOR UPDATE OF j""",
        (vault_id, group_id)).fetchall()


def cancel_cloud_deletion(connection: Any, *, vault_id: int, group_id: str,
                          actor: str, cancelled_at: str) -> CancelledDeletion:
    lock_vault(connection, vault_id)
    rows = _group_jobs(connection, vault_id, group_id)
    if not rows or any(r["status"] != "pending_delay" or r["leased"] for r in rows):
        raise CloudDeletionError("Deletion has started or is no longer cancellable")
    _cancel_rows(connection, rows, at=cancelled_at, actor=actor,
                 reason="Cancelled before deletion started")
    return CancelledDeletion(len(rows), group_id)


def _cancel_rows(connection: Any, rows: list[dict[str, Any]], *, at: str,
                 actor: str, reason: str) -> None:
    if not rows:
        return
    ids = [int(r["id"]) for r in rows]
    connection.execute(
        """UPDATE jobs SET status='cancelled', message=%s, message_key=NULL, message_params=NULL,
               updated_at=%s, finished_at=%s
           WHERE id = ANY(%s::bigint[]) AND status NOT IN ('completed','failed','cancelled')""",
        (reason, at, at, ids))
    revoke_grants(connection, group_id=str(rows[0]["group_id"]),
                  file_ids=[str(r["file_id"]) for r in rows])
    record_audit_event(connection, event="cloud_deletion.cancelled", actor=actor,
                       vault_id=int(rows[0]["vault_id"]), outcome="cancelled",
                       group_id=str(rows[0]["group_id"]), job_ids=ids[:100], reason=reason)


def revoke_grants(connection: Any, *, group_id: str, file_ids: list[str] | None = None) -> None:
    """Remove the open (not executed) grants of a purge group, with their batches."""
    params: list[Any] = [group_id]
    scope = ""
    if file_ids is not None:
        scope = " AND b.file_id = ANY(%s::uuid[])"
        params.append(file_ids)
    batches = connection.execute(
        f"""SELECT b.id FROM cleanup_batches b WHERE b.purge_group_id=%s{scope}
            FOR UPDATE""", params).fetchall()
    ids = [str(b["id"]) for b in batches]
    if not ids:
        return
    if connection.execute(
            """SELECT 1 FROM cleanup_candidates WHERE batch_id = ANY(%s::uuid[])
                 AND deleted_at IS NOT NULL LIMIT 1""", (ids,)).fetchone():
        raise CloudDeletionError("A recorded cloud deletion is never revoked")
    connection.execute("DELETE FROM cleanup_candidates WHERE batch_id = ANY(%s::uuid[])", (ids,))
    connection.execute("DELETE FROM cleanup_batches WHERE id = ANY(%s::uuid[])", (ids,))


def validate_execution(connection: Any, *, vault_id: int, group_id: str, at: str) -> bool:
    """Under the Vault lock, revoke stale consent before *any* S3 delete call.

    The consent never widens: each Job still names the file's Current, the
    file is still Cloud-only and the request still matches the Vault's
    lifecycle (an ordinary deletion only while active, a Stop Protection one
    only while stopping). Anything else cancels the whole group.
    """
    vault = lock_vault(connection, vault_id)
    rows = _group_jobs(connection, vault_id, group_id)
    active = [r for r in rows if r["status"] not in TERMINAL]
    request = connection.execute(
        "SELECT * FROM cloud_purge_requests WHERE group_id=%s", (group_id,)).fetchone()
    try:
        if not active or request is None or vault.get("root_released_at"):
            raise CloudDeletionError("Deletion authority is no longer active")
        if request["stop_protection"]:
            if vault["lifecycle"] != "stopping":
                raise CloudDeletionError("Stopped folder deletion authority changed")
        elif vault["lifecycle"] != "active":
            raise CloudDeletionError("Deletion authority is no longer active")
        moment = _aware(at)
        for row in active:
            earliest = _aware(row["requested_at"]) + timedelta(seconds=DELETION_DELAY_SECONDS)
            if row["run_after"] is None or moment < earliest or moment < _aware(row["run_after"]):
                raise CloudDeletionError("The seven-day delay has not elapsed")
        files = {str(f["file_id"]): f for f in _files_by_id(
            connection, [str(r["file_id"]) for r in active])}
        for row in active:
            live = files.get(str(row["file_id"]))
            if (live is None or row["archive_version_id"] is None
                    or str(live["current_version_id"] or "") != str(row["archive_version_id"])):
                raise CloudDeletionError("Cloud content changed after confirmation")
        _validate_files(connection, vault, list(files.values()), own_group=group_id)
    except CloudDeletionError as exc:
        _cancel_rows(connection, active, at=at, actor="system", reason=str(exc))
        return False
    return True


def freeze_grants(connection: Any, *, vault: dict[str, Any], group_id: str,
                  jobs: list[dict[str, Any]], provider: str) -> dict[str, str]:
    """Freeze one batch per file granting exactly its consented Current.

    Runs in the transaction that validated the group (Vault locked, members
    claimed). Returns ``{file_id: batch_id}``; an existing open grant of this
    group is reused, never widened.
    """
    file_ids = [str(j["file_id"]) for j in jobs]
    existing = {str(r["file_id"]): str(r["id"]) for r in connection.execute(
        "SELECT id, file_id FROM cleanup_batches WHERE purge_group_id=%s AND file_id = ANY(%s::uuid[])",
        (group_id, file_ids)).fetchall()}
    fresh = [j for j in jobs if str(j["file_id"]) not in existing]
    if fresh:
        rows = connection.execute(
            """INSERT INTO cleanup_batches(vault_id, file_id, purge_group_id, job_id, provider,
                                           path_since, source_root, root_identity)
               SELECT %s, f.id, %s, j.job_id, %s, f.path_since, %s, %s
               FROM unnest(%s::uuid[], %s::bigint[]) AS j(file_id, job_id)
               JOIN files f ON f.id = j.file_id
               RETURNING id, file_id""",
            (vault["id"], group_id, provider, vault["source_root"], vault.get("root_identity"),
             [str(j["file_id"]) for j in fresh], [int(j["id"]) for j in fresh])).fetchall()
        created = {str(r["file_id"]): str(r["id"]) for r in rows}
        connection.execute(
            """INSERT INTO cleanup_candidates(batch_id, file_id, archive_version_id)
               SELECT b.batch_id, b.file_id, b.version_id
               FROM unnest(%s::uuid[], %s::uuid[], %s::uuid[]) AS b(batch_id, file_id, version_id)""",
            ([created[str(j["file_id"])] for j in fresh], [str(j["file_id"]) for j in fresh],
             [str(j["archive_version_id"]) for j in fresh]))
        existing.update(created)
    return existing


def record_deleted(connection: Any, *, jobs: list[dict[str, Any]], at: str, actor: str) -> None:
    """The exact DELETE returned (or the version is proven absent): record it.

    Clears Current and removes the Archive Version row with its completed
    proof, then completes each Job. The caller holds the group claim.
    """
    if not jobs:
        return
    versions = [str(j["archive_version_id"]) for j in jobs]
    files = [str(j["file_id"]) for j in jobs]
    connection.execute(
        """UPDATE cleanup_candidates c SET deleted_at=%s
           FROM cleanup_batches b
           WHERE b.id = c.batch_id AND b.purge_group_id = %s
             AND c.archive_version_id = ANY(%s::uuid[]) AND c.deleted_at IS NULL""",
        (at, str(jobs[0]["group_id"]), versions))
    proven = connection.execute(
        """SELECT count(*) AS n FROM cleanup_candidates
           WHERE archive_version_id = ANY(%s::uuid[]) AND deleted_at IS NOT NULL""",
        (versions,)).fetchone()["n"]
    if int(proven) != len(versions):
        raise CloudDeletionError("A cloud deletion is recorded only with its grant")
    connection.execute(
        """UPDATE files SET current_version_id=NULL, matched_version_id=NULL,
               held_version_id=NULL
           WHERE id = ANY(%s::uuid[]) AND current_version_id = ANY(%s::uuid[])""",
        (files, versions))
    batches = [str(r["batch_id"]) for r in connection.execute(
        "SELECT batch_id FROM cleanup_candidates WHERE archive_version_id = ANY(%s::uuid[])",
        (versions,)).fetchall()]
    connection.execute("DELETE FROM archive_versions WHERE id = ANY(%s::uuid[])", (versions,))
    connection.execute("DELETE FROM cleanup_batches WHERE id = ANY(%s::uuid[])", (batches,))
    ids = [int(j["id"]) for j in jobs]
    connection.execute(
        """UPDATE jobs SET status='completed', message='Cloud deletion completed',
               message_key=NULL, message_params=NULL, transferred_bytes=total_bytes,
               updated_at=%s, finished_at=%s
           WHERE id = ANY(%s::bigint[]) AND status NOT IN ('completed','failed','cancelled')""",
        (at, at, ids))
    mark_paths_dirty(connection, int(jobs[0]["vault_id"]), [r["path"] for r in connection.execute(
        "SELECT path FROM files WHERE id = ANY(%s::uuid[])", (files,)).fetchall()])
    record_audit_event(connection, event="cloud_deletion.completed", actor=actor,
                       vault_id=int(jobs[0]["vault_id"]), outcome="success",
                       group_id=str(jobs[0]["group_id"]), deleted=len(ids), job_ids=ids[:100])


def record_failed(connection: Any, *, failures: list[tuple[dict[str, Any], str]],
                  at: str, actor: str) -> None:
    """A deletion the provider refused: revoke its grant and fail its Job."""
    if not failures:
        return
    group_id = str(failures[0][0]["group_id"])
    revoke_grants(connection, group_id=group_id,
                  file_ids=[str(job["file_id"]) for job, _ in failures])
    for job, message in failures:
        connection.execute(
            """UPDATE jobs SET status='failed', message=%s, message_key=NULL, message_params=NULL,
                   updated_at=%s, finished_at=%s
               WHERE id=%s AND status NOT IN ('completed','failed','cancelled')""",
            (f"Cloud deletion failed: {message}"[:500], at, at, int(job["id"])))
    record_audit_event(connection, event="cloud_deletion.partial_failure", actor=actor,
                       vault_id=int(failures[0][0]["vault_id"]), outcome="partial_failure",
                       group_id=group_id, failed=len(failures),
                       job_ids=[int(job["id"]) for job, _ in failures][:100])
