"""Durable catalog incidents that need the owner.

``archive_version_missing``: unexpected Archive Version loss (#303).
``cloud_only_conflict``: different content at the path of a file whose only
copy is in the cloud; it is held back from upload (#411, #363 D7).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from . import notifications as notification_service


INCIDENT_KIND_MISSING = "archive_version_missing"
INCIDENT_KIND_CLOUD_ONLY_CONFLICT = "cloud_only_conflict"
INCIDENT_STATUS_OPEN = "open"
INCIDENT_STATUS_RESOLVED = "resolved"


def remember_missing_archives(
    connection: Any,
    rows: list[Mapping[str, Any]],
    *,
    reason: str,
    observed_at: str,
) -> list[dict[str, Any]]:
    """Open or refresh one incident per newly missing Archive Version.

    Repeated scans update ``last_seen_at`` without a second open incident or
    a second notification. Authorized purge rows never reach this helper.
    """
    opened: list[dict[str, Any]] = []
    for row in rows:
        version_id = str(row["id"])
        existing = connection.execute(
            """
            SELECT * FROM catalog_incidents
            WHERE archive_version_id=%s AND kind=%s AND status=%s
            """,
            (version_id, INCIDENT_KIND_MISSING, INCIDENT_STATUS_OPEN),
        ).fetchone()
        if existing:
            connection.execute(
                """
                UPDATE catalog_incidents
                SET last_seen_at=%s, reason=%s
                WHERE id=%s
                """,
                (observed_at, reason, existing["id"]),
            )
            continue
        inserted = connection.execute(
            """
            INSERT INTO catalog_incidents(
                vault_id, vault_file_id, archive_version_id, kind, status,
                reason, path, object_key, provider_version_id,
                opened_at, last_seen_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                int(row["vault_id"]),
                str(row["vault_file_id"]),
                version_id,
                INCIDENT_KIND_MISSING,
                INCIDENT_STATUS_OPEN,
                reason,
                str(row.get("path") or ""),
                row.get("object_key"),
                row.get("provider_version_id"),
                observed_at,
                observed_at,
            ),
        ).fetchone()
        if inserted is None:
            continue
        incident = dict(inserted)
        notification_service.enqueue_archive_version_missing(
            connection, incident=incident
        )
        opened.append(incident)
    return opened


