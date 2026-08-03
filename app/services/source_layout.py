"""Fixed Source Volume namespace (issue #148).

Production paths are fixed at ``/sources`` and ``/sources/managed``.
Tests inject a private layout seam via ``override_sources_root``; no
application environment setting redirects the production boundary.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import source_identity

PRODUCTION_SOURCES_ROOT = Path("/sources")
MANAGED_DIR_NAME = "managed"
_UUID_DIR_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_sources_root_override: Path | None = None
_structural_error: str | None = None
# Transient health is process-local and deliberately separate from persisted
# markerless identity. Identity is reconciled before background work starts.
_runtime_mount_state: dict[str, dict[str, object]] = {}
_known_volume_aliases: set[str] = set()
_identity_enforcement_ready = False
_expected_identities: dict[str, tuple[str, str]] = {}


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


def path_is_accessible(path: Path | str) -> bool:
    """A Source Volume needs directory read and search access."""
    return os.access(Path(path), os.R_OK | os.X_OK)


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
        accessible = path_is_accessible(entry)
        access = "rw" if accessible and path_is_writable(entry) else "ro"
        health = (
            "inaccessible"
            if not accessible
            else ("ok" if access == "rw" else "read_only")
        )
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
        if _identity_enforcement_ready and _runtime_mount_state.get(
            volume.alias, {}
        ).get("identity_health") != "ok":
            continue
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
    """Clear transient health bookkeeping (tests / process restart)."""
    global _known_volume_aliases, _identity_enforcement_ready
    _runtime_mount_state.clear()
    _known_volume_aliases = set()
    _expected_identities.clear()
    _identity_enforcement_ready = False


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


def _identity_error_health(error: source_identity.MountIdentityError) -> str:
    message = str(error)
    if "ambiguous" in message:
        return "identity_ambiguous"
    return "identity_unsupported"


def _check_live_volume_identity(volume: SourceVolume) -> None:
    """Refresh one live volume's identity health without opening the database."""
    expected = _expected_identities.get(volume.alias)
    state = _runtime_mount_state.setdefault(volume.alias, {})
    previous_health = state.get("identity_health")
    try:
        observed = source_identity.fingerprint_for_mount(volume.path)
    except source_identity.MountIdentityError as exc:
        health = _identity_error_health(exc)
    else:
        if expected is None or expected[0] != source_identity.FINGERPRINT_VERSION:
            health = "identity_unsupported"
        else:
            health = "ok" if observed == expected[1] else "replaced"
    state["identity_health"] = health
    state["lost"] = False
    if health == "ok" and previous_health in {
        "absent", "replaced", "identity_ambiguous", "identity_unsupported"
    }:
        state["needs_scan"] = True


def reconcile_source_volume_identities() -> None:
    """Enroll or compare every live custom volume before local data is touched.

    Expected fingerprints are immutable. A retry or scan can never accept a
    replacement; only restoring mount metadata that hashes to the persisted
    expected value clears the replacement state (and then requires a scan).
    """
    global _identity_enforcement_ready
    from ..database import db
    from .audit_events import record_audit_event

    volumes = {volume.alias: volume for volume in discover_source_volumes()}
    now = datetime.now(UTC).isoformat()
    with db() as connection:
        persisted_rows = connection.execute(
            "SELECT * FROM source_volumes ORDER BY alias"
        ).fetchall()
        persisted = {row["alias"]: row for row in persisted_rows}
        _expected_identities.clear()
        _expected_identities.update(
            {
                alias: (row["fingerprint_version"], row["expected_fingerprint"])
                for alias, row in persisted.items()
            }
        )

        for alias, volume in volumes.items():
            state = _runtime_mount_state.setdefault(alias, {})
            try:
                observed = source_identity.fingerprint_for_mount(volume.path)
                identity_health = "ok"
                alert_token: str | None = None
            except source_identity.MountIdentityError as exc:
                observed = None
                identity_health = _identity_error_health(exc)
                alert_token = identity_health

            expected = persisted.get(alias)
            if expected is None and observed is not None:
                connection.execute(
                    """
                    INSERT INTO source_volumes(
                        alias, fingerprint_version, expected_fingerprint,
                        first_seen_at, last_seen_at, last_alert_token
                    ) VALUES (%s, %s, %s, %s, %s, NULL)
                    """,
                    (
                        alias,
                        source_identity.FINGERPRINT_VERSION,
                        observed,
                        now,
                        now,
                    ),
                )
                expected = {
                    "alias": alias,
                    "fingerprint_version": source_identity.FINGERPRINT_VERSION,
                    "expected_fingerprint": observed,
                    "last_alert_token": None,
                }
                persisted[alias] = expected
                _expected_identities[alias] = (
                    source_identity.FINGERPRINT_VERSION,
                    observed,
                )
            elif expected is not None and (
                expected["fingerprint_version"] != source_identity.FINGERPRINT_VERSION
            ):
                identity_health = "identity_unsupported"
                alert_token = "identity_version_unsupported"
            elif expected is not None and observed is not None:
                if observed != expected["expected_fingerprint"]:
                    identity_health = "replaced"
                    alert_token = f"replaced:{observed}"
                else:
                    identity_health = "ok"
                    alert_token = None

            previous_health = state.get("identity_health")
            state["identity_health"] = identity_health
            state["lost"] = False
            if identity_health == "ok" and previous_health in {
                "replaced", "identity_ambiguous", "identity_unsupported"
            }:
                state["needs_scan"] = True

            if expected is None:
                # Unsupported/ambiguous first sighting cannot establish an
                # expected identity and therefore cannot be accepted.
                continue
            connection.execute(
                "UPDATE source_volumes SET last_seen_at=%s WHERE alias=%s",
                (now, alias),
            )
            previous_alert = expected.get("last_alert_token")
            if alert_token != previous_alert:
                connection.execute(
                    "UPDATE source_volumes SET last_alert_token=%s WHERE alias=%s",
                    (alert_token, alias),
                )
                expected["last_alert_token"] = alert_token
                if alert_token is not None:
                    record_audit_event(
                        connection,
                        event="source_volume_identity_transition",
                        outcome="blocked",
                        visibility="admin",
                        alias=alias,
                        status=identity_health,
                    )

        for alias in persisted.keys() - volumes.keys():
            state = _runtime_mount_state.setdefault(alias, {})
            state["lost"] = True
            state["needs_scan"] = True
            state["identity_health"] = "absent"

    _known_volume_aliases.update(volumes)
    _identity_enforcement_ready = True


