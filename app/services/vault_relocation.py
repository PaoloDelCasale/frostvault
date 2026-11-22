"""Explicit Reconnect folder (#427), retaining the pinned-root scan machinery.

``vaults.lifecycle='reconnect_required'`` is the mandatory reconnect scan
gate (ADR-0035); the previous root is kept only in the audit event. An accepted mount fingerprint belongs only to the selected Vault;
shared Source Volume enrollment is never silently replaced.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Callable

from . import bulk_upload_admission, source_directories, source_layout, source_identity, write_access
from .directory_aggregates import request_vault_rebuild
from .audit_events import record_audit_event

# v2 (#784, ADR-0054): the device number counts only for a real block device.
# An anonymous device (major 0: FUSE/shfs, NFS, ZFS, btrfs subvolumes, overlay)
# is renumbered at mount time, so its identity is the inode alone; the
# filesystem itself stays guarded by the Source Volume Identity.
ROOT_IDENTITY_VERSION = "linux-stat-dir-v2"
LEGACY_ROOT_IDENTITY_VERSION = "linux-stat-dir-v1"
# Every anonymous device number the kernel can hand out: major 0, 20-bit minor.
_ANONYMOUS_MINORS = 1 << 20
_TERMINAL_JOB_STATES = ("completed", "failed", "cancelled")
# Immediate in-process gate closes the validation window before the database
# transaction publishes persistent scan_required state.
_runtime_suspended_vaults: set[int] = set()


class VaultRelocationError(Exception):
    def __init__(self, reason: str, message: str | None = None,
                 params: dict[str, Any] | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason
        self.params = dict(params or {})


def _not_writable(reason: str, directory: Path) -> VaultRelocationError:
    """Say that FrostVault's own user may not write there, and whose it is (#681)."""
    error = write_access.DestinationNotWritable(directory)
    return VaultRelocationError(reason, str(error), params=error.message_params)


def _identity_payload(info: os.stat_result) -> dict[str, Any]:
    if not stat.S_ISDIR(info.st_mode):
        raise VaultRelocationError("not_directory", "Destination is not a directory")
    if not info.st_ino or info.st_dev < 0:
        raise VaultRelocationError("identity_ambiguous", "Filesystem has no stable directory identity")
    return {"dev": int(info.st_dev), "ino": int(info.st_ino)}


def device_is_anonymous(st_dev: int) -> bool:
    """True for a device number the kernel assigns at mount time (major 0)."""
    return os.major(int(st_dev)) == 0


