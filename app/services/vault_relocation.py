"""Fail-closed Vault Root Relocation within one Source Volume (issue #152).

A relocation is not a generic rebind.  FrostVault accepts a destination only
when its opaque ``stat`` identity matches the identity enrolled while the old
root was healthy.  On filesystems where directory inode identity is absent or
unstable, relocation is intentionally unavailable rather than guessing from
names or content samples.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Callable

from . import source_areas, source_layout
from .audit_events import record_audit_event
from .notifications import enqueue_notification

ROOT_IDENTITY_VERSION = "linux-stat-dir-v1"
_TERMINAL_JOB_STATES = ("completed", "failed", "cancelled")
# Immediate in-process gate closes the validation window before the database
# transaction publishes persistent scan_required state.
_runtime_suspended_vaults: set[int] = set()


class VaultRelocationError(Exception):
    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason


def root_identity(path: str | Path) -> str:
    """Opaque identity for one real directory; never follows the final symlink."""
    candidate = Path(path)
    try:
        info = candidate.lstat()
    except OSError as exc:
        raise VaultRelocationError("inaccessible", "Directory identity is inaccessible") from exc
    if stat.S_ISLNK(info.st_mode):
        raise VaultRelocationError("symlink", "Vault roots cannot be symbolic links")
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
            "SELECT id, source_root FROM vaults WHERE root_identity IS NULL"
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


def _alias_for_root(root: str) -> str:
    location, alias = source_layout.source_alias_for_root(root)
    if location != "custom" or not alias:
        raise VaultRelocationError("unsupported_root", "Only custom Source Volume roots can be relocated")
    return alias


def _candidate(volume: source_layout.SourceVolume, relative_path: str) -> tuple[str, Path]:
    try:
        relative = source_areas.canonicalize_relative_path(relative_path)
    except source_areas.SourceAreaError as exc:
        raise VaultRelocationError("invalid_path", str(exc)) from exc
    target = Path(volume.path)
    for part in relative.split("/") if relative else ():
        target = target / part
        if source_layout.path_is_symlink(target):
            raise VaultRelocationError("symlink", "Destination path crosses a symbolic link")
    if source_layout.path_is_symlink(target):
        raise VaultRelocationError("symlink", "Destination is a symbolic link")
    if not target.is_dir():
        raise VaultRelocationError("inaccessible", "Destination is missing or not a directory")
    try:
        resolved = target.resolve(strict=True)
        resolved.relative_to(Path(volume.path).resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise VaultRelocationError("inaccessible", "Destination is inaccessible or outside the Source Volume") from exc
    current = resolved
    volume_root = Path(volume.path).resolve()
    while current != volume_root:
        if source_layout.path_is_mount(current):
            raise VaultRelocationError("different_volume", "Destination crosses a nested mount")
        current = current.parent
    if not os.access(resolved, os.R_OK | os.X_OK) or not source_layout.path_is_writable(resolved):
        raise VaultRelocationError("inaccessible", "Destination is not readable, traversable, and writable")
    return relative, resolved


def _reject_overlap(connection: Any, *, vault_id: int, destination: Path) -> None:
    destination_text = os.path.normpath(str(destination))
    rows = connection.execute("SELECT id, source_root FROM vaults WHERE id<>%s", (vault_id,)).fetchall()
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


def _relocate_vault_root(
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
    source_areas._lock_source_area_mutations(connection)
    vault = connection.execute("SELECT * FROM vaults WHERE id=%s", (vault_id,)).fetchone()
    if vault is None:
        raise VaultRelocationError("not_found", "Vault not found")
    if vault.get("relocation_state") != "ready":
        raise VaultRelocationError("relocation_in_progress", "Vault relocation already requires recovery")
    expected_alias = _alias_for_root(str(vault["source_root"]))
    if volume_alias != expected_alias:
        raise VaultRelocationError("different_volume", "Destination must remain on the same Source Volume")
    try:
        Path(str(vault["source_root"])).lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise VaultRelocationError(
            "source_inaccessible",
            "Original root cannot be proven missing; relocation is refused",
        ) from exc
    else:
        raise VaultRelocationError("source_present", "Original root still exists; generic rebind is forbidden")
    if runtime_busy or _active_jobs(connection, vault_id):
        raise VaultRelocationError("active_jobs", "Vault has active Jobs or a scan")
    if vault.get("root_identity_version") != ROOT_IDENTITY_VERSION or not vault.get("root_identity"):
        raise VaultRelocationError("identity_ambiguous", "No verified identity was enrolled before the move")

    access = source_layout.vault_local_access(source_layout.get_sources_root() / expected_alias)
    if not access.local_operations_allowed:
        raise VaultRelocationError("volume_unavailable", "Expected Source Volume is not healthy")
    volumes = {item.alias: item for item in source_layout.discover_source_volumes()}
    volume = volumes.get(expected_alias)
    if volume is None or volume.health != "ok" or volume.access != "rw":
        raise VaultRelocationError("volume_unavailable", "Expected Source Volume is unavailable")
    _, destination = _candidate(volume, relative_path)
    _reject_overlap(connection, vault_id=vault_id, destination=destination)
    if root_identity(destination) != vault["root_identity"]:
        raise VaultRelocationError("identity_mismatch", "Destination is not the enrolled Vault directory")

    previous_root = str(vault["source_root"])
    connection.execute(
        """
        UPDATE vaults
        SET source_root=%s, relocation_state='scan_required', relocation_previous_root=%s
        WHERE id=%s AND source_root=%s AND relocation_state='ready'
        """,
        (str(destination), previous_root, vault_id, previous_root),
    )
    if after_update is not None:
        after_update()
    owner = connection.execute(
        "SELECT user_id FROM vault_members WHERE vault_id=%s AND role='owner' LIMIT 1",
        (vault_id,),
    ).fetchone()
    record_audit_event(
        connection,
        event="vault_root_relocated",
        actor_user_id=actor_user_id,
        vault_id=vault_id,
        outcome="success",
        visibility="owner",
        reason=cleaned_reason,
        admin_override=True,
        previous_source_root=previous_root,
        source_root=str(destination),
        mandatory_full_scan=True,
    )
    if owner:
        enqueue_notification(
            connection,
            user_id=int(owner["user_id"]),
            vault_id=vault_id,
            event="vault_root_relocated",
            title="Administrator action: Vault root relocated",
            body=cleaned_reason,
            channels=("in_app",),
        )
    return connection.execute("SELECT * FROM vaults WHERE id=%s", (vault_id,)).fetchone()


def relocate_vault_root(connection: Any, **kwargs: Any) -> dict[str, Any]:
    """Suspend immediately, then validate; failures release the runtime gate."""
    vault_id = int(kwargs["vault_id"])
    _runtime_suspended_vaults.add(vault_id)
    succeeded = False
    try:
        result = _relocate_vault_root(connection, **kwargs)
        succeeded = True
        return result
    finally:
        if not succeeded:
            _runtime_suspended_vaults.discard(vault_id)


def complete_relocation_scan(connection: Any, vault_id: int) -> None:
    connection.execute(
        """
        UPDATE vaults SET relocation_state='ready', relocation_previous_root=NULL
        WHERE id=%s AND relocation_state='scan_required'
        """,
        (vault_id,),
    )
    _runtime_suspended_vaults.discard(int(vault_id))


def local_work_suspended(vault: dict[str, Any]) -> bool:
    vault_id = vault.get("id", vault.get("vault_id"))
    return (
        (vault_id is not None and int(vault_id) in _runtime_suspended_vaults)
        or str(vault.get("relocation_state") or "ready") != "ready"
    )
