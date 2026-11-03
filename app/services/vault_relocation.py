"""Explicit Reconnect folder (#427), retaining the pinned-root scan machinery.

The historic relocation_state columns now represent the mandatory reconnect
scan gate. An accepted mount fingerprint belongs only to the selected Vault;
shared Source Volume enrollment is never silently replaced.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Callable

from . import source_directories, source_layout, source_identity
from .job_admission import lock_vault
from .directory_aggregates import request_vault_rebuild
from .audit_events import record_audit_event

ROOT_IDENTITY_VERSION = "linux-stat-dir-v1"
_TERMINAL_JOB_STATES = ("completed", "failed", "cancelled")
# Immediate in-process gate closes the validation window before the database
# transaction publishes persistent scan_required state.
_runtime_suspended_vaults: set[int] = set()


class VaultRelocationError(Exception):
    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason


def _identity_from_stat(info: os.stat_result) -> str:
    if not stat.S_ISDIR(info.st_mode):
        raise VaultRelocationError("not_directory", "Destination is not a directory")
    if not info.st_ino or info.st_dev < 0:
        raise VaultRelocationError("identity_ambiguous", "Filesystem has no stable directory identity")
    payload = json.dumps(
        {"dev": int(info.st_dev), "ino": int(info.st_ino), "version": ROOT_IDENTITY_VERSION},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def root_identity(path: str | Path) -> str:
    """Opaque identity for a real directory reached without any symlink.

    Component checks are deliberately fail-closed but are not an atomic path
    lookup. Callers which use the directory after this check must pin it with a
    directory descriptor and verify that descriptor's identity.
    """
    candidate = Path(os.path.abspath(os.fspath(path)))
    current = Path(candidate.anchor)
    try:
        info = current.lstat()
        components = candidate.parts[1:] if candidate.anchor else candidate.parts
        for component in components:
            current /= component
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise VaultRelocationError(
                    "symlink", "Vault root paths cannot contain symbolic links"
                )
    except VaultRelocationError:
        raise
    except OSError as exc:
        raise VaultRelocationError("inaccessible", "Directory identity is inaccessible") from exc
    return _identity_from_stat(info)


def opened_root_identity(fd: int) -> str:
    """Return the identity of an already-open directory descriptor."""
    try:
        return _identity_from_stat(os.fstat(fd))
    except OSError as exc:
        raise VaultRelocationError("inaccessible", "Directory identity is inaccessible") from exc


def enroll_vault_root_identity(connection: Any, vault_id: int, source_root: str) -> str:
    """Enroll identity only for a currently healthy real root."""
    identity = root_identity(source_root)
    connection.execute(
        "UPDATE vaults SET root_identity_version=%s, root_identity=%s WHERE id=%s",
        (ROOT_IDENTITY_VERSION, identity, vault_id),
    )
    return identity


def reconcile_vault_root_identities() -> int:
    """Startup enrollment for legacy healthy roots; missing roots remain ambiguous."""
    from ..database import db

    enrolled = 0
    with db() as connection:
        rows = connection.execute(
            """
            SELECT id, source_root FROM vaults
            WHERE root_identity IS NULL AND root_released_at IS NULL
              AND decommission_state='active' AND relocation_state='ready'
            """
        ).fetchall()
        for row in rows:
            access = source_layout.vault_local_access(row["source_root"])
            if not access.local_operations_allowed:
                continue
            try:
                enroll_vault_root_identity(connection, int(row["id"]), row["source_root"])
            except VaultRelocationError:
                continue
            enrolled += 1
    return enrolled


def _clean_reason(reason: str) -> str:
    value = (reason or "").strip()
    if len(value) < 3 or len(value) > 500:
        raise VaultRelocationError("reason_required", "A reason between 3 and 500 characters is required")
    return value


def _candidate(volume: source_layout.SourceVolume, relative_path: str) -> tuple[str, Path]:
    try:
        relative = source_directories.canonicalize_relative_path(relative_path)
    except source_directories.SourceDirectoryError as exc:
        raise VaultRelocationError("invalid_path", str(exc)) from exc

    # Normalize the untrusted relative path before performing any filesystem
    # probe. A realpath/controlled-prefix guard is intentionally used here so
    # path traversal and every symlinked component fail before the resulting
    # destination reaches is_dir, mount, or access checks.
    try:
        volume_root_text = os.path.realpath(volume.path)
        if relative:
            lexical_target = os.path.abspath(
                os.path.join(volume_root_text, relative)
            )
            resolved_target = os.path.realpath(lexical_target)
            if resolved_target != lexical_target:
                raise VaultRelocationError(
                    "symlink", "Destination path crosses a symbolic link"
                )
            guarded_boundary = volume_root_text.rstrip(os.sep) + os.sep
            if not resolved_target.startswith(guarded_boundary):
                raise VaultRelocationError(
                    "inaccessible",
                    "Destination is inaccessible or outside the Source Volume",
                )
            safe_target = Path(resolved_target)
        else:
            # The empty relative path names the already trusted volume root;
            # never derive it from request data.
            safe_target = Path(volume_root_text)
    except VaultRelocationError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise VaultRelocationError(
            "inaccessible", "Destination is inaccessible or outside the Source Volume"
        ) from exc
    if not safe_target.is_dir():
        raise VaultRelocationError(
            "inaccessible", "Destination is missing or not a directory"
        )

    current = safe_target
    volume_root = Path(volume_root_text)
    while current != volume_root:
        if source_layout.path_is_mount(current):
            raise VaultRelocationError(
                "different_volume", "Destination crosses a nested mount"
            )
        parent = current.parent
        if parent == current:
            raise VaultRelocationError(
                "inaccessible", "Destination is inaccessible or outside the Source Volume"
            )
        current = parent
    if not os.access(
        safe_target, os.R_OK | os.X_OK
    ) or not source_layout.path_is_writable(safe_target):
        raise VaultRelocationError(
            "inaccessible",
            "Destination is not readable, traversable, and writable",
        )
    return relative, safe_target


def _reject_overlap(connection: Any, *, vault_id: int, destination: Path) -> None:
    destination_text = os.path.normpath(str(destination))
    rows = connection.execute(
        """
        SELECT id, source_root FROM vaults
        WHERE id<>%s AND root_released_at IS NULL
        """,
        (vault_id,),
    ).fetchall()
    for row in rows:
        other = os.path.normpath(str(row["source_root"]))
        try:
            common = os.path.commonpath((destination_text, other))
        except ValueError:
            continue
        if common in {destination_text, other}:
            raise VaultRelocationError("overlap", "Destination overlaps another Vault root")


def _active_jobs(connection: Any, vault_id: int) -> bool:
    placeholders = ",".join("%s" for _ in _TERMINAL_JOB_STATES)
    row = connection.execute(
        f"SELECT 1 FROM jobs WHERE vault_id=%s AND status NOT IN ({placeholders}) LIMIT 1",
        (vault_id, *_TERMINAL_JOB_STATES),
    ).fetchone()
    return row is not None


def _reconnect_vault_root(
    connection: Any,
    *,
    vault_id: int,
    volume_alias: str,
    relative_path: str,
    actor_user_id: int,
    reason: str,
    runtime_busy: bool = False,
    after_update: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Validate and atomically enter the mandatory-full-scan recovery state.

    ``after_update`` is a narrow test/integration seam.  If watcher/scan handoff
    raises before commit, the transaction rolls back both ``source_root`` and
    suspension state.
    """
    cleaned_reason = _clean_reason(reason)
    source_directories.lock_vault_root_allocation(connection)
    try:
        lock_vault(connection, vault_id)
    except LookupError as exc:
        reason = "not_found" if str(exc) == "vault_not_found" else "decommission_in_progress"
        raise VaultRelocationError(reason) from exc
    vault = connection.execute("SELECT * FROM vaults WHERE id=%s", (vault_id,)).fetchone()
    if vault.get("decommission_state", "active") != "active" or vault.get("root_released_at"):
        raise VaultRelocationError(
            "decommission_in_progress",
            "A decommissioned or decommissioning Vault cannot be reconnected",
        )
    if runtime_busy or _active_jobs(connection, vault_id):
        raise VaultRelocationError("active_jobs", "Vault has active Jobs or a scan")
    if (not volume_alias or volume_alias == source_layout.MANAGED_DIR_NAME
            or Path(volume_alias).parts != (volume_alias,)
            or not source_layout.sources_layout_is_ready()):
        raise VaultRelocationError("volume_unavailable", "Destination Source Volume is unavailable")
    volumes = {item.alias: item for item in source_layout.discover_source_volumes()}
    volume = volumes.get(volume_alias)
    if volume is None or volume.health != "ok" or volume.access != "rw":
        raise VaultRelocationError("volume_unavailable", "Destination Source Volume is unavailable")
    # This is the ONLY explicit acceptance seam. Ambiguous/unsupported mount
    # identity still fails closed, before traversing any candidate content.
    try:
        fingerprint = source_identity.fingerprint_for_mount(volume.path)
    except source_identity.MountIdentityError as exc:
        raise VaultRelocationError("identity_ambiguous", "Source Volume identity is unavailable") from exc
    _, destination = _candidate(volume, relative_path)
    _reject_overlap(connection, vault_id=vault_id, destination=destination)
    identity = root_identity(destination)
    if source_identity.fingerprint_for_mount(volume.path) != fingerprint:
        raise VaultRelocationError("identity_mismatch", "Source Volume changed during reconnect")

    previous_root = str(vault["source_root"])
    connection.execute(
        """
        UPDATE vaults
        SET source_root=%s, relocation_state='scan_required', relocation_previous_root=%s,
            root_identity_version=%s, root_identity=%s, reconnect_volume_fingerprint=%s
        WHERE id=%s AND source_root=%s
        """,
        (str(destination), previous_root, ROOT_IDENTITY_VERSION, identity, fingerprint,
         vault_id, previous_root),
    )
    if after_update is not None:
        after_update()
    record_audit_event(
        connection,
        event="vault_folder_reconnected",
        actor_user_id=actor_user_id,
        vault_id=vault_id,
        outcome="success",
        visibility="owner",
        reason=cleaned_reason,
        previous_source_root=previous_root,
        source_root=str(destination),
        mandatory_full_scan=True,
    )
    return connection.execute("SELECT * FROM vaults WHERE id=%s", (vault_id,)).fetchone()


