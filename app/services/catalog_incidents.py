"""Catalog problems that need the Owner, derived from explicit catalog state.

``archive_version_missing``: a known Archive Version a complete cloud listing
no longer shows (#303). Its state is ``archive_versions.availability``.
``cloud_only_conflict``: different content at the path of a file whose only
copy is in the cloud; it is held back from upload (#411, #363 D7). Its state
is ``files.held_version_id``.

There is no incident table (#593): each problem is one Owner notification
whose ``dedupe_key`` names the exact version (and file), opened and resolved
from those columns. Resolution is reconciled by
:mod:`app.services.notifications` from the same columns.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from . import notifications as notification_service


INCIDENT_KIND_MISSING = "archive_version_missing"
INCIDENT_KIND_CLOUD_ONLY_CONFLICT = "cloud_only_conflict"
MISSING_KEY_PREFIX = "missing:"
CONFLICT_KEY_PREFIX = "conflict:"


def missing_key(version_id: Any) -> str:
    return f"{MISSING_KEY_PREFIX}{version_id}"


def conflict_key(file_id: Any, version_id: Any) -> str:
    return f"{CONFLICT_KEY_PREFIX}{file_id}:{version_id}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _is_open(connection: Any, key: str) -> bool:
    row = connection.execute(
        "SELECT problem_status FROM notifications WHERE dedupe_key=%s", (key,),
    ).fetchone()
    return bool(row and row["problem_status"] == "open")


def remember_missing_archives(
    connection: Any,
    rows: list[Mapping[str, Any]],
    *,
    reason: str,
    observed_at: Any,
) -> list[dict[str, Any]]:
    """Open one Owner problem per newly missing Archive Version.

    Repeated scans never notify twice while the problem is open. Authorized
    purges never reach this helper.
    """
    opened: list[dict[str, Any]] = []
    for row in rows:
        version_id = str(row["id"])
        key = missing_key(version_id)
        if _is_open(connection, key):
            continue
        params = {
            "path": str(row.get("path") or ""),
            "archive_version_id": version_id,
            "provider_version_id": str(row.get("provider_version_id") or ""),
            "observed_at": _stamp(observed_at),
            "reason": reason,
        }
        notification_service.set_problem(
            connection, key=key, event=INCIDENT_KIND_MISSING,
            vault_id=int(row["vault_id"]), active=True, params=params,
        )
        opened.append({**params, "vault_id": int(row["vault_id"]), "key": key})
    return opened


def resolve_archive_version_incidents(
    connection: Any,
    archive_version_ids: list[str],
    *,
    observed_at: Any = None,
) -> int:
    """Resolve open missing-version problems whose version is located again."""
    resolved = 0
    for version_id in archive_version_ids:
        key = missing_key(version_id)
        if _is_open(connection, key):
            notification_service.set_problem(
                connection, key=key, event=INCIDENT_KIND_MISSING, active=False,
            )
            resolved += 1
    return resolved


def settle_cloud_only_copies(
    connection: Any,
    *,
    vault_id: int,
    path: str | None = None,
    observed_at: Any = None,
) -> dict[str, int]:
    """Decide held content at a cloud-only path (``files.held_version_id``).

    Identical content is matched (and released) by the fingerprint itself
    (:meth:`ArchiveCatalog.set_local_fingerprint`). Here: when Current is no
    longer recoverable there is nothing left to protect, so the hold is
    lifted; fingerprinted different content opens one ``cloud_only_conflict``
    problem with an immediate notification; problems whose hold ended are
    resolved by the notification reconciliation.
    """
    stamp = _stamp(observed_at or _now_iso())
    path_clause = "AND f.path=%s" if path is not None else ""
    params: list[Any] = [vault_id]
    if path is not None:
        params.append(path)
    released = connection.execute(
        f"""
        UPDATE files f SET held_version_id=NULL
        FROM archive_versions cur
        WHERE f.vault_id=%s {path_clause}
          AND cur.id=f.held_version_id
          AND NOT (cur.integrity='verified' AND cur.availability='available')
        """,
        params,
    ).rowcount
    held = connection.execute(
        f"""
        SELECT f.vault_id, f.id AS file_id, f.path, f.held_version_id,
               cur.provider_version_id
        FROM files f
        JOIN archive_versions cur ON cur.id=f.held_version_id
        WHERE f.vault_id=%s {path_clause}
          AND f.local_presence='present'
          AND f.local_sha256 IS NOT NULL
          AND f.local_sha256 <> cur.plaintext_sha256
        """,
        params,
    ).fetchall()
    summary = {"identical": 0, "conflicts": 0, "released": int(released or 0), "resolved": 0}
    for row in held:
        key = conflict_key(row["file_id"], row["held_version_id"])
        if _is_open(connection, key):
            continue
        notification_service.set_problem(
            connection, key=key, event=INCIDENT_KIND_CLOUD_ONLY_CONFLICT,
            vault_id=int(row["vault_id"]), active=True,
            params={"path": str(row["path"]), "observed_at": stamp},
        )
        summary["conflicts"] += 1
    return summary


class NoCloudOnlyConflict(LookupError):
    """No different content is held back at this path."""


def confirm_cloud_only_replacement(
    connection: Any,
    *,
    vault_id: int,
    path: str,
    actor: str,
    observed_at: Any = None,
) -> dict[str, Any]:
    """Release held-back content so its upload may replace the cloud copy.

    The Owner's explicit choice for a ``cloud_only_conflict``. The caller
    queues the upload; Current is replaced (and its exact cleanup frozen)
    only after the new version is verified.
    """
    from ..audit import audit_log

    settle_cloud_only_copies(
        connection, vault_id=vault_id, path=path, observed_at=observed_at,
    )
    row = connection.execute(
        """
        UPDATE files f SET held_version_id=NULL
        FROM files before
        WHERE f.id=before.id AND f.vault_id=%s AND f.path=%s
          AND f.held_version_id IS NOT NULL AND f.local_presence='present'
        RETURNING f.id AS file_id, before.held_version_id
        """,
        (vault_id, path),
    ).fetchone()
    if row is None:
        raise NoCloudOnlyConflict(path)
    key = conflict_key(row["file_id"], row["held_version_id"])
    notification_service.set_problem(
        connection, key=key, event=INCIDENT_KIND_CLOUD_ONLY_CONFLICT, active=False,
    )
    audit_log(
        "cloud_only_conflict_replacement_confirmed",
        connection=connection,
        vault_id=vault_id,
        actor=actor,
        vault_file_id=str(row["file_id"]),
        archive_version_id=str(row["held_version_id"]),
        path=path,
        outcome="confirmed",
    )
    return {
        "vault_id": vault_id,
        "file_id": str(row["file_id"]),
        "archive_version_id": str(row["held_version_id"]),
        "path": path,
    }
