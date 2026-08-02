"""Exclusive Source Area grants (issue #149).

A Source Area is a reusable, non-overlapping subtree of one Source Volume
assigned exclusively to one User. It authorizes discovery and creation of
new Vault roots only; membership remains the sole data-access boundary
(ADR-0013).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings
from . import source_layout

# Advisory lock key shared with Vault-root allocation (#150) so concurrent
# Source Area mutations and adoption fail closed on PostgreSQL.
_SOURCE_AREA_LOCK_KEY = 0x534F555243415245  # "SOURCARE"


class SourceAreaError(Exception):
    """A recoverable failure while managing Source Areas.

    ``reason`` is a short machine-readable code callers may surface safely.
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def paths_overlap(left: str, right: str) -> bool:
    """True when two volume-relative paths conflict by equality or ancestry."""
    if left == right:
        return True
    if left == "" or right == "":
        return True
    return left.startswith(right + "/") or right.startswith(left + "/")


def _lock_source_area_mutations(connection: Any) -> None:
    """Serialize Source Area assignment against concurrent overlap races.

    SQLite takes the database-wide write lock; PostgreSQL takes a transaction
    advisory lock that Vault-root allocation (#150) will share.
    """
    backend = getattr(connection, "backend", None) or settings.db_backend
    if backend == "sqlite":
        raw = getattr(connection, "connection", None)
        if raw is not None and getattr(raw, "in_transaction", False):
            return
        connection.begin_immediate()
    else:
        connection.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (_SOURCE_AREA_LOCK_KEY,),
        )


def canonicalize_relative_path(relative_path: str) -> str:
    """Normalize a path relative to a Source Volume root.

    Empty string means the volume root itself. Rejects empty segments,
    ``.`` / ``..``, backslashes, and absolute forms.
    """
    raw = (relative_path or "").strip().replace("\\", "/")
    if raw in ("", "."):
        return ""
    if raw.startswith("/"):
        raise SourceAreaError("invalid_path", "Source Area path must be relative")
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise SourceAreaError("invalid_path", "Source Area path must not escape")
        parts.append(part)
    return "/".join(parts)


def _volume_or_raise(volume_alias: str) -> source_layout.SourceVolume:
    alias = (volume_alias or "").strip()
    if (
        not alias
        or alias == source_layout.MANAGED_DIR_NAME
        or Path(alias).parts != (alias,)
    ):
        raise SourceAreaError("invalid_volume", "Source Volume is not assignable")
    # Gate identity and volume health before discovering or resolving a
    # candidate directory. A replacement must not be traversed by an
    # assignment or browser request.
    identity_access = source_layout.vault_local_access(
        source_layout.get_sources_root() / alias
    )
    if identity_access.volume_health == "absent":
        raise SourceAreaError("volume_not_found", f"Source Volume '{alias}' was not found")
    if not identity_access.local_operations_allowed:
        raise SourceAreaError(
            "volume_unavailable",
            f"Source Volume '{alias}' is not healthy for assignment",
        )
    volumes = {volume.alias: volume for volume in source_layout.discover_source_volumes()}
    volume = volumes.get(alias)
    if volume is None:
        raise SourceAreaError("volume_not_found", f"Source Volume '{alias}' was not found")
    if volume.health != "ok" or volume.access != "rw":
        raise SourceAreaError(
            "volume_unavailable",
            f"Source Volume '{alias}' is not healthy for assignment",
        )
    return volume


def _resolve_area_directory(volume: source_layout.SourceVolume, relative_path: str) -> Path:
    root = Path(volume.path)
    target = root if relative_path == "" else root.joinpath(*relative_path.split("/"))
    if source_layout.path_is_symlink(target):
        raise SourceAreaError("invalid_path", "Source Area must not be a symlink")
    if not target.is_dir():
        raise SourceAreaError("path_missing", "Source Area directory does not exist")
    try:
        resolved = target.resolve()
        resolved.relative_to(Path(volume.path).resolve())
    except ValueError as exc:
        raise SourceAreaError(
            "invalid_path",
            "Source Area path must stay inside its Source Volume",
        ) from exc
    return resolved


