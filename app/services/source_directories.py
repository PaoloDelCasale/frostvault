"""Vault-root discovery and adoption under the Source Volumes.

The single owner may choose any healthy, writable directory inside a custom
Source Volume as a Vault root (#412, #366 Q2). The only constraints are
technical: no symlinks, no nested mounts, no escape from the volume, no
overlap with another Vault root, and an accessible root.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import source_layout

# Advisory lock key that serializes Vault-root allocation, relocation, and
# root release so two concurrent requests cannot claim overlapping roots.
_VAULT_ROOT_LOCK_KEY = 0x534F555243415245  # "SOURCARE"


class SourceDirectoryError(Exception):
    """A recoverable failure while resolving a Source Volume directory.

    ``reason`` is a short machine-readable code callers may surface safely.
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason


def paths_overlap(left: str, right: str) -> bool:
    """True when two volume-relative paths conflict by equality or ancestry."""
    if left == right:
        return True
    if left == "" or right == "":
        return True
    return left.startswith(right + "/") or right.startswith(left + "/")


def lock_vault_root_allocation(connection: Any) -> None:
    """Serialize Vault-root allocation against concurrent overlap races.

    A transaction advisory lock is shared by creation, relocation, and root release.
    """
    connection.execute(
        "SELECT pg_advisory_xact_lock(%s)",
        (_VAULT_ROOT_LOCK_KEY,),
    )


def canonicalize_relative_path(relative_path: str) -> str:
    """Normalize a path relative to a Source Volume root.

    Empty string means the volume root itself. Rejects empty segments,
    ``.`` / ``..``, backslashes, and absolute forms.
    """
    raw = (relative_path or "").strip().replace("\\", "/")
    if "\x00" in raw:
        raise SourceDirectoryError("invalid_path", "Source directory path contains invalid data")
    if raw in ("", "."):
        return ""
    if raw.startswith("/"):
        raise SourceDirectoryError("invalid_path", "Source directory path must be relative")
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise SourceDirectoryError("invalid_path", "Source directory path must not escape")
        parts.append(part)
    return "/".join(parts)


def _volume_or_raise(volume_alias: str) -> source_layout.SourceVolume:
    alias = (volume_alias or "").strip()
    if (
        not alias
        or alias == source_layout.MANAGED_DIR_NAME
        or Path(alias).parts != (alias,)
    ):
        raise SourceDirectoryError("invalid_volume", "Source Volume is not usable for Vault roots")
    # Gate identity and volume health before discovering or resolving a
    # candidate directory. A replacement must not be traversed by an
    # adoption or browser request.
    identity_access = source_layout.vault_local_access(
        source_layout.get_sources_root() / alias
    )
    if identity_access.volume_health == "absent":
        raise SourceDirectoryError("volume_not_found", f"Source Volume '{alias}' was not found")
    if not identity_access.local_operations_allowed:
        raise SourceDirectoryError(
            "volume_unavailable",
            f"Source Volume '{alias}' is not healthy",
        )
    volumes = {volume.alias: volume for volume in source_layout.discover_source_volumes()}
    volume = volumes.get(alias)
    if volume is None:
        raise SourceDirectoryError("volume_not_found", f"Source Volume '{alias}' was not found")
    if volume.health != "ok" or volume.access != "rw":
        raise SourceDirectoryError(
            "volume_unavailable",
            f"Source Volume '{alias}' is not healthy",
        )
    return volume


def _resolve_directory(volume: source_layout.SourceVolume, relative_path: str) -> Path:
    root = Path(volume.path)
    target = root if relative_path == "" else root.joinpath(*relative_path.split("/"))
    if source_layout.path_is_symlink(target):
        raise SourceDirectoryError("invalid_path", "Source directory must not be a symlink")
    if not target.is_dir():
        raise SourceDirectoryError("path_missing", "Source directory does not exist")
    try:
        resolved = target.resolve()
        resolved.relative_to(Path(volume.path).resolve())
    except ValueError as exc:
        raise SourceDirectoryError(
            "invalid_path",
            "Source directory must stay inside its Source Volume",
        ) from exc
    return resolved