def _identity_from_stat(info: os.stat_result) -> str:
    fields = _identity_payload(info)
    # An explicit null keeps the two shapes distinct: no real device has its
    # number omitted, no anonymous one has it recorded.
    dev = None if device_is_anonymous(fields["dev"]) else fields["dev"]
    payload = json.dumps(
        {"dev": dev, "ino": fields["ino"], "version": ROOT_IDENTITY_VERSION},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _legacy_identity(dev: int, ino: int) -> str:
    """The v1 identity, byte for byte as ``json.dumps`` wrote it (a test pins this)."""
    return hashlib.sha256(
        b'{"dev":%d,"ino":%d,"version":"linux-stat-dir-v1"}' % (dev, ino)
    ).hexdigest()


def _root_stat(path: str | Path) -> os.stat_result:
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
    return info


def root_identity(path: str | Path) -> str:
    """Opaque identity for a real directory reached without any symlink.

    Component checks are deliberately fail-closed but are not an atomic path
    lookup. Callers which use the directory after this check must pin it with a
    directory descriptor and verify that descriptor's identity.
    """
    return _identity_from_stat(_root_stat(path))


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
        "UPDATE vaults SET root_identity_version=%s, root_identity=%s,"
        " legacy_root_identity=NULL WHERE id=%s",
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
              AND lifecycle='active'
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


# Legacy (v1) identities, #784 and ADR-0054. The verified recovery searches
# every anonymous device number, so it runs at most once per Vault per process.
_legacy_recovery_attempted: set[int] = set()
# A legacy identity whose Source Volume was not healthy waits for a later pass.
_legacy_upgrade_pending = False


def _anonymous_devices_reproducing(stored: str, ino: int) -> list[int]:
    """Every anonymous device number whose v1 payload with ``ino`` hashes to ``stored``."""
    try:
        target = bytes.fromhex(stored)
    except (TypeError, ValueError):
        return []
    if len(target) != hashlib.sha256().digest_size:
        return []
    sha256 = hashlib.sha256
    makedev = os.makedev
    tail = b',"ino":%d,"version":"linux-stat-dir-v1"}' % ino
    return [
        dev for dev in (makedev(0, minor) for minor in range(_ANONYMOUS_MINORS))
        if sha256(b'{"dev":%d' % dev + tail).digest() == target
    ]


def _device_label(st_dev: int) -> str:
    return f"{os.major(st_dev)}:{os.minor(st_dev)}"


def _upgrade_legacy_root_identity(row: dict[str, Any]) -> str:
    """Upgrade one v1 identity; returns upgraded, recovered, unmatched or deferred."""
    from ..database import db

    vault_id = int(row["id"])
    source_root = str(row["source_root"])
    stored = str(row["root_identity"])
    # The scan gate's own condition: a Source Volume whose identity holds but
    # which waits for a full scan is healthy here, or that scan could never run.
    access = source_layout.vault_local_access(source_root)
    if not access.local_operations_allowed and access.volume_health != "scan_required":
        return "deferred"
    try:
        info = _root_stat(source_root)
        fields = _identity_payload(info)
        identity = _identity_from_stat(info)
    except VaultRelocationError as exc:
        # Unreadable now may be readable later; a symlink or a file in place of
        # the folder is an observed replacement and stays blocked.
        return "deferred" if exc.reason == "inaccessible" else "unmatched"
    previous_device: int | None = None
    if _legacy_identity(fields["dev"], fields["ino"]) != stored:
        if not device_is_anonymous(fields["dev"]) or vault_id in _legacy_recovery_attempted:
            return "unmatched"
        _legacy_recovery_attempted.add(vault_id)
        # Only st_dev changed? Then exactly one anonymous device number, with
        # the current inode, reproduces the stored bytes.
        devices = _anonymous_devices_reproducing(stored, fields["ino"])
        if len(devices) != 1:
            return "unmatched"
        previous_device = devices[0]
    with db() as connection:
        try:
            # The folder must still be the one just verified when it is written.
            if root_identity(source_root) != identity:
                return "unmatched"
        except VaultRelocationError:
            return "deferred"
        before = connection.execute(
            "SELECT * FROM vaults WHERE id=%s FOR NO KEY UPDATE", (vault_id,),
        ).fetchone()
        updated = connection.execute(
            """
            UPDATE vaults SET root_identity_version=%s, root_identity=%s,
                legacy_root_identity=root_identity
            WHERE id=%s AND source_root=%s AND root_identity_version=%s
              AND root_identity=%s AND root_released_at IS NULL
            RETURNING *
            """,
            (ROOT_IDENTITY_VERSION, identity, vault_id, source_root,
             LEGACY_ROOT_IDENTITY_VERSION, stored),
        ).fetchone()
        if before is None or updated is None:
            return "unmatched"
        # A pending manual bulk upload was admitted for this same root: it
        # keeps going instead of being invalidated as a changed Vault.
        bulk_upload_admission.carry_vault_identity(connection, before, updated)
        if previous_device is None:
            return "upgraded"
        record_audit_event(
            connection,
            event="vault_root_identity_recovered",
            actor="system",
            vault_id=vault_id,
            outcome="success",
            source_root=source_root,
            verified="device_number_only",
            previous_device=_device_label(previous_device),
            current_device=_device_label(fields["dev"]),
            previous_identity_version=LEGACY_ROOT_IDENTITY_VERSION,
            identity_version=ROOT_IDENTITY_VERSION,
        )
    return "recovered"


def upgrade_legacy_root_identities() -> dict[str, int]:
    """Rewrite persisted v1 identities as v2 when the root is provably the same.

    Runs at startup, before scans, workers and watchers, and may run again: a
    rewritten identity is no longer v1. A Vault whose Source Volume is not
    healthy is left for a later pass. An identity that no current fact
    reproduces stays as it is, and blocked: Reconnect Folder recovers it.
    """
    global _legacy_upgrade_pending
    from ..database import db

    with db() as connection:
        rows = connection.execute(
            """
            SELECT id, source_root, root_identity FROM vaults
            WHERE root_identity_version=%s AND root_identity IS NOT NULL
              AND root_released_at IS NULL
              AND lifecycle IN ('active', 'reconnect_required')
            ORDER BY id
            """,
            (LEGACY_ROOT_IDENTITY_VERSION,),
        ).fetchall()
    counts = {"upgraded": 0, "recovered": 0, "unmatched": 0, "deferred": 0}
    for row in rows:
        counts[_upgrade_legacy_root_identity(dict(row))] += 1
    _legacy_upgrade_pending = counts["deferred"] > 0
    return counts


def upgrade_pending_legacy_root_identities() -> dict[str, int] | None:
    """Retry the upgrade only while a v1 Vault waited for its Source Volume."""
    if not _legacy_upgrade_pending:
        return None
    return upgrade_legacy_root_identities()


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
    with source_layout.mount_points_once():
        while current != volume_root:
            if source_layout.path_is_mount(current):
                raise VaultRelocationError(
                    "different_volume", "Destination crosses a nested mount"
                )
            parent = current.parent
            if parent == current:
                raise VaultRelocationError(
                    "inaccessible",
                    "Destination is inaccessible or outside the Source Volume",
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
    from .bulk_upload_admission import has_pending
    if has_pending(connection, vault_id):
        return True
    placeholders = ",".join("%s" for _ in _TERMINAL_JOB_STATES)
    row = connection.execute(
        f"SELECT 1 FROM jobs WHERE vault_id=%s AND status NOT IN ({placeholders}) LIMIT 1",
        (vault_id, *_TERMINAL_JOB_STATES),
    ).fetchone()
    return row is not None


def _source_volume_destination(
    connection: Any, *, vault_id: int, volume_alias: str, relative_path: str,
) -> tuple[Path, str, str]:
    """Validate a directory on a custom Source Volume and its mount identity."""
    volumes = {item.alias: item for item in source_layout.discover_source_volumes()}
    volume = volumes.get(volume_alias)
    if (volume is not None and volume.health == "read_only"
            and not source_layout.path_is_read_only_mount(volume.path)):
        # Mounted read-write, but its root denies FrostVault's identity.
        raise _not_writable("volume_not_writable", Path(volume.path))
    if volume is None or volume.health != "ok" or volume.access != "rw":
        raise VaultRelocationError("volume_unavailable", "Destination Source Volume is unavailable")
    # This is the ONLY explicit acceptance seam. Ambiguous/unsupported mount
    # identity still fails closed, before traversing any candidate content.
    try:
        fingerprint = source_identity.fingerprint_for_mount(volume.path)
    except source_identity.MountIdentityError as exc:
        raise VaultRelocationError("identity_ambiguous", "Source Volume identity is unavailable") from exc
    _, destination = _candidate(volume, relative_path)
    try:
        write_access.require_writable_directory(destination)
    except write_access.DestinationNotWritable:
        raise _not_writable("destination_not_writable", destination) from None
    _reject_overlap(connection, vault_id=vault_id, destination=destination)
    identity = root_identity(destination)
    try:
        source_identity.validate_root_mount_boundary(volume.path, destination)
        current_fingerprint = source_identity.fingerprint_for_mount(volume.path)
    except source_identity.MountIdentityError as exc:
        raise VaultRelocationError("identity_ambiguous", "Source Volume boundary is unavailable") from exc
    if current_fingerprint != fingerprint:
        raise VaultRelocationError("identity_mismatch", "Source Volume changed during reconnect")
    return destination, identity, fingerprint


def _own_managed_destination(
    connection: Any, vault: dict[str, Any], relative_path: str,
) -> tuple[Path, str]:
    """Accept only this Vault's own ``/sources/managed/<uuid>`` folder (#641).

    The managed folder lives inside the ``/sources`` mount, not on a mount of
    its own: the boundary is that mount. Its fingerprint must be available and
    unchanged across the check, and no nested mount may intersect the folder.
    The folder is never created: a missing one is an explicit refusal.
    """
    vault_uuid = str(vault["uuid"])
    own = source_layout.managed_vault_path(vault_uuid)
    current = os.path.normpath(str(vault["source_root"]))
    if current != os.path.normpath(str(own)) or (relative_path or "").strip() != vault_uuid:
        raise VaultRelocationError(
            "managed_not_own",
            "Only this Vault's own managed folder can be reconnected",
        )
    sources = source_layout.get_sources_root()
    try:
        fingerprint = source_identity.fingerprint_for_mount(sources)
    except source_identity.MountIdentityError as exc:
        raise VaultRelocationError("identity_ambiguous", "Source Volume identity is unavailable") from exc
    managed = source_layout.get_managed_root()
    try:
        managed_info = managed.lstat()
        own_info = own.lstat()
    except FileNotFoundError as exc:
        raise VaultRelocationError("managed_missing", "Managed folder missing") from exc
    except OSError as exc:
        raise VaultRelocationError("inaccessible", "Destination is inaccessible") from exc
    for info in (managed_info, own_info):
        if not stat.S_ISDIR(info.st_mode):
            raise VaultRelocationError(
                "managed_missing", "Managed folder missing or not a real directory",
            )
    with source_layout.mount_points_once():
        nested = source_layout.path_is_mount(managed) or source_layout.path_is_mount(own)
    if nested:
        raise VaultRelocationError("different_volume", "Destination crosses a nested mount")
    if not os.access(own, os.R_OK | os.X_OK) or not source_layout.path_is_writable(own):
        raise VaultRelocationError(
            "inaccessible", "Destination is not readable, traversable, and writable",
        )
    _reject_overlap(connection, vault_id=int(vault["id"]), destination=own)
    identity = root_identity(own)
    try:
        source_identity.validate_root_mount_boundary(sources, own)
        current_fingerprint = source_identity.fingerprint_for_mount(sources)
    except source_identity.MountIdentityError as exc:
        raise VaultRelocationError("identity_ambiguous", "Source Volume boundary is unavailable") from exc
    if current_fingerprint != fingerprint:
        raise VaultRelocationError("identity_mismatch", "Source Volume changed during reconnect")
    return own, identity


def _reconnect_vault_root(
    connection: Any,
    *,
    vault_id: int,
    volume_alias: str,
    relative_path: str,
    actor: str,
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
    # The admission fence of lock_vault, but a Vault already waiting for its
    # Reconnect (a failed earlier attempt, a metadata restore) may reconnect.
    vault = connection.execute(
        "SELECT * FROM vaults WHERE id=%s FOR NO KEY UPDATE", (vault_id,),
    ).fetchone()
    if vault is None:
        raise VaultRelocationError("not_found")
    if vault["lifecycle"] not in {"active", "reconnect_required"} or vault.get("root_released_at"):
        raise VaultRelocationError(
            "protection_stopped",
            "A folder whose protection has stopped cannot be reconnected",
        )
    if runtime_busy or _active_jobs(connection, vault_id):
        raise VaultRelocationError("active_jobs", "Vault has active Jobs or a scan")
    if (not volume_alias or Path(volume_alias).parts != (volume_alias,)
            or not source_layout.sources_layout_is_ready()):
        raise VaultRelocationError("volume_unavailable", "Destination Source Volume is unavailable")
    if volume_alias == source_layout.MANAGED_DIR_NAME:
        destination, identity = _own_managed_destination(connection, vault, relative_path)
        # A managed root is no Source Volume: its local access never consults
        # a per-Vault mount acceptance, exactly as for a newly created Vault.
        fingerprint = None
    else:
        destination, identity, fingerprint = _source_volume_destination(
            connection, vault_id=vault_id, volume_alias=volume_alias,
            relative_path=relative_path,
        )

    previous_root = str(vault["source_root"])
    connection.execute(
        """
        UPDATE vaults
        SET source_root=%s, lifecycle='reconnect_required',
            root_identity_version=%s, root_identity=%s, legacy_root_identity=NULL,
            reconnect_volume_fingerprint=%s
        WHERE id=%s AND source_root=%s
        """,
        (str(destination), ROOT_IDENTITY_VERSION, identity, fingerprint,
         vault_id, previous_root),
    )
    if after_update is not None:
        after_update()
    record_audit_event(
        connection,
        event="vault_folder_reconnected",
        actor=actor,
        vault_id=vault_id,
        outcome="success",
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
    succeeded first. The ordinary missing-to-present observation installs the
    current cloud hold; files that remain absent must not acquire a conflict.
    """
    # A new generation invalidates every in-flight digest of the old root.
    connection.execute(
        """
        UPDATE files SET local_presence='missing', local_sha256=NULL,
            local_part_size=NULL, local_part_sha256=NULL,
            matched_version_id=NULL, held_version_id=NULL, generation=generation + 1
        WHERE vault_id=%s AND local_presence IS NOT NULL
        """, (vault_id,),
    )
    request_vault_rebuild(connection, vault_id)


def complete_relocation_scan(
    connection: Any, vault_id: int, *, release_runtime: bool = True
) -> None:
    connection.execute(
        """
        UPDATE vaults SET lifecycle='active'
        WHERE id=%s AND lifecycle='reconnect_required'
        """,
        (vault_id,),
    )
    # The first complete Reconnect after a metadata restore ends its cloud
    # reconciliation; later reconnects never adopt cloud versions.
    connection.execute("DELETE FROM metadata_restore_vaults WHERE vault_id=%s", (vault_id,))
    if release_runtime:
        release_relocation_scan_runtime(vault_id)


def release_relocation_scan_runtime(vault_id: int) -> None:
    """Release the process gate only after recovery state has committed."""
    _runtime_suspended_vaults.discard(int(vault_id))


def local_work_suspended(vault: dict[str, Any]) -> bool:
    vault_id = vault.get("id", vault.get("vault_id"))
    return (
        (vault_id is not None and int(vault_id) in _runtime_suspended_vaults)
        or str(vault.get("lifecycle") or vault.get("vault_lifecycle") or "active") == "reconnect_required"
    )