def _public_grant(
    row: dict[str, Any],
    *,
    availability: str,
    usable: bool,
) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "volume_alias": row["volume_alias"],
        "relative_path": row["relative_path"],
        "created_at": row["created_at"],
        "availability": availability,
        "usable": usable,
    }


def _user_is_active(connection: Any, user_id: int) -> bool:
    row = connection.execute(
        "SELECT active FROM users WHERE id=%s",
        (user_id,),
    ).fetchone()
    if row is None:
        return False
    return bool(row["active"])


def _availability_for(volume_alias: str, relative_path: str) -> str:
    # Listing grants is an administrative/catalog operation, but it must not
    # resolve a configured tree while the backing Source Volume is unsafe.
    access = source_layout.vault_local_access(
        source_layout.get_sources_root() / volume_alias
    )
    if not access.local_operations_allowed:
        return "unavailable"
    volumes = {volume.alias: volume for volume in source_layout.discover_source_volumes()}
    volume = volumes.get(volume_alias)
    if volume is None:
        return "unavailable"
    try:
        _resolve_area_directory(volume, relative_path)
    except SourceAreaError:
        return "unavailable"
    return "available"


def _assert_no_overlap(
    connection: Any,
    *,
    volume_alias: str,
    relative_path: str,
) -> None:
    existing = connection.execute(
        """
        SELECT relative_path FROM source_areas
        WHERE volume_alias=%s
        """,
        (volume_alias,),
    ).fetchall()
    for row in existing:
        if paths_overlap(relative_path, row["relative_path"]):
            raise SourceAreaError(
                "overlap",
                "Source Area overlaps an existing grant on this Source Volume",
            )


def _require_reason(reason: str) -> str:
    cleaned = (reason or "").strip()
    if len(cleaned) < 3 or len(cleaned) > 500:
        raise SourceAreaError(
            "reason_required",
            "Source Area mutations require a reason between 3 and 500 characters",
        )
    return cleaned


def _record_mutation(
    connection: Any,
    *,
    event: str,
    actor_user_id: int,
    grantee_user_id: int,
    reason: str,
    grant: dict[str, Any],
) -> None:
    from .audit_events import record_audit_event
    from .notifications import enqueue_notification

    record_audit_event(
        connection,
        event=event,
        actor_user_id=actor_user_id,
        outcome="success",
        visibility="admin",
        reason=reason,
        source_area_id=grant["id"],
        target_user_id=grantee_user_id,
        volume_alias=grant["volume_alias"],
        relative_path=grant["relative_path"],
    )
    enqueue_notification(
        connection,
        user_id=grantee_user_id,
        event=event,
        title=f"Administrator action: {event}",
        body=reason,
        channels=("in_app",),
    )


def assign_source_area(
    connection: Any,
    *,
    user_id: int,
    volume_alias: str,
    relative_path: str,
    actor_user_id: int,
    reason: str,
) -> dict[str, Any]:
    """Persist an exclusive Source Area for ``user_id`` on a healthy volume."""
    cleaned_reason = _require_reason(reason)
    _lock_source_area_mutations(connection)
    user = connection.execute(
        "SELECT id FROM users WHERE id=%s",
        (user_id,),
    ).fetchone()
    if user is None:
        raise SourceAreaError("user_not_found", "User was not found")

    volume = _volume_or_raise(volume_alias)
    canonical = canonicalize_relative_path(relative_path)
    _resolve_area_directory(volume, canonical)
    _assert_no_overlap(
        connection,
        volume_alias=volume.alias,
        relative_path=canonical,
    )

    try:
        row = connection.execute(
            """
            INSERT INTO source_areas(user_id, volume_alias, relative_path, created_at)
            VALUES (%s, %s, %s, %s)
            RETURNING id, user_id, volume_alias, relative_path, created_at
            """,
            (user_id, volume.alias, canonical, _utcnow()),
        ).fetchone()
    except Exception as exc:
        # Unique constraint is the last line of defence for exact overlap races.
        message = str(exc).lower()
        if "unique" in message or "source_areas_volume_relative" in message:
            raise SourceAreaError(
                "overlap",
                "Source Area overlaps an existing grant on this Source Volume",
            ) from exc
        raise
    grant = _public_grant(row, availability="available", usable=True)
    _record_mutation(
        connection,
        event="source_area_assigned",
        actor_user_id=actor_user_id,
        grantee_user_id=user_id,
        reason=cleaned_reason,
        grant=grant,
    )
    return grant