def resolve_archive_version_incidents(
    connection: Any,
    archive_version_ids: list[str],
    *,
    observed_at: str,
) -> int:
    """Mark open missing-archive incidents resolved once protection is valid."""
    resolved = 0
    for version_id in archive_version_ids:
        result = connection.execute(
            """
            UPDATE catalog_incidents
            SET status=%s, resolved_at=%s, last_seen_at=%s
            WHERE archive_version_id=%s AND kind=%s AND status=%s
            """,
            (
                INCIDENT_STATUS_RESOLVED,
                observed_at,
                observed_at,
                str(version_id),
                INCIDENT_KIND_MISSING,
                INCIDENT_STATUS_OPEN,
            ),
        )
        resolved += int(getattr(result, "rowcount", 0) or 0)
    return resolved


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open_cloud_only_conflict(
    connection: Any, row: Mapping[str, Any], *, observed_at: str
) -> dict[str, Any] | None:
    existing = connection.execute(
        """
        SELECT id FROM catalog_incidents
        WHERE archive_version_id=%s AND kind=%s AND status=%s
        """,
        (
            str(row["cloud_only_version_id"]),
            INCIDENT_KIND_CLOUD_ONLY_CONFLICT,
            INCIDENT_STATUS_OPEN,
        ),
    ).fetchone()
    if existing:
        connection.execute(
            "UPDATE catalog_incidents SET last_seen_at=%s WHERE id=%s",
            (observed_at, existing["id"]),
        )
        return None
    inserted = connection.execute(
        """
        INSERT INTO catalog_incidents(
            vault_id, vault_file_id, archive_version_id, kind, status,
            reason, path, object_key, provider_version_id,
            opened_at, last_seen_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            int(row["vault_id"]),
            str(row["vault_file_id"]),
            str(row["cloud_only_version_id"]),
            INCIDENT_KIND_CLOUD_ONLY_CONFLICT,
            INCIDENT_STATUS_OPEN,
            "different_content",
            str(row["path"]),
            row.get("object_key"),
            row.get("provider_version_id"),
            observed_at,
            observed_at,
        ),
    ).fetchone()
    if inserted is None:
        return None
    incident = dict(inserted)
    notification_service.enqueue_cloud_only_conflict(
        connection, incident=incident
    )
    return incident


def settle_cloud_only_copies(
    connection: Any,
    *,
    vault_id: int,
    path: str | None = None,
    observed_at: str | None = None,
) -> dict[str, int]:
    """Decide hashed content that reappeared at a cloud-only path.

    Identical content becomes a Local Copy of the cloud-only Archive Version
    with no upload. Different content stays held back (never uploaded) and
    opens one ``cloud_only_conflict`` incident with an immediate notification.
    When that version is no longer recoverable there is nothing left to
    protect, so the hold is lifted. Open conflicts whose content left the path
    (renamed or deleted), or whose hold was lifted, are resolved.
    """
    stamp = observed_at or _now_iso()
    path_clause = "AND fp.path=%s" if path is not None else ""
    params: list[Any] = [vault_id]
    if path is not None:
        params.append(path)
    rows = connection.execute(
        f"""
        SELECT vf.vault_id, lc.vault_file_id, fp.path, lc.presence,
               lc.file_type, lc.plaintext_sha256, lc.cloud_only_version_id,
               av.plaintext_sha256 AS version_sha256, av.integrity,
               av.availability, av.object_key, av.provider_version_id
        FROM local_copies lc
        JOIN vault_files vf ON vf.id=lc.vault_file_id
        JOIN file_paths fp
          ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
        LEFT JOIN archive_versions av ON av.id=lc.cloud_only_version_id
        WHERE fp.vault_id=%s
          AND vf.vault_id=fp.vault_id
          AND lc.cloud_only_version_id IS NOT NULL
          {path_clause}
        """,
        params,
    ).fetchall()
    summary = {"identical": 0, "conflicts": 0, "released": 0, "resolved": 0}
    for row in rows:
        if row["integrity"] != "verified" or row["availability"] != "available":
            connection.execute(
                """
                UPDATE local_copies SET cloud_only_version_id=NULL
                WHERE vault_file_id=%s AND cloud_only_version_id=%s
                """,
                (row["vault_file_id"], row["cloud_only_version_id"]),
            )
            summary["released"] += 1
            continue
        if (
            row["presence"] != "present"
            or row["file_type"] != "regular"
            or not row["plaintext_sha256"]
        ):
            continue
        if str(row["plaintext_sha256"]).lower() == str(
            row["version_sha256"] or ""
        ).lower():
            connection.execute(
                """
                UPDATE local_copies
                SET matched_archive_version_id=cloud_only_version_id,
                    cloud_only_version_id=NULL
                WHERE vault_file_id=%s AND cloud_only_version_id=%s
                """,
                (row["vault_file_id"], row["cloud_only_version_id"]),
            )
            summary["identical"] += 1
            continue
        if _open_cloud_only_conflict(connection, row, observed_at=stamp):
            summary["conflicts"] += 1
    resolve_params: list[Any] = [
        INCIDENT_STATUS_RESOLVED,
        stamp,
        stamp,
        vault_id,
        INCIDENT_KIND_CLOUD_ONLY_CONFLICT,
        INCIDENT_STATUS_OPEN,
    ]
    resolve_path = ""
    if path is not None:
        resolve_path = "AND catalog_incidents.path=%s"
        resolve_params.append(path)
    result = connection.execute(
        f"""
        UPDATE catalog_incidents
        SET status=%s, resolved_at=%s, last_seen_at=%s
        WHERE vault_id=%s AND kind=%s AND status=%s
          {resolve_path}
          AND NOT EXISTS (
              SELECT 1 FROM local_copies lc
              WHERE lc.vault_file_id=catalog_incidents.vault_file_id
                AND lc.cloud_only_version_id=catalog_incidents.archive_version_id
                AND lc.presence='present'
          )
        """,
        resolve_params,
    )
    summary["resolved"] = int(getattr(result, "rowcount", 0) or 0)
    return summary


class NoCloudOnlyConflict(LookupError):
    """No different content is held back at this path."""


def confirm_cloud_only_replacement(
    connection: Any,
    *,
    vault_id: int,
    path: str,
    actor_user_id: int | None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Release held-back content so its upload may replace the cloud copy.

    The owner's explicit choice for a ``cloud_only_conflict``. The caller
    queues the upload; Current Snapshot deletes the previous version only
    after the new one is verified.
    """
    from ..audit import audit_log

    stamp = observed_at or _now_iso()
    settle_cloud_only_copies(
        connection, vault_id=vault_id, path=path, observed_at=stamp
    )
    incident = connection.execute(
        """
        SELECT * FROM catalog_incidents
        WHERE vault_id=%s AND path=%s AND kind=%s AND status=%s
        ORDER BY id DESC
        LIMIT 1
        """,
        (vault_id, path, INCIDENT_KIND_CLOUD_ONLY_CONFLICT, INCIDENT_STATUS_OPEN),
    ).fetchone()
    if incident is None:
        raise NoCloudOnlyConflict(path)
    released = connection.execute(
        """
        UPDATE local_copies SET cloud_only_version_id=NULL
        WHERE vault_file_id=%s AND cloud_only_version_id=%s
          AND presence='present'
        """,
        (incident["vault_file_id"], incident["archive_version_id"]),
    )
    if not int(getattr(released, "rowcount", 0) or 0):
        raise NoCloudOnlyConflict(path)
    connection.execute(
        """
        UPDATE catalog_incidents
        SET status=%s, resolved_at=%s, last_seen_at=%s
        WHERE id=%s
        """,
        (INCIDENT_STATUS_RESOLVED, stamp, stamp, incident["id"]),
    )
    audit_log(
        "cloud_only_conflict_replacement_confirmed",
        connection=connection,
        vault_id=vault_id,
        actor_user_id=actor_user_id,
        vault_file_id=incident["vault_file_id"],
        archive_version_id=incident["archive_version_id"],
        path=path,
        outcome="confirmed",
    )
    return dict(incident)
