"""Durable catalog incidents for unexpected Archive Version loss (issue #303)."""

from __future__ import annotations

from typing import Any, Mapping

from . import notifications as notification_service


INCIDENT_KIND_MISSING = "archive_version_missing"
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