def reconnect_vault_root(connection: Any, **kwargs: Any) -> dict[str, Any]:
    """Suspend immediately, then validate; failures release the runtime gate."""
    vault_id = int(kwargs["vault_id"])
    _runtime_suspended_vaults.add(vault_id)
    succeeded = False
    try:
        result = _reconnect_vault_root(connection, **kwargs)
        succeeded = True
        return result
    finally:
        if not succeeded:
            _runtime_suspended_vaults.discard(vault_id)


def prepare_reconnect_local_scan(connection: Any, vault_id: int) -> None:
    """Discard every old local observation; identical stat tuples prove nothing.

    This is repeatable on retry. Remote reconciliation/verification must have
    succeeded first. Every current cloud copy is held before re-observation.
    """
    connection.execute(
        """
        UPDATE local_copies lc SET presence='missing', plaintext_sha256=NULL,
            matched_archive_version_id=NULL, cloud_only_version_id=(
                SELECT av.id FROM archive_versions av
                WHERE av.vault_file_id=lc.vault_file_id AND av.availability='available'
                  AND av.integrity='verified'
                ORDER BY av.version_number DESC LIMIT 1
            )
        WHERE lc.vault_file_id IN (SELECT id FROM vault_files WHERE vault_id=%s)
        """, (vault_id,),
    )
    request_vault_rebuild(connection, vault_id)


def complete_relocation_scan(
    connection: Any, vault_id: int, *, release_runtime: bool = True
) -> None:
    connection.execute(
        """
        UPDATE vaults SET relocation_state='ready', relocation_previous_root=NULL
        WHERE id=%s AND relocation_state='scan_required'
        """,
        (vault_id,),
    )
    if release_runtime:
        release_relocation_scan_runtime(vault_id)


def release_relocation_scan_runtime(vault_id: int) -> None:
    """Release the process gate only after recovery state has committed."""
    _runtime_suspended_vaults.discard(int(vault_id))


def local_work_suspended(vault: dict[str, Any]) -> bool:
    vault_id = vault.get("id", vault.get("vault_id"))
    return (
        (vault_id is not None and int(vault_id) in _runtime_suspended_vaults)
        or str(vault.get("relocation_state") or "ready") != "ready"
    )