def _occupied_vault_roots(
    connection: Any,
    volume: source_layout.SourceVolume,
) -> list[dict[str, Any]]:
    """Vault roots under ``volume``, including disabled/unavailable Vaults."""
    # Filter by the lexical namespace before canonicalizing any stored root;
    # a different, currently replaced Source Volume must not be resolved just
    # because the owner is browsing this healthy volume.
    volume_path = Path(volume.path).resolve()
    lexical_volume_path = Path(os.path.normpath(volume.path))
    rows = connection.execute(
        """
        SELECT id, name, source_root FROM vaults
        WHERE root_released_at IS NULL
        """
    ).fetchall()
    occupied: list[dict[str, Any]] = []
    for row in rows:
        configured_root = Path(os.path.normpath(str(row["source_root"])))
        try:
            configured_root.relative_to(lexical_volume_path)
        except ValueError:
            continue
        try:
            root = configured_root.resolve()
            relative = root.relative_to(volume_path).as_posix()
        except ValueError:
            continue
        if relative == ".":
            relative = ""
        occupied.append({"relative_path": relative, "vault_name": row["name"]})
    return occupied


def _occupation_for_path(
    relative_path: str,
    occupied: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for item in occupied:
        if item["relative_path"] == relative_path:
            return {"kind": "vault", "vault_name": item["vault_name"]}
    return None


def _has_occupied_descendant(relative_path: str, occupied: list[dict[str, Any]]) -> bool:
    prefix = "" if relative_path == "" else relative_path + "/"
    for item in occupied:
        path = item["relative_path"]
        if path == relative_path:
            continue
        if relative_path == "" or path.startswith(prefix):
            return True
    return False


def _assert_no_nested_mount(volume: source_layout.SourceVolume, target: Path) -> None:
    """Reject candidates that cross a mount nested under the Source Volume."""
    volume_root = Path(volume.path).resolve()
    current = target.resolve()
    while current != volume_root:
        if source_layout.path_is_mount(current):
            raise SourceDirectoryError(
                "nested_mount",
                "Adoption cannot cross a nested mount point",
            )
        parent = current.parent
        if parent == current:
            break
        current = parent


def _assert_root_access(target: Path) -> None:
    """Immediate root preflight: list/traverse/write before commit."""
    if not os.access(target, os.R_OK | os.X_OK):
        raise SourceDirectoryError(
            "unreadable",
            "Adoption root is not listable or traversable",
        )
    if not source_layout.path_is_writable(target):
        raise SourceDirectoryError(
            "unwritable",
            "Adoption root is not writable",
        )


def _assert_no_vault_overlap(
    connection: Any,
    volume: source_layout.SourceVolume,
    canonical: str,
) -> None:
    """Reject exact, candidate-inside-existing, and candidate-contains-existing."""
    for item in _occupied_vault_roots(connection, volume):
        if paths_overlap(canonical, item["relative_path"]):
            raise SourceDirectoryError(
                "overlap",
                "Adoption path overlaps an existing Vault root",
            )


def resolve_adoption_candidate(
    connection: Any,
    *,
    volume_alias: str,
    relative_path: str,
) -> Path:
    """Resolve and validate a Vault-root candidate for adoption.

    Serializes against concurrent root allocation via the shared lock.
    """
    lock_vault_root_allocation(connection)
    volume = _volume_or_raise(volume_alias)
    canonical = canonicalize_relative_path(relative_path)
    target = _resolve_directory(volume, canonical)
    _assert_no_nested_mount(volume, target)
    _assert_root_access(target)
    _assert_no_vault_overlap(connection, volume, canonical)
    return target


def browse_source_directories(
    connection: Any,
    *,
    volume_alias: str,
    relative_path: str = "",
) -> dict[str, Any]:
    """Lazy directory-only listing for the Vault-root picker.

    Occupied Vault roots are not traversable. Ancestors with occupied
    descendants stay navigable but are not selectable.
    """
    volume = _volume_or_raise(volume_alias)
    canonical = canonicalize_relative_path(relative_path)
    occupied = _occupied_vault_roots(connection, volume)

    for item in occupied:
        root = item["relative_path"]
        if root == "" or canonical == root or canonical.startswith(root + "/"):
            raise SourceDirectoryError(
                "occupied",
                "Occupied Vault roots cannot be browsed",
            )

    directory = _resolve_directory(volume, canonical)
    try:
        children = sorted(directory.iterdir(), key=lambda path: path.name.lower())
    except OSError as exc:
        raise SourceDirectoryError("path_missing", "Directory cannot be listed") from exc

    entries: list[dict[str, Any]] = []
    for child in children:
        if source_layout.path_is_symlink(child):
            continue
        if not child.is_dir():
            continue
        name = child.name
        child_rel = name if canonical == "" else f"{canonical}/{name}"
        occupation = _occupation_for_path(child_rel, occupied)
        entries.append(
            {
                "name": name,
                "relative_path": child_rel,
                "navigable": occupation is None,
                "selectable": occupation is None
                and not _has_occupied_descendant(child_rel, occupied),
                "occupation": occupation,
            }
        )

    return {
        "volume_alias": volume.alias,
        "relative_path": canonical,
        "items": entries,
    }