def _blocked_vault_access(
    *, alias: str | None, health: str = "unavailable"
) -> VaultLocalAccess:
    return VaultLocalAccess(
        local_operations_allowed=False,
        cloud_catalog_allowed=True,
        volume_alias=alias,
        volume_health=health,
    )


def _lexical_source_alias(vault_source_root: str | Path) -> tuple[str, str | None]:
    """Classify a configured root without resolving or probing the filesystem."""
    text = str(vault_source_root or "").strip()
    if not text or "\x00" in text:
        return "invalid", None
    path = Path(text)
    # Reject traversal even when normpath would place the result back in bounds.
    if ".." in path.parts:
        return "invalid", None
    normalized = os.path.normpath(text)
    sources = os.path.normpath(str(get_sources_root()))
    if not os.path.isabs(normalized) or not os.path.isabs(sources):
        return "outside", None
    try:
        if os.path.commonpath((normalized, sources)) != sources:
            return "outside", None
    except ValueError:
        return "outside", None
    relative = os.path.relpath(normalized, sources)
    if relative == ".":
        return "managed", MANAGED_DIR_NAME
    alias = relative.split(os.sep, 1)[0]
    if alias == MANAGED_DIR_NAME:
        return "managed", MANAGED_DIR_NAME
    return "custom", alias


def source_alias_for_root(vault_source_root: str | Path) -> tuple[str, str | None]:
    """Public lexical-only classifier used by relocation preflight."""
    return _lexical_source_alias(vault_source_root)


def _canonical_root_stays_within(root: str | Path, boundary: str | Path) -> bool:
    """Resolve and constrain a configured root before any later filesystem use."""
    try:
        resolved_root = os.path.realpath(os.fspath(root))
        resolved_boundary = os.path.realpath(os.fspath(boundary))
    except (OSError, TypeError, ValueError):
        return False

    # Include the separator in the prefix so a sibling such as
    # ``/sources/photos-escape`` cannot satisfy the boundary check. Appending
    # it to the candidate also permits the boundary directory itself.
    guarded_root = resolved_root.rstrip(os.sep) + os.sep
    guarded_boundary = resolved_boundary.rstrip(os.sep) + os.sep
    return guarded_root.startswith(guarded_boundary)