def revoke_source_area(
    connection: Any,
    *,
    source_area_id: int,
    actor_user_id: int,
    reason: str,
) -> dict[str, Any]:
    """Revoke one Source Area. Never relocates or alters existing Vaults."""
    cleaned_reason = _require_reason(reason)
    _lock_source_area_mutations(connection)
    row = connection.execute(
        """
        SELECT id, user_id, volume_alias, relative_path, created_at
        FROM source_areas
        WHERE id=%s
        """,
        (source_area_id,),
    ).fetchone()
    if row is None:
        raise SourceAreaError("not_found", "Source Area was not found")
    grant = _public_grant(
        row,
        availability=_availability_for(row["volume_alias"], row["relative_path"]),
        usable=_user_is_active(connection, int(row["user_id"])),
    )
    connection.execute("DELETE FROM source_areas WHERE id=%s", (source_area_id,))
    _record_mutation(
        connection,
        event="source_area_revoked",
        actor_user_id=actor_user_id,
        grantee_user_id=int(row["user_id"]),
        reason=cleaned_reason,
        grant=grant,
    )
    return grant


def list_source_areas_for_user(connection: Any, *, user_id: int) -> list[dict[str, Any]]:
    """Return every Source Area granted to ``user_id``.

    Inactive Users keep reserved grants (``usable=False``); reactivation
    restores use without reassignment.
    """
    usable = _user_is_active(connection, user_id)
    rows = connection.execute(
        """
        SELECT id, user_id, volume_alias, relative_path, created_at
        FROM source_areas
        WHERE user_id=%s
        ORDER BY volume_alias, relative_path, id
        """,
        (user_id,),
    ).fetchall()
    return [
        _public_grant(
            row,
            availability=_availability_for(row["volume_alias"], row["relative_path"]),
            usable=usable,
        )
        for row in rows
    ]


def list_source_areas_for_volume(
    connection: Any, *, volume_alias: str
) -> list[dict[str, Any]]:
    """Return every Source Area on one Source Volume."""
    rows = connection.execute(
        """
        SELECT sa.id, sa.user_id, sa.volume_alias, sa.relative_path, sa.created_at,
               u.active AS user_active
        FROM source_areas sa
        JOIN users u ON u.id=sa.user_id
        WHERE sa.volume_alias=%s
        ORDER BY sa.relative_path, sa.id
        """,
        (volume_alias,),
    ).fetchall()
    return [
        _public_grant(
            row,
            availability=_availability_for(row["volume_alias"], row["relative_path"]),
            usable=bool(row["user_active"]),
        )
        for row in rows
    ]


def list_all_source_areas(connection: Any) -> list[dict[str, Any]]:
    """Return every Source Area (admin inventory)."""
    rows = connection.execute(
        """
        SELECT sa.id, sa.user_id, sa.volume_alias, sa.relative_path, sa.created_at,
               u.active AS user_active
        FROM source_areas sa
        JOIN users u ON u.id=sa.user_id
        ORDER BY sa.volume_alias, sa.relative_path, sa.id
        """
    ).fetchall()
    return [
        _public_grant(
            row,
            availability=_availability_for(row["volume_alias"], row["relative_path"]),
            usable=bool(row["user_active"]),
        )
        for row in rows
    ]


