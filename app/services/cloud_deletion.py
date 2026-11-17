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

An automatic request (Automatic Duplicate Cleanup, #726, ADR-0048) covers
only Cloud Duplicates. Its members are revoked one by one: at execution, a
file that is no longer a valid Cloud Duplicate is skipped and recorded, and
the rest of the group proceeds. Anything about the Vault itself still cancels
the whole group, as for the Owner's requests, which stay all-or-nothing.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any

from . import source_layout, vault_relocation
from .audit_events import record_audit_event
from .component_health import load_vault_health
from .directory_aggregates import lock_vaults_in_order, mark_paths_dirty
from .protection_states import (CURRENT_JOIN_SQL, PROTECTED_SQL, STATES, VISIBLE_SQL,
                                masked_state_sql, observation_is_unconfirmed)

DELETION_DELAY_SECONDS = 7 * 24 * 60 * 60
TERMINAL = ("completed", "failed", "cancelled")


class CloudDeletionError(ValueError):
    """The request no longer proves safe, deliberate deletion authority."""


class ConfirmationRequired(CloudDeletionError):
    pass


class SelectionChanged(CloudDeletionError):
    """What a folder selection covers differs from what its preview listed (#723)."""

    message_key = "api.cloud_purge_selection_changed"


# Why a file of a folder selection is skipped (#723), in the order they are tested.
SKIP_REASONS = ("present_locally", "pending_work", "pending_cleanup", "conflict",
                "not_verified", "no_exact_version")


@dataclass(frozen=True)
class ListingFilter:
    """A file-list view (``/api/files``): ``q`` searches the Vault, otherwise
    the direct children of ``directory``; ``state`` filters both."""

    directory: str = ""
    q: str = ""
    state: str = ""


@dataclass(frozen=True)
class DeletionPreview:
    """What a confirmation schedules (the counts) and how the selection splits.

    ``object_count``/``byte_count`` are the files that would be scheduled. A
    folder, multi-item or list selection (``mode`` "selection") covers only its
    eligible files; ``skipped`` counts the rest per reason. Eligible files are
    Cloud Duplicates or have no local copy anywhere in the Vault; with
    ``duplicates_only`` only the duplicates are scheduled. ``fingerprint``
    names the exact (file, Current) set: a confirmation must present it.
    """

    object_count: int
    version_count: int
    delete_marker_count: int
    byte_count: int
    fingerprint: str = ""
    mode: str = "exact"
    duplicates_only: bool = False
    eligible_count: int = 0
    eligible_bytes: int = 0
    duplicate_count: int = 0
    duplicate_bytes: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    # The scheduled files still within their Minimum Storage Duration at the
    # earliest execution, and what deleting them then still bills (#731).
    minimum_storage: dict[str, Any] | None = None

    @property
    def no_local_copy_count(self) -> int:
        return self.eligible_count - self.duplicate_count

    @property
    def no_local_copy_bytes(self) -> int:
        return self.eligible_bytes - self.duplicate_bytes

    def as_dict(self) -> dict[str, Any]:
        return dict(object_count=self.object_count, version_count=self.version_count,
                    delete_marker_count=self.delete_marker_count, byte_count=self.byte_count,
                    fingerprint=self.fingerprint, mode=self.mode,
                    duplicates_only=self.duplicates_only,
                    eligible_count=self.eligible_count, eligible_bytes=self.eligible_bytes,
                    duplicate_count=self.duplicate_count, duplicate_bytes=self.duplicate_bytes,
                    no_local_copy_count=self.no_local_copy_count,
                    no_local_copy_bytes=self.no_local_copy_bytes,
                    skipped={reason: int(self.skipped.get(reason, 0)) for reason in SKIP_REASONS},
                    skipped_count=sum(int(n) for n in self.skipped.values()),
                    minimum_storage=self.minimum_storage)


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
    _present_paths(vault, paths, first_only=True)


def _present_paths(vault: dict[str, Any], paths: list[str], *,
                   first_only: bool = False) -> set[str]:
    """The paths with a local entry; anything that is not proven absence raises.

    ``first_only`` raises at the first present entry (an exact selection);
    otherwise the present paths are returned so a folder selection skips them.
    """
    present: set[str] = set()
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
        root_device = os.fstat(root).st_dev
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
                    if (os.fstat(child).st_dev != root_device
                            or source_layout.path_is_mount(f"/proc/self/fd/{child}")):
                        raise CloudDeletionError("Nested mount prevents confirming absence")
                    parent = child
                else:
                    try:
                        os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if first_only:
                        raise CloudDeletionError("A selected file is present on the server")
                    present.add(path)
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
    return present


# Everything a decision about one selected file reads, from its live row.
_FILE_SQL = """
    SELECT f.id AS file_id, f.vault_id, f.path, f.path_since, f.local_presence,
           f.held_version_id, f.replacing_version_id, f.current_version_id,
           cur.integrity, cur.availability, cur.object_key, cur.provider_version_id,
           cur.size, cur.storage_class, cur.storage_class_since,
           cur.created_at AS version_created_at,
           COALESCE(att.outcome, cpy.outcome) AS attempt_outcome,
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


def _row_problem(row: dict[str, Any], prefix: str, own_group: str | None) -> str | None:
    """Why one live file row cannot be consented, or None when it can."""
    if (row["local_presence"] not in {None, "missing"} or row["held_version_id"] is not None
            or row["current_version_id"] is None or row["replacing_version_id"] is not None
            or row["integrity"] != "verified" or row["availability"] != "available"
            or not row["path"]):
        return "Only Cloud-only files can be deleted from the cloud"
    if (row["other_versions"] or row["attempt_open"]
            or (row["batch_open"] and (own_group is None
                                       or str(row["batch_group"] or "") != str(own_group)))):
        return "Selected file has pending cloud cleanup"
    if (not row["object_key"] or not row["provider_version_id"]
            or row["provider_version_id"] == "null"
            or not str(row["object_key"]).startswith(prefix)
            or row["attempt_outcome"] != "landed"
            or row["landed_version_id"] != row["provider_version_id"]):
        return "An exact cloud VersionId in this Vault is required"
    return None


def _busy_files(connection: Any, file_ids: list[str], own_group: str | None) -> set[str]:
    """The files with unfinished work outside ``own_group``."""
    if not file_ids:
        return set()
    rows = connection.execute(
        """SELECT DISTINCT file_id FROM jobs WHERE file_id = ANY(%s::uuid[])
             AND status NOT IN ('completed','failed','cancelled')
             AND (CAST(%s AS uuid) IS NULL OR group_id IS DISTINCT FROM CAST(%s AS uuid))""",
        (file_ids, own_group, own_group)).fetchall()
    return {str(r["file_id"]) for r in rows}


def _validate_files(connection: Any, vault: dict[str, Any], files: list[dict[str, Any]],
                    *, own_group: str | None = None, check_local: bool = True) -> None:
    """Every selected file is Cloud-only with exactly one consentable version."""
    if observation_is_unconfirmed(load_vault_health(connection, int(vault["id"]))):
        raise CloudDeletionError("Protection observations are not confirmed")
    prefix = _vault_prefix(vault)
    for row in files:
        problem = _row_problem(row, prefix, own_group)
        if problem:
            raise CloudDeletionError(problem)
    if _busy_files(connection, [str(r["file_id"]) for r in files], own_group):
        raise CloudDeletionError("Selected file has pending work")
    if check_local:
        require_local_absence(vault, [r["path"] for r in files])


@dataclass(frozen=True)
class _Selection:
    """What one request names: exact files, recursive folders, a list view.

    Strict (all-or-nothing, as before #723): one exact file, or the whole
    Vault of a Stop Protection. Anything else covers its eligible files only.
    """

    files: tuple[str, ...] = ()
    directories: tuple[str, ...] = ()
    listing: ListingFilter | None = None
    whole_vault: bool = False
    # Cloud Duplicates of the whole Vault (#726, #729): every file with a
    # Current and no Local Copy, the only files that can be eligible.
    cloud_only: bool = False

    @property
    def strict(self) -> bool:
        return self.whole_vault or (
            len(self.files) == 1 and not self.directories and self.listing is None)

    @property
    def group_path(self) -> str:
        if self.whole_vault:
            return ""
        if self.files or self.directories:
            return (self.files or self.directories)[0]
        return self.listing.directory if self.listing else ""


def _selection(*, paths: list[str], is_directory: bool, whole_vault: bool,
               directories: list[str] | None, listing: ListingFilter | None) -> _Selection:
    if whole_vault:
        return _Selection(whole_vault=True)
    folders = list(directories or [])
    files: list[str] = []
    if is_directory:
        folders = [*paths, *folders]
    else:
        files = list(paths)
    if files:
        _require_paths(files)
    if folders:
        _require_paths(folders)
    if listing is not None:
        if listing.state and listing.state not in STATES:
            raise CloudDeletionError("Unknown protection state")
        if listing.directory:
            _require_paths([listing.directory])
    if not files and not folders and listing is None:
        raise CloudDeletionError("Select valid relative file paths")
    return _Selection(tuple(dict.fromkeys(files)), tuple(dict.fromkeys(folders)), listing)


def _unconfirmed(connection: Any, vault: dict[str, Any]) -> Any:
    from .operation_policies import unconfirmed_scope
    access = source_layout.vault_local_access(vault.get("source_root") or "")
    return unconfirmed_scope(connection, int(vault["id"]), lifecycle=vault.get("lifecycle", "active"),
                             local_operations_allowed=access.local_operations_allowed)


_HAS_COPY_SQL = "(f.current_version_id IS NOT NULL OR f.local_presence IS DISTINCT FROM 'missing')"


def _listing_sql(connection: Any, vault: dict[str, Any],
                 listing: ListingFilter) -> tuple[str, list[Any]]:
    """The files a list view shows, with every folder it shows taken whole.

    The same filters as ``/api/files``: a search covers the matching files of
    the Vault; a folder view its direct child files in ``state`` and each
    child folder holding at least one file in ``state``.
    """
    from ..catalog import ArchiveCatalog

    vault_id = int(vault["id"])
    catalog = ArchiveCatalog(connection, protection_unconfirmed=_unconfirmed(connection, vault))
    if listing.q:
        clauses, params = catalog._direct_child_file_clauses(
            vault_id, directory="", state=listing.state, search=listing.q)
        return "(" + " AND ".join(clauses) + ")", params
    clauses, params = catalog._direct_child_file_clauses(
        vault_id, directory=listing.directory, state=listing.state)
    prefix = f"{listing.directory}/" if listing.directory else ""
    start = len(prefix) + 1
    nested = "starts_with(f.path, %s) AND strpos(substr(f.path, %s), '/') > 0"
    folder = f"({nested} AND {_HAS_COPY_SQL})"
    folder_params: list[Any] = [prefix, start]
    if listing.state:
        if catalog.protection_unconfirmed or catalog.unconfirmed.by_path:
            blocked, blocked_params = catalog.unconfirmed.sql("f.path")
            state = masked_state_sql(blocked, catalog._state_sql(vault_id))
        else:
            blocked_params, state = [], catalog._state_sql(vault_id)
        folder = f"""({folder} AND split_part(substr(f.path, %s), '/', 1) IN (
            SELECT split_part(substr(f.path, %s), '/', 1) FROM files f {CURRENT_JOIN_SQL}
            WHERE f.vault_id = %s AND {nested} AND {VISIBLE_SQL} AND ({state}) = %s))"""
        folder_params += [start, start, vault_id, prefix, start, *blocked_params, listing.state]
    return f"(({' AND '.join(clauses)}) OR {folder})", [*params, *folder_params]


def _selection_sql(connection: Any, vault: dict[str, Any],
                   selection: _Selection) -> tuple[str, list[Any]]:
    parts: list[str] = []
    params: list[Any] = []
    if selection.files:
        parts.append("f.path = ANY(%s::text[])")
        params.append(list(selection.files))
    if selection.directories:
        # starts_with treats SQL wildcards in folder names literally.
        parts.append(f"""(EXISTS (SELECT 1 FROM unnest(%s::text[]) AS d(path)
                         WHERE f.path = d.path OR starts_with(f.path, d.path || '/'))
                         AND {_HAS_COPY_SQL})""")
        params.append(list(selection.directories))
    if selection.listing is not None:
        sql, values = _listing_sql(connection, vault, selection.listing)
        parts.append(sql)
        params.extend(values)
    if selection.cloud_only:
        parts.append("(f.current_version_id IS NOT NULL"
                     " AND (f.local_presence IS NULL OR f.local_presence = 'missing'))")
    return "(" + " OR ".join(parts) + ")", params


# One row per selected file and why it is skipped, in SQL: no per-file Python
# work for the (possibly huge) skipped part of a folder. The same facts as
# _validate_files, which still revalidates the eligible set before consent.
_CLASSIFY_SQL = """
WITH c AS MATERIALIZED (
    SELECT f.id AS file_id, f.path, f.current_version_id, cur.size, f.local_seen_at,
      cur.storage_class, cur.storage_class_since, cur.created_at AS version_created_at,
      CASE
        WHEN f.local_presence IS NOT NULL AND f.local_presence <> 'missing' THEN 'present_locally'
        WHEN f.current_version_id IS NULL THEN 'not_verified'
        WHEN f.replacing_version_id IS NOT NULL
          OR EXISTS (SELECT 1 FROM upload_attempts a WHERE a.file_id = f.id AND a.settled_at IS NULL)
          OR EXISTS (SELECT 1 FROM copy_attempts p WHERE p.file_id = f.id AND p.settled_at IS NULL)
          OR EXISTS (SELECT 1 FROM jobs j WHERE j.file_id = f.id
                       AND j.status NOT IN ('completed','failed','cancelled'))
          THEN 'pending_work'
        WHEN EXISTS (SELECT 1 FROM cleanup_batches b WHERE b.file_id = f.id)
          OR EXISTS (SELECT 1 FROM archive_versions other WHERE other.file_id = f.id
                       AND other.id <> f.current_version_id)
          THEN 'pending_cleanup'
        WHEN f.held_version_id IS NOT NULL THEN 'conflict'
        WHEN cur.integrity IS DISTINCT FROM 'verified'
          OR cur.availability IS DISTINCT FROM 'available' THEN 'not_verified'
        WHEN cur.object_key IS NULL OR NOT starts_with(cur.object_key, %s)
          OR cur.provider_version_id IS NULL OR cur.provider_version_id IN ('', 'null')
          OR COALESCE(att.outcome, cpy.outcome) IS DISTINCT FROM 'landed'
          OR COALESCE(att.landed_version_id, cpy.landed_version_id)
             IS DISTINCT FROM cur.provider_version_id
          THEN 'no_exact_version'
        ELSE 'eligible'
      END AS reason
    FROM files f
    LEFT JOIN archive_versions cur ON cur.id = f.current_version_id
    LEFT JOIN upload_attempts att ON att.id = cur.upload_attempt_id
    LEFT JOIN copy_attempts cpy ON cpy.id = cur.copy_attempt_id
    WHERE f.vault_id = %s AND f.path <> '' AND {selection}
)
SELECT file_id, path, current_version_id, size, local_seen_at, storage_class,
       storage_class_since, version_created_at, reason, NULL::bigint AS n
FROM c WHERE reason = 'eligible'
UNION ALL
SELECT NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, reason, count(*) FROM c
WHERE reason <> 'eligible'
GROUP BY reason
"""


def _duplicate_ids(connection: Any, vault: dict[str, Any], file_ids: list[str]) -> set[str]:
    """The Cloud Duplicates among ``file_ids`` (CONTEXT.md, #723).

    Another file of the same Vault, at a different path, is present locally
    and protected by a verified, available Current with the same SHA-256 as
    this file's Current. A copy whose protection is not confirmed (ADR-0042)
    or that has an open problem does not count.
    """
    if not file_ids:
        return set()
    blocked, blocked_params = _unconfirmed(connection, vault).sql("f.path")
    rows = connection.execute(
        f"""SELECT DISTINCT e.id AS file_id
            FROM files e
            JOIN archive_versions ecur ON ecur.id = e.current_version_id
            -- files_rename_digest_idx (vault_id, local_sha256) finds the copies.
            JOIN files f ON f.vault_id = e.vault_id AND f.local_sha256 = ecur.plaintext_sha256
            JOIN archive_versions cur ON cur.id = f.current_version_id
            WHERE e.id = ANY(%s::uuid[]) AND ecur.plaintext_sha256 IS NOT NULL
              AND f.path <> e.path
              AND f.held_version_id IS NULL AND f.reconciliation_block IS NULL
              AND {PROTECTED_SQL} AND NOT ({blocked})""",
        [file_ids, *blocked_params]).fetchall()
    return {str(r["file_id"]) for r in rows}


def minimum_storage_summary(connection: Any, vault: dict[str, Any], files: list[dict[str, Any]],
                            *, now: datetime | None = None) -> dict[str, Any]:
    """What ``files`` still bill for their Minimum Storage Duration when deleted
    at the earliest execution, after the delay (#731): a Cost Estimate."""
    from . import minimum_storage
    from .lifecycle_policies import load_vault_plan

    moment = now or datetime.now(timezone.utc)
    return minimum_storage.deletion_summary(
        files, plan=load_vault_plan(connection, int(vault["id"])), now=moment,
        at=moment + timedelta(seconds=DELETION_DELAY_SECONDS))


def _fingerprint(files: list[dict[str, Any]]) -> str:
    members = sorted(f"{f['file_id']}:{f['current_version_id']}" for f in files)
    return hashlib.sha256("\n".join(members).encode()).hexdigest()


def _resolve(connection: Any, vault: dict[str, Any], selection: _Selection, *,
             duplicates_only: bool) -> tuple[list[dict[str, Any]], DeletionPreview]:
    """The files a confirmation would schedule, and the preview describing them."""
    if selection.strict:
        files = _selected_files(connection, vault_id=int(vault["id"]), paths=list(selection.files),
                                is_directory=False, whole_vault=selection.whole_vault)
        if not files:
            raise CloudDeletionError("No Cloud-only files selected")
        _validate_files(connection, vault, files)
        duplicates = set() if selection.whole_vault else _duplicate_ids(
            connection, vault, [str(f["file_id"]) for f in files])
        if duplicates_only and not selection.whole_vault and len(duplicates) != len(files):
            raise CloudDeletionError("The selected file is not a Cloud Duplicate")
        eligible, skipped, mode = files, {}, "exact"
    else:
        if observation_is_unconfirmed(load_vault_health(connection, int(vault["id"]))):
            raise CloudDeletionError("Protection observations are not confirmed")
        where, params = _selection_sql(connection, vault, selection)
        rows = connection.execute(_CLASSIFY_SQL.format(selection=where),
                                  [_vault_prefix(vault), int(vault["id"]), *params]).fetchall()
        eligible = [r for r in rows if r["reason"] == "eligible"]
        skipped = {r["reason"]: int(r["n"]) for r in rows if r["reason"] != "eligible"}
        # Local absence is proven for the eligible files only; an entry the
        # catalog has not observed yet is skipped like any present file.
        present = _present_paths(vault, [r["path"] for r in eligible]) if eligible else set()
        if present:
            eligible = [r for r in eligible if r["path"] not in present]
            skipped["present_locally"] = skipped.get("present_locally", 0) + len(present)
        duplicates = _duplicate_ids(connection, vault, [str(r["file_id"]) for r in eligible])
        mode = "selection"
    chosen = ([f for f in eligible if str(f["file_id"]) in duplicates]
              if duplicates_only else eligible)
    size = lambda rows: sum(int(r["size"] or 0) for r in rows)  # noqa: E731
    duplicate_rows = [f for f in eligible if str(f["file_id"]) in duplicates]
    preview = DeletionPreview(
        len(chosen), len(chosen), 0, size(chosen), fingerprint=_fingerprint(chosen), mode=mode,
        duplicates_only=duplicates_only, eligible_count=len(eligible), eligible_bytes=size(eligible),
        duplicate_count=len(duplicate_rows), duplicate_bytes=size(duplicate_rows), skipped=skipped,
        minimum_storage=minimum_storage_summary(connection, vault, chosen))
    return chosen, preview


def preview_selection(connection: Any, *, vault_id: int, paths: list[str],
                      is_directory: bool = False, whole_vault: bool = False,
                      directories: list[str] | None = None,
                      listing: ListingFilter | None = None,
                      duplicates_only: bool = False) -> DeletionPreview:
    vault = lock_vault(connection, vault_id)
    selection = _selection(paths=paths, is_directory=is_directory, whole_vault=whole_vault,
                           directories=directories, listing=listing)
    return _resolve(connection, vault, selection, duplicates_only=duplicates_only)[1]


def schedule_cloud_purge(connection: Any, *, vault_id: int, paths: list[str],
                         is_directory: bool, actor: str, requested_at: str,
                         confirmation: str, reason: str,
                         whole_vault: bool = False, origin: str = "manual",
                         directories: list[str] | None = None,
                         listing: ListingFilter | None = None,
                         duplicates_only: bool = False,
                         fingerprint: str | None = None) -> ScheduledDeletion:
    """Record the consent: one group, its request and one pending Job per file.

    A folder, multi-item or list selection schedules exactly the set its
    preview listed: ``fingerprint`` must match the recomputed one (#723).
    """
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
    selection = _selection(paths=paths, is_directory=is_directory, whole_vault=whole_vault,
                           directories=directories, listing=listing)
    files, preview = _resolve(connection, vault, selection, duplicates_only=duplicates_only)
    if (fingerprint is not None or not selection.strict) and fingerprint != preview.fingerprint:
        raise SelectionChanged("The selection changed since the preview: preview it again")
    if not files:
        raise CloudDeletionError("No Cloud-only files selected")
    if not selection.strict:
        # The eligible set passes the very checks of an exact selection.
        _validate_files(connection, vault, _files_by_id(connection, [str(f["file_id"]) for f in files]),
                        check_local=False)
    return _record_consent(connection, vault_id=vault_id, files=files, preview=preview,
                           group_path=selection.group_path, actor=actor, requested=requested,
                           requested_at=requested_at, reason=reason, origin=origin)


def _record_consent(connection: Any, *, vault_id: int, files: list[dict[str, Any]],
                    preview: DeletionPreview, group_path: str, actor: str, requested: datetime,
                    requested_at: str, reason: str, origin: str) -> ScheduledDeletion:
    """One group, its request and one pending Job per file, then the audit record."""
    pending_until = (requested + timedelta(seconds=DELETION_DELAY_SECONDS)).isoformat()
    group_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO job_groups(id, vault_id, path, created_at) VALUES (%s, %s, %s, %s)",
        (group_id, vault_id, group_path, requested_at))
    connection.execute(
        """INSERT INTO cloud_purge_requests(group_id, reason, stop_protection, automatic)
           VALUES (%s, %s, %s, %s)""",
        (group_id, reason.strip(), origin == "stop-protection", origin == "automatic"))
    rows = connection.execute(
        """INSERT INTO jobs(vault_id, file_id, archive_version_id, group_id, action, status,
                            origin, total_bytes, requested_at, updated_at, run_after)
           SELECT %s, f.file_id, f.version_id, %s, 'cloud-purge', 'pending_delay',
                  %s, f.size, %s, %s, %s
           FROM unnest(%s::uuid[], %s::uuid[], %s::bigint[]) AS f(file_id, version_id, size)
           RETURNING id, file_id""",
        (vault_id, group_id, "automatic" if origin == "automatic" else "manual",
         requested_at, requested_at, pending_until,
         [str(f["file_id"]) for f in files], [str(f["current_version_id"]) for f in files],
         [int(f["size"] or 0) for f in files])).fetchall()
    job_ids = sorted(int(r["id"]) for r in rows)
    record_audit_event(connection, event="cloud_deletion.requested", actor=actor,
                       vault_id=vault_id, outcome="requested", group_id=group_id,
                       paths=sorted(f["path"] for f in files)[:100], reason=reason.strip(),
                       pending_until=pending_until, preview=preview.as_dict(),
                       stop_protection=origin == "stop-protection",
                       automatic=origin == "automatic")
    return ScheduledDeletion(group_id, job_ids, preview, pending_until)


def cloud_duplicates(connection: Any,
                     vault: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Every Cloud Duplicate of the Vault a deletion could schedule now (#726, #729).

    The same set as the manual «Only Cloud Duplicates» preview of the whole
    Vault: the eligible Cloud-only files (files already in pending work, such
    as another deletion group, are skipped as ``pending_work``) narrowed to the
    Cloud Duplicates; local absence is then proven for those only. Each row
    carries ``local_seen_at`` (None when never seen locally). Returns the rows
    and the skipped counts per reason.
    """
    if observation_is_unconfirmed(load_vault_health(connection, int(vault["id"]))):
        raise CloudDeletionError("Protection observations are not confirmed")
    where, params = _selection_sql(connection, vault, _Selection(cloud_only=True))
    rows = connection.execute(_CLASSIFY_SQL.format(selection=where),
                              [_vault_prefix(vault), int(vault["id"]), *params]).fetchall()
    eligible = [r for r in rows if r["reason"] == "eligible"]
    duplicates = _duplicate_ids(connection, vault, [str(r["file_id"]) for r in eligible])
    chosen = [r for r in eligible if str(r["file_id"]) in duplicates]
    # Always called, even with nothing left to check: an unreadable root or a
    # changed identity must raise, so a count of zero is never a guess (#729).
    present = _present_paths(vault, [r["path"] for r in chosen])
    skipped = {r["reason"]: int(r["n"]) for r in rows if r["reason"] != "eligible"}
    if present:
        skipped["present_locally"] = skipped.get("present_locally", 0) + len(present)
    return [r for r in chosen if r["path"] not in present], skipped


@dataclass(frozen=True)
class Sighting:
    """Cloud Duplicates split by the last sighting of their Local Copy (#729).

    ``ready``: seen at or before the cut-off; ``waiting``: seen after it;
    ``unseen``: never seen locally, so never a candidate of the automatic
    cleanup (no invented time ever makes it one). ``held``: seen long enough
    ago but still within their Minimum Storage Duration at the earliest
    execution, kept back when the cleanup waits for it (#731).
    """
    ready: list[dict[str, Any]]
    waiting: list[dict[str, Any]]
    unseen: list[dict[str, Any]]
    held: list[dict[str, Any]] = field(default_factory=list)


def split_by_sighting(rows: list[dict[str, Any]], seen_before: datetime) -> Sighting:
    ready: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    unseen: list[dict[str, Any]] = []
    for row in rows:
        seen = row["local_seen_at"]
        (unseen if seen is None else ready if _aware(seen) <= seen_before else waiting).append(row)
    return Sighting(ready, waiting, unseen)


def automatic_duplicates(connection: Any, vault: dict[str, Any], *, seen_before: str,
                         beyond_minimum: bool = False, now: datetime | None = None,
                         ) -> tuple[DeletionPreview, Sighting]:
    """The Cloud Duplicates an automatic run would schedule (#726, ADR-0048).

    Those of :func:`cloud_duplicates` whose Local Copy was last seen at or
    before ``seen_before`` (``Sighting.ready``), with their preview; a file
    without another protected local copy is never among them. The waiting
    and never-seen Cloud Duplicates are returned too (#729). With
    ``beyond_minimum`` a duplicate still within its Minimum Storage Duration
    at the earliest execution is held back (``Sighting.held``, #731).
    """
    from . import minimum_storage
    from .lifecycle_policies import load_vault_plan

    rows, skipped = cloud_duplicates(connection, vault)
    sighting = split_by_sighting(rows, _aware(seen_before))
    moment = now or datetime.now(timezone.utc)
    if beyond_minimum and sighting.ready:
        plan = load_vault_plan(connection, int(vault["id"]))
        executes = moment + timedelta(seconds=DELETION_DELAY_SECONDS)
        ready: list[dict[str, Any]] = []
        held: list[dict[str, Any]] = []
        for row in sighting.ready:
            (ready if minimum_storage.beyond_minimum(row, plan=plan, at=executes, now=moment)
             else held).append(row)
        sighting = Sighting(ready, sighting.waiting, sighting.unseen, held)
    chosen = sighting.ready
    size = sum(int(r["size"] or 0) for r in chosen)
    preview = DeletionPreview(
        len(chosen), len(chosen), 0, size, fingerprint=_fingerprint(chosen), mode="automatic",
        duplicates_only=True, eligible_count=len(chosen), eligible_bytes=size,
        duplicate_count=len(chosen), duplicate_bytes=size, skipped=skipped,
        minimum_storage=minimum_storage_summary(connection, vault, chosen, now=moment))
    return preview, sighting


def schedule_automatic_duplicates(connection: Any, *, vault: dict[str, Any],
                                  files: list[dict[str, Any]], preview: DeletionPreview,
                                  requested_at: str, reason: str) -> ScheduledDeletion:
    """Record one automatic Delayed Cloud Deletion of ``files`` (#726).

    The caller holds the Vault lock and resolved ``files`` with
    :func:`automatic_duplicates` in this transaction. The same seven-day delay
    and execution checks as the Owner's requests apply. The audit actor is
    ``system``; the request, its Jobs (origin ``automatic``) and the audit
    record say it was automatic.
    """
    if vault["lifecycle"] != "active" or vault.get("root_released_at") or vault.get("paused"):
        raise CloudDeletionError("Automatic cleanup runs only on an active Vault")
    if not files:
        raise CloudDeletionError("No Cloud-only files selected")
    if not (reason or "").strip() or len(reason) > 500:
        raise CloudDeletionError("A deletion reason is required")
    _validate_files(connection, vault, _files_by_id(connection, [str(f["file_id"]) for f in files]),
                    check_local=False)
    return _record_consent(connection, vault_id=int(vault["id"]), files=files, preview=preview,
                           group_path="", actor="system", requested=_aware(requested_at),
                           requested_at=requested_at, reason=reason, origin="automatic")


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


def validate_execution(connection: Any, *, vault_id: int, group_id: str, at: str,
                       revoked: set[int] | None = None) -> bool:
    """Under the Vault lock, revoke stale consent before *any* S3 delete call.

    The consent never widens: each Job still names the file's Current, the
    file is still Cloud-only and the request still matches the Vault's
    lifecycle (an ordinary deletion only while active, a Stop Protection one
    only while stopping). Anything else cancels the whole group, except in an
    automatic group (#726): there a file that no longer qualifies is revoked
    alone (its Job id is added to ``revoked``) and the others proceed.
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
        if request.get("automatic"):
            invalid = _automatic_invalid(connection, vault, active, files, group_id)
            dropped = [r for r in active if str(r["file_id"]) in invalid]
            if dropped:
                _revoke_members(connection, dropped, invalid, files, at=at)
                if revoked is not None:
                    revoked.update(int(r["id"]) for r in dropped)
            return len(dropped) < len(active)
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


def _automatic_invalid(connection: Any, vault: dict[str, Any], active: list[dict[str, Any]],
                       files: dict[str, dict[str, Any]], group_id: str) -> dict[str, str]:
    """``{file_id: why}`` for the members of an automatic group that no longer qualify.

    Each member must still be exactly what it consented to, Cloud-only with no
    other work, absent locally and a Cloud Duplicate. What concerns the whole
    Vault (unconfirmed observations, an unreadable root) raises instead and
    cancels the group.
    """
    if observation_is_unconfirmed(load_vault_health(connection, int(vault["id"]))):
        raise CloudDeletionError("Protection observations are not confirmed")
    prefix = _vault_prefix(vault)
    invalid: dict[str, str] = {}
    for row in active:
        file_id = str(row["file_id"])
        live = files.get(file_id)
        if (live is None or row["archive_version_id"] is None
                or str(live["current_version_id"] or "") != str(row["archive_version_id"])):
            invalid[file_id] = "Cloud content changed after confirmation"
            continue
        problem = _row_problem(live, prefix, group_id)
        if problem:
            invalid[file_id] = problem
    remaining = [file_id for file_id in (str(r["file_id"]) for r in active) if file_id not in invalid]
    for file_id in _busy_files(connection, remaining, group_id):
        invalid[file_id] = "Selected file has pending work"
    remaining = [file_id for file_id in remaining if file_id not in invalid]
    present = _present_paths(vault, [files[file_id]["path"] for file_id in remaining])
    for file_id in remaining:
        if files[file_id]["path"] in present:
            invalid[file_id] = "A selected file is present on the server"
    remaining = [file_id for file_id in remaining if file_id not in invalid]
    duplicates = _duplicate_ids(connection, vault, remaining)
    for file_id in remaining:
        if file_id not in duplicates:
            invalid[file_id] = "No longer a Cloud Duplicate"
    return invalid


def _revoke_members(connection: Any, rows: list[dict[str, Any]], reasons: dict[str, str],
                    files: dict[str, dict[str, Any]], *, at: str) -> None:
    """Cancel some members of an automatic group, each with its reason, and record it."""
    by_reason: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_reason.setdefault(reasons[str(row["file_id"])], []).append(row)
    for reason, members in by_reason.items():
        connection.execute(
            """UPDATE jobs SET status='cancelled', message=%s, message_key=NULL,
                   message_params=NULL, updated_at=%s, finished_at=%s
               WHERE id = ANY(%s::bigint[]) AND status NOT IN ('completed','failed','cancelled')""",
            (f"Skipped: {reason}"[:500], at, at, [int(r["id"]) for r in members]))
    revoke_grants(connection, group_id=str(rows[0]["group_id"]),
                  file_ids=[str(r["file_id"]) for r in rows])
    record_audit_event(
        connection, event="cloud_deletion.files_revoked", actor="system",
        vault_id=int(rows[0]["vault_id"]), outcome="revoked", group_id=str(rows[0]["group_id"]),
        revoked=len(rows), reasons={reason: len(m) for reason, m in by_reason.items()},
        paths=sorted(str((files.get(str(r["file_id"])) or {}).get("path") or "")
                     for r in rows)[:100],
        job_ids=[int(r["id"]) for r in rows][:100])


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
    proof, then completes each Job; a member of an automatic deletion adds to
    the Vault's cleanup totals (#729). The caller holds the Vault row, then
    the group claim.
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
    completed = connection.execute(
        """UPDATE jobs SET status='completed', message='Cloud deletion completed',
               message_key=NULL, message_params=NULL, transferred_bytes=total_bytes,
               updated_at=%s, finished_at=%s
           WHERE id = ANY(%s::bigint[]) AND status NOT IN ('completed','failed','cancelled')
           RETURNING total_bytes""",
        (at, at, ids)).fetchall()
    _count_automatic_deletions(connection, vault_id=int(jobs[0]["vault_id"]),
                               group_id=str(jobs[0]["group_id"]), completed=completed, at=at)
    mark_paths_dirty(connection, int(jobs[0]["vault_id"]), [r["path"] for r in connection.execute(
        "SELECT path FROM files WHERE id = ANY(%s::uuid[])", (files,)).fetchall()])
    record_audit_event(connection, event="cloud_deletion.completed", actor=actor,
                       vault_id=int(jobs[0]["vault_id"]), outcome="success",
                       group_id=str(jobs[0]["group_id"]), deleted=len(ids), job_ids=ids[:100])


def lock_vault_for_totals(connection: Any, *, vault_id: int, group_id: str) -> None:
    """Hold the Vault row before the Jobs of an automatic group (#729).

    Recording one of its members as deleted adds to the Vault's cleanup
    totals; taking the Vault row first keeps the repository-wide lock order
    (Vault row, then Jobs). A manual group touches no Vault row.
    """
    automatic = connection.execute(
        "SELECT automatic FROM cloud_purge_requests WHERE group_id=%s", (group_id,)).fetchone()
    if automatic and automatic["automatic"]:
        lock_vaults_in_order(connection, [vault_id])


def _count_automatic_deletions(connection: Any, *, vault_id: int, group_id: str,
                               completed: list[dict[str, Any]], at: str) -> None:
    """Add the members just completed to the Vault's cleanup totals (#729).

    Only for an automatic request, in the transaction that records them as
    deleted: the totals outlive the history retention of their Jobs. Revoked,
    failed or cancelled members never reach here. The caller holds the Vault
    row first (the repository-wide lock order).
    """
    if not completed:
        return
    connection.execute(
        """UPDATE vaults v SET
               duplicate_cleanup_deleted_count = COALESCE(v.duplicate_cleanup_deleted_count, 0) + %s,
               duplicate_cleanup_deleted_bytes = COALESCE(v.duplicate_cleanup_deleted_bytes, 0) + %s,
               duplicate_cleanup_last_deleted_at = GREATEST(v.duplicate_cleanup_last_deleted_at,
                                                            %s::timestamptz)
           FROM cloud_purge_requests r
           WHERE v.id = %s AND r.group_id = %s AND r.automatic""",
        (len(completed), sum(int(r["total_bytes"] or 0) for r in completed), at,
         vault_id, group_id))


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