def vault_local_access(vault_source_root: str | Path) -> VaultLocalAccess:
    """Decide local-ops eligibility using a lexical-first identity gate.

    Unsafe or unavailable Source Volumes are classified from their lexical
    alias and runtime health before the configured Vault tree is resolved or
    probed. Only mounted ``ok``, ``read_only`` and ``scan_required`` volumes
    proceed to canonical containment checks.
    """
    location, alias = _lexical_source_alias(vault_source_root)
    if location == "invalid":
        return _blocked_vault_access(alias=None)
    if location == "outside":
        test_seam_active = (
            _sources_root_override is not None
            or bool(os.getenv("FROSTVAULT_TEST_SOURCES_ROOT", "").strip())
        )
        if test_seam_active:
            return _blocked_vault_access(alias=None)
        production_active = (
            get_sources_root() == PRODUCTION_SOURCES_ROOT
            and PRODUCTION_SOURCES_ROOT.exists()
            and path_is_mount(PRODUCTION_SOURCES_ROOT)
        )
        if production_active:
            return _blocked_vault_access(alias=None)
        # Historical unit fixtures use private roots without the layout seam.
        return VaultLocalAccess(True, True, None, "ok")

    if location == "managed":
        ready = sources_layout_is_ready()
        if not ready:
            return _blocked_vault_access(alias=MANAGED_DIR_NAME)
        if not _canonical_root_stays_within(vault_source_root, get_managed_root()):
            return _blocked_vault_access(alias=MANAGED_DIR_NAME)
        return VaultLocalAccess(True, True, MANAGED_DIR_NAME, "ok")

    assert alias is not None
    state = _runtime_mount_state.get(alias, {})
    identity_health = state.get("identity_health")
    if identity_health in {
        "absent",
        "replaced",
        "identity_ambiguous",
        "identity_unsupported",
    }:
        return _blocked_vault_access(alias=alias, health=str(identity_health))
    if state.get("lost"):
        return _blocked_vault_access(alias=alias, health="mount_lost")

    volumes = {volume.alias: volume for volume in discover_source_volumes()}
    volume = volumes.get(alias)
    if volume is None:
        return _blocked_vault_access(alias=alias, health="absent")
    if volume.health == "inaccessible":
        return _blocked_vault_access(alias=alias, health="inaccessible")

    if _identity_enforcement_ready:
        _check_live_volume_identity(volume)
        state = _runtime_mount_state.get(alias, {})
        identity_health = state.get("identity_health")
        if identity_health in {
            "absent",
            "replaced",
            "identity_ambiguous",
            "identity_unsupported",
        }:
            return _blocked_vault_access(alias=alias, health=str(identity_health))
        if state.get("lost"):
            return _blocked_vault_access(alias=alias, health="mount_lost")

    health = "scan_required" if state.get("needs_scan") else volume.health
    if health not in {"ok", "read_only", "scan_required"}:
        return _blocked_vault_access(alias=alias, health=health)
    if not _canonical_root_stays_within(vault_source_root, volume.path):
        return _blocked_vault_access(alias=alias)
    return VaultLocalAccess(
        local_operations_allowed=health == "ok" and volume.access == "rw",
        cloud_catalog_allowed=True,
        volume_alias=alias,
        volume_health=health,
    )



def source_volume_inventory() -> list[dict[str, object]]:
    """Admin inventory including persisted aliases that are currently absent."""
    from ..database import db

    if _identity_enforcement_ready:
        reconcile_source_volume_identities()
    live = {volume.alias: volume for volume in discover_source_volumes()}
    with db() as connection:
        persisted = {
            row["alias"]
            for row in connection.execute("SELECT alias FROM source_volumes").fetchall()
        }
        vault_rows = connection.execute(
            "SELECT source_root FROM vaults WHERE root_released_at IS NULL"
        ).fetchall()
        area_rows = connection.execute(
            "SELECT volume_alias, COUNT(*) AS total FROM source_areas "
            "GROUP BY volume_alias"
        ).fetchall()

    aliases = set(live) | persisted | {row["volume_alias"] for row in area_rows}
    counts = {alias: 0 for alias in aliases}
    area_counts = {alias: 0 for alias in aliases}
    # Count roots from their stored namespace path only. Inventory must remain
    # usable for an absent/replaced volume and must not resolve or traverse a
    # Vault tree merely to produce an administrative count.
    for row in vault_rows:
        location, alias = _lexical_source_alias(row["source_root"])
        if location == "custom" and alias in counts:
            counts[alias] += 1
    for row in area_rows:
        area_counts[row["volume_alias"]] = int(row["total"])

    items: list[dict[str, object]] = []
    for alias in sorted(aliases):
        volume = live.get(alias)
        state = _runtime_mount_state.get(alias, {})
        identity_health = state.get("identity_health")
        if identity_health in {"replaced", "identity_ambiguous", "identity_unsupported"}:
            health = str(identity_health)
        elif volume is None or state.get("lost"):
            health = "absent"
        elif state.get("needs_scan"):
            health = "scan_required"
        else:
            health = volume.health
        diagnostic_code = None if health == "ok" else f"source_volume.{health}"
        items.append(
            {
                "alias": alias,
                "path": str(get_sources_root() / alias),
                "access": volume.access if volume is not None else "none",
                "health": health,
                "vault_count": counts.get(alias, 0),
                "source_area_count": area_counts.get(alias, 0),
                "diagnostic": None,
                "diagnostic_code": diagnostic_code,
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
    if _identity_enforcement_ready:
        reconcile_source_volume_identities()
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
    except SourcesLayoutError as exc:
        _structural_error = str(exc)
        if _sources_root_override is not None:
            raise


def validate_nested_mounts_after_identity() -> None:
    """Run content-boundary validation only after identity reconciliation."""
    global _structural_error
    try:
        reject_nested_mounts()
    except (OSError, SourcesLayoutError) as exc:
        _structural_error = str(exc)
        if _sources_root_override is not None:
            raise SourcesLayoutError(str(exc)) from exc


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