def _occupied_vault_roots(
    connection: Any,
    volume: source_layout.SourceVolume,
) -> list[dict[str, Any]]:
    """Vault roots under ``volume``, including disabled/unavailable Vaults."""
    # Filter by the lexical namespace before canonicalizing any stored root;
    # a different, currently replaced Source Volume must not be resolved just
    # because an admin is browsing this healthy volume.
    volume_path = Path(volume.path).resolve()
    lexical_volume_path = Path(os.path.normpath(volume.path))
    rows = connection.execute(
        """
        SELECT v.id, v.name, v.source_root, u.display_name AS owner_display_name
        FROM vaults v
        LEFT JOIN vault_members vm ON vm.vault_id=v.id AND vm.role='owner'
        LEFT JOIN users u ON u.id=vm.user_id
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
        occupied.append(
            {
                "relative_path": relative,
                "vault_name": row["name"],
                "owner_display_name": row["owner_display_name"] or "",
            }
        )
    return occupied


def _occupation_for_path(
    relative_path: str,
    occupied: list[dict[str, Any]],
    *,
    viewer_is_admin: bool,
) -> dict[str, Any] | None:
    for item in occupied:
        if item["relative_path"] == relative_path:
            if viewer_is_admin:
                return {
                    "kind": "vault",
                    "vault_name": item["vault_name"],
                    "owner_display_name": item["owner_display_name"],
                }
            return {"kind": "vault", "label": "Occupied by a Vault"}
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


def _user_visible_relative_paths(
    connection: Any,
    *,
    user_id: int,
    volume_alias: str,
) -> list[str]:
    rows = connection.execute(
        """
        SELECT relative_path FROM source_areas
        WHERE user_id=%s AND volume_alias=%s
        ORDER BY relative_path
        """,
        (user_id, volume_alias),
    ).fetchall()
    return [row["relative_path"] for row in rows]


def _path_allowed_for_user(relative_path: str, grants: list[str]) -> bool:
    for grant in grants:
        if relative_path == grant:
            return True
        if grant == "":
            return True
        if relative_path.startswith(grant + "/"):
            return True
        # Ancestors of a grant remain navigable so the User can reach it.
        if grant.startswith(relative_path + "/") or (
            relative_path == "" and grant != ""
        ):
            return True
    return False


def path_covered_by_grants(relative_path: str, grants: list[str]) -> bool:
    """True when ``relative_path`` equals or descends from one grant root."""
    for grant in grants:
        if relative_path == grant:
            return True
        if grant == "":
            return True
        if relative_path.startswith(grant + "/"):
            return True
    return False


def _assert_no_nested_mount(volume: source_layout.SourceVolume, target: Path) -> None:
    """Reject candidates that cross a mount nested under the Source Volume."""
    volume_root = Path(volume.path).resolve()
    current = target.resolve()
    while current != volume_root:
        if source_layout.path_is_mount(current):
            raise SourceAreaError(
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
        raise SourceAreaError(
            "unreadable",
            "Adoption root is not listable or traversable",
        )
    if not source_layout.path_is_writable(target):
        raise SourceAreaError(
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
            raise SourceAreaError(
                "overlap",
                "Adoption path overlaps an existing Vault root",
            )


def resolve_adoption_candidate(
    connection: Any,
    *,
    owner_user_id: int,
    volume_alias: str,
    relative_path: str,
    actor_is_admin: bool = False,
) -> Path:
    """Resolve a Vault-root candidate for adoption and authorize the owner.

    Serializes against Source Area mutations via the shared advisory lock.
    A normal User may adopt only under their own Source Areas. An
    administrator may adopt an unassigned path, or a path covered by a
    Source Area belonging to ``owner_user_id``.
    """
    _lock_source_area_mutations(connection)
    volume = _volume_or_raise(volume_alias)
    canonical = canonicalize_relative_path(relative_path)
    target = _resolve_area_directory(volume, canonical)
    _assert_no_nested_mount(volume, target)
    _assert_root_access(target)
    _assert_no_vault_overlap(connection, volume, canonical)

    grants = _user_visible_relative_paths(
        connection,
        user_id=owner_user_id,
        volume_alias=volume.alias,
    )
    covered_by_owner = path_covered_by_grants(canonical, grants)

    if actor_is_admin:
        if covered_by_owner:
            return target
        other = connection.execute(
            """
            SELECT user_id, relative_path FROM source_areas
            WHERE volume_alias=%s
            """,
            (volume.alias,),
        ).fetchall()
        for row in other:
            if path_covered_by_grants(canonical, [row["relative_path"]]):
                if int(row["user_id"]) != int(owner_user_id):
                    raise SourceAreaError(
                        "forbidden",
                        "Path is assigned to another User",
                    )
        # Unassigned (or covered by owner, handled above) is allowed for admin.
        return target

    if not covered_by_owner:
        raise SourceAreaError(
            "forbidden",
            "Path is outside the owner's Source Areas",
        )
    return target


def browse_source_directories(
    connection: Any,
    *,
    volume_alias: str,
    relative_path: str = "",
    viewer_user_id: int,
    viewer_is_admin: bool,
    purpose: str = "adopt",
) -> dict[str, Any]:
    """Lazy directory-only listing for Source Area / adoption pickers.

    Admin sees the full healthy volume. Users see only their Source Areas.
    Occupied Vault roots are not traversable. For adoption pickers, ancestors
    with occupied descendants stay navigable but are not selectable; Source
    Area grant assignment may still select those ancestors.
    """
    if purpose not in {"adopt", "grant"}:
        raise SourceAreaError("invalid_path", "Unknown browse purpose")
    grant_mode = purpose == "grant"
    volume = _volume_or_raise(volume_alias)
    canonical = canonicalize_relative_path(relative_path)
    occupied = _occupied_vault_roots(connection, volume)

    for item in occupied:
        root = item["relative_path"]
        if root == "":
            raise SourceAreaError(
                "occupied",
                "Occupied Vault roots cannot be browsed",
            )
        if canonical == root or canonical.startswith(root + "/"):
            raise SourceAreaError(
                "occupied",
                "Occupied Vault roots cannot be browsed",
            )

    grants: list[str] | None = None
    if not viewer_is_admin:
        grants = _user_visible_relative_paths(
            connection,
            user_id=viewer_user_id,
            volume_alias=volume.alias,
        )
        if not _path_allowed_for_user(canonical, grants):
            raise SourceAreaError(
                "forbidden",
                "Path is outside the viewer's Source Areas",
            )

    directory = _resolve_area_directory(volume, canonical)
    entries: list[dict[str, Any]] = []

    def _selectable(child_rel: str, occupation: dict[str, Any] | None) -> bool:
        if occupation is not None:
            return False
        if grant_mode:
            return True
        return not _has_occupied_descendant(child_rel, occupied)

    if not viewer_is_admin and grants is not None and canonical == "":
        if "" not in grants:
            seen: set[str] = set()
            for grant in grants:
                name = grant.split("/", 1)[0]
                if name in seen:
                    continue
                seen.add(name)
                child_rel = name
                occupation = _occupation_for_path(
                    child_rel, occupied, viewer_is_admin=False
                )
                entries.append(
                    {
                        "name": name,
                        "relative_path": child_rel,
                        "navigable": occupation is None,
                        "selectable": _selectable(child_rel, occupation)
                        and child_rel in grants,
                        "occupation": occupation,
                    }
                )
            entries.sort(key=lambda item: item["name"].lower())
            return {
                "volume_alias": volume.alias,
                "relative_path": canonical,
                "items": entries,
            }

    try:
        children = sorted(directory.iterdir(), key=lambda path: path.name.lower())
    except OSError as exc:
        raise SourceAreaError("path_missing", "Directory cannot be listed") from exc

    for child in children:
        if source_layout.path_is_symlink(child):
            continue
        if not child.is_dir():
            continue
        name = child.name
        child_rel = name if canonical == "" else f"{canonical}/{name}"
        if grants is not None and not _path_allowed_for_user(child_rel, grants):
            continue
        occupation = _occupation_for_path(
            child_rel, occupied, viewer_is_admin=viewer_is_admin
        )
        if occupation is not None:
            entries.append(
                {
                    "name": name,
                    "relative_path": child_rel,
                    "navigable": False,
                    "selectable": False,
                    "occupation": occupation,
                }
            )
            continue
        entries.append(
            {
                "name": name,
                "relative_path": child_rel,
                "navigable": True,
                "selectable": _selectable(child_rel, None),
                "occupation": None,
            }
        )

    return {
        "volume_alias": volume.alias,
        "relative_path": canonical,
        "items": entries,
    }
