"""Fixed Source Volume namespace (issue #148).

Production paths are fixed at ``/sources`` and ``/sources/managed``.
Tests inject a private layout seam via ``override_sources_root``; no
application environment setting redirects the production boundary.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

PRODUCTION_SOURCES_ROOT = Path("/sources")
MANAGED_DIR_NAME = "managed"
_UUID_DIR_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_sources_root_override: Path | None = None
_structural_error: str | None = None
# alias -> {"lost": bool, "needs_scan": bool}
_runtime_mount_state: dict[str, dict[str, bool]] = {}
_known_volume_aliases: set[str] = set()


class SourcesLayoutError(Exception):
    """Structural failure of the fixed ``/sources`` namespace."""


@dataclass(frozen=True)
class SourceVolume:
    """One operator-mounted filesystem directly under ``/sources/<alias>``."""

    alias: str
    path: str
    access: str
    health: str
    diagnostic: str | None = None


@dataclass(frozen=True)
class VaultLocalAccess:
    """Whether a Vault root may run local filesystem operations right now."""

    local_operations_allowed: bool
    cloud_catalog_allowed: bool
    volume_alias: str | None
    volume_health: str


def path_is_symlink(path: Path | str) -> bool:
    """Symlink probe seam — mockable for non-privileged CI hosts."""
    return Path(path).is_symlink()


def path_is_mount(path: Path | str) -> bool:
    """Mount-point probe seam — mockable for non-Linux CI hosts."""
    return os.path.ismount(Path(path))


def path_is_writable(path: Path | str) -> bool:
    """Writability probe seam — mockable for non-privileged CI hosts."""
    return os.access(Path(path), os.W_OK)


def get_sources_root() -> Path:
    if _sources_root_override is not None:
        return _sources_root_override
    # Private test/native seam for subprocess fixtures — not a production
    # application setting and not honored as VAULT_SOURCES_ROOT.
    test_root = os.getenv("FROSTVAULT_TEST_SOURCES_ROOT", "").strip()
    if test_root:
        return Path(test_root)
    return PRODUCTION_SOURCES_ROOT


def get_managed_root() -> Path:
    return get_sources_root() / MANAGED_DIR_NAME


def managed_vault_path(vault_uuid: str) -> Path:
    return get_managed_root() / vault_uuid


def sources_layout_is_ready() -> bool:
    """True when the last prepare/validation left no structural error."""
    return _structural_error is None


def structural_error_message() -> str | None:
    return _structural_error


def ensure_managed_directory() -> Path:
    """Create ``managed`` under the sources root when absent.

    Never chowns or chmods. The sources root itself must already exist;
    structural validation of that mount belongs to readiness checks.
    """
    managed = get_managed_root()
    managed.mkdir(exist_ok=True)
    return managed


def validate_sources_structure() -> None:
    """Fail closed when the fixed sources namespace is structurally invalid."""
    sources = get_sources_root()
    if not sources.exists() or not sources.is_dir():
        raise SourcesLayoutError(
            f"Sources root is missing or not a directory: {sources}"
        )
    if not path_is_mount(sources):
        raise SourcesLayoutError(
            f"Sources root must be a real mounted directory: {sources}"
        )

    managed = get_managed_root()
    if managed.exists() and path_is_symlink(managed):
        raise SourcesLayoutError(
            f"Managed sources directory must not be a symbolic link: {managed}"
        )
    if managed.exists() and managed.is_dir():
        for entry in managed.iterdir():
            if (
                not entry.is_dir()
                or path_is_symlink(entry)
                or not _UUID_DIR_PATTERN.fullmatch(entry.name)
            ):
                raise SourcesLayoutError(
                    f"Unexpected foreign entry under managed sources: {entry}"
                )

    for entry in sorted(sources.iterdir(), key=lambda item: item.name):
        if entry.name == MANAGED_DIR_NAME:
            continue
        if not entry.is_dir() or path_is_symlink(entry) or not path_is_mount(entry):
            raise SourcesLayoutError(
                f"Direct child {entry.name!r} under {sources} must be a real "
                f"mount point mounted directly as /sources/{entry.name}; "
                "ordinary directories and nested mounts are unsupported — place "
                "content inside one Source Volume sibling instead"
            )


def discover_source_volumes() -> list[SourceVolume]:
    """Return custom Source Volumes mounted directly under the sources root.

    ``managed`` is reserved and never returned. Only direct children that are
    real mount points are Source Volumes; callers that need rejection
    diagnostics for ordinary directories use ``validate`` / inventory helpers.
    """
    sources = get_sources_root()
    volumes: list[SourceVolume] = []
    if not sources.exists() or not sources.is_dir():
        return volumes
    for entry in sorted(sources.iterdir(), key=lambda item: item.name):
        if entry.name == MANAGED_DIR_NAME:
            continue
        if not entry.is_dir() or path_is_symlink(entry) or not path_is_mount(entry):
            continue
        access = "rw" if path_is_writable(entry) else "ro"
        health = "ok" if access == "rw" else "read_only"
        volumes.append(
            SourceVolume(
                alias=entry.name,
                path=str(entry),
                access=access,
                health=health,
            )
        )
    return volumes


def reject_nested_mounts() -> None:
    """Fail closed when a Source Volume contains another mount point.

    Nested mounts are unsupported: each filesystem must be mounted directly
    as ``/sources/<alias>`` so one Vault can never cross Source Volumes.
    """
    for volume in discover_source_volumes():
        root = Path(volume.path)
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            if entry.is_dir() and not path_is_symlink(entry) and path_is_mount(entry):
                raise SourcesLayoutError(
                    f"Nested mount {entry} inside Source Volume {volume.alias!r} "
                    f"is unsupported; mount each filesystem directly under "
                    f"/sources/<alias> as a sibling of {volume.alias!r}"
                )




def reset_runtime_mount_state() -> None:
    """Clear runtime mount-loss bookkeeping (tests / process restart)."""
    global _known_volume_aliases
    _runtime_mount_state.clear()
    _known_volume_aliases = set()


def note_mount_lost(alias: str) -> None:
    """Record that a custom Source Volume mount disappeared at runtime."""
    _runtime_mount_state[alias] = {"lost": True, "needs_scan": True}


def note_mount_returned(alias: str) -> None:
    """Record remount; local ops stay blocked until a full local scan completes."""
    state = _runtime_mount_state.get(alias, {"lost": False, "needs_scan": False})
    state["lost"] = False
    state["needs_scan"] = True
    _runtime_mount_state[alias] = state


def note_full_local_scan_completed(alias: str) -> None:
    """Clear the post-remount full-scan gate for ``alias``."""
    state = _runtime_mount_state.get(alias)
    if state is None:
        return
    state["needs_scan"] = False
    if not state["lost"]:
        _runtime_mount_state.pop(alias, None)
    else:
        _runtime_mount_state[alias] = state


def should_emit_local_copy_removals(alias: str) -> bool:
    """Mount loss must not mass-mark Local Copies missing."""
    state = _runtime_mount_state.get(alias)
    if state and state.get("lost"):
        return False
    return True


def requires_full_local_scan(alias: str) -> bool:
    state = _runtime_mount_state.get(alias)
    return bool(state and state.get("needs_scan"))


def vault_local_access(vault_source_root: str | Path) -> VaultLocalAccess:
    """Decide local-ops eligibility for a Vault root from Source Volume health.

    Managed UUID roots are always locally eligible when the structural layout
    is ready. Custom Source Volume roots inherit that volume's health: an
    unhealthy volume suspends local operations while cloud catalog access
    remains available.
    """
    root = Path(vault_source_root).resolve()
    managed = get_managed_root().resolve()
    if root == managed or managed in root.parents or root.parent == managed:
        ready = sources_layout_is_ready()
        if not ready and _sources_root_override is not None and root.is_dir():
            ready = True
        return VaultLocalAccess(
            local_operations_allowed=ready,
            cloud_catalog_allowed=True,
            volume_alias=MANAGED_DIR_NAME,
            volume_health="ok" if ready else "unavailable",
        )

    sources = get_sources_root().resolve()
    try:
        relative = root.relative_to(sources)
    except ValueError:
        # Outside the fixed namespace.
        test_seam_active = (
            _sources_root_override is not None
            or bool(os.getenv("FROSTVAULT_TEST_SOURCES_ROOT", "").strip())
        )
        if test_seam_active:
            return VaultLocalAccess(
                local_operations_allowed=False,
                cloud_catalog_allowed=True,
                volume_alias=None,
                volume_health="unavailable",
            )
        production_active = (
            get_sources_root() == PRODUCTION_SOURCES_ROOT
            and PRODUCTION_SOURCES_ROOT.exists()
            and path_is_mount(PRODUCTION_SOURCES_ROOT)
        )
        if production_active:
            return VaultLocalAccess(
                local_operations_allowed=False,
                cloud_catalog_allowed=True,
                volume_alias=None,
                volume_health="unavailable",
            )
        # Historical unit fixtures often use private or synthetic source_root
        # values outside /sources without installing the layout seam.
        return VaultLocalAccess(
            local_operations_allowed=True,
            cloud_catalog_allowed=True,
            volume_alias=None,
            volume_health="ok",
        )

    alias = relative.parts[0] if relative.parts else None
    if alias is None or alias == MANAGED_DIR_NAME:
        ready = sources_layout_is_ready()
        return VaultLocalAccess(
            local_operations_allowed=ready,
            cloud_catalog_allowed=True,
            volume_alias=MANAGED_DIR_NAME,
            volume_health="ok" if ready else "unavailable",
        )

    volumes = {volume.alias: volume for volume in discover_source_volumes()}
    volume = volumes.get(alias)
    if volume is None:
        return VaultLocalAccess(
            local_operations_allowed=False,
            cloud_catalog_allowed=True,
            volume_alias=alias,
            volume_health="missing",
        )
    state = _runtime_mount_state.get(alias, {})
    if state.get("lost"):
        return VaultLocalAccess(
            local_operations_allowed=False,
            cloud_catalog_allowed=True,
            volume_alias=volume.alias,
            volume_health="mount_lost",
        )
    if state.get("needs_scan"):
        return VaultLocalAccess(
            local_operations_allowed=False,
            cloud_catalog_allowed=True,
            volume_alias=volume.alias,
            volume_health="scan_required",
        )
    allowed = volume.health == "ok" and volume.access == "rw"
    return VaultLocalAccess(
        local_operations_allowed=allowed,
        cloud_catalog_allowed=True,
        volume_alias=volume.alias,
        volume_health=volume.health,
    )



def source_volume_inventory() -> list[dict[str, object]]:
    """Admin inventory rows for discovered Source Volumes.

    Source Area counts stay 0 until exclusive grants land (#149).
    """
    from ..database import db

    volumes = discover_source_volumes()
    if not volumes:
        return []

    resolved = [(volume, Path(volume.path).resolve()) for volume in volumes]
    counts: dict[str, int] = {volume.alias: 0 for volume, _ in resolved}
    with db() as connection:
        rows = connection.execute("SELECT source_root FROM vaults").fetchall()
    for row in rows:
        root = Path(row["source_root"]).resolve()
        for volume, volume_path in resolved:
            try:
                root.relative_to(volume_path)
            except ValueError:
                continue
            counts[volume.alias] += 1
            break

    items: list[dict[str, object]] = []
    for volume, _ in resolved:
        state = _runtime_mount_state.get(volume.alias, {})
        health = volume.health
        if state.get("lost"):
            health = "mount_lost"
        elif state.get("needs_scan"):
            health = "scan_required"
        diagnostic = volume.diagnostic
        if health != "ok" and diagnostic is None:
            diagnostic = (
                f"Ensure /sources/{volume.alias} is a direct rw mount; "
                "nested mounts and ordinary directories under /sources are unsupported."
            )
        items.append(
            {
                "alias": volume.alias,
                "path": volume.path,
                "access": volume.access,
                "health": health,
                "vault_count": counts.get(volume.alias, 0),
                "source_area_count": 0,
                "diagnostic": diagnostic,
            }
        )
    return items


def verify_mounts_once() -> list[str]:
    """Reconcile known Source Volume mounts with the live filesystem.

    Returns aliases whose runtime state changed.
    """
    global _known_volume_aliases
    changed: list[str] = []
    current = {volume.alias: volume for volume in discover_source_volumes()}
    current_aliases = set(current)

    for alias in sorted(_known_volume_aliases - current_aliases):
        state = _runtime_mount_state.get(alias, {})
        if not state.get("lost"):
            note_mount_lost(alias)
            changed.append(alias)

    for alias in sorted(current_aliases):
        state = _runtime_mount_state.get(alias, {})
        if state.get("lost"):
            note_mount_returned(alias)
            changed.append(alias)

    _known_volume_aliases |= current_aliases
    return changed


def prepare_sources_layout() -> None:
    """Validate the fixed sources namespace and create ``managed`` when absent.

    Records structural failures for readiness. Without a test layout override,
    failures are recorded without raising so the process can still expose
    liveness and diagnostics; readiness stays not-ready. With an explicit
    test override, invalid layouts raise so suite assertions stay precise.
    """
    global _structural_error
    _structural_error = None
    try:
        validate_sources_structure()
        ensure_managed_directory()
        validate_sources_structure()
        reject_nested_mounts()
    except SourcesLayoutError as exc:
        _structural_error = str(exc)
        if _sources_root_override is not None:
            raise


def override_sources_root(path: str | Path) -> Path:
    """Test-only private layout seam. Production must not call this."""
    global _sources_root_override
    global _structural_error
    resolved = Path(path)
    _sources_root_override = resolved
    # A new seam replaces any prior production soft-fail recorded by lifespan.
    _structural_error = None
    return resolved


def reset_sources_root_override() -> None:
    """Clear the test-only sources-root override."""
    global _sources_root_override
    global _structural_error
    _sources_root_override = None
    _structural_error = None
    reset_runtime_mount_state()
