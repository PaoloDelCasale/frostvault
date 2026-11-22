"""Small reads and writes on service state that only tests need (#791)."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.services import notifications
from app.services import upload_provenance as provenance


def enqueue_archive_version_missing(connection: Any, *, incident: Mapping[str, Any]) -> int:
    """Notify the Owner of unexpected loss."""
    return notifications._enqueue_incident_notification(
        connection,
        incident=incident,
        event="archive_version_missing",
        params={
            "path": str(incident.get("path") or ""),
            "archive_version_id": str(incident.get("archive_version_id") or ""),
            "provider_version_id": str(incident.get("provider_version_id") or ""),
            "observed_at": str(incident.get("opened_at") or notifications.now_iso()),
            "reason": str(incident.get("reason") or "missing_in_cloud"),
        },
    )


def bound_snapshot(connection: Any, job_id: int) -> provenance.AdmittedSnapshot | None:
    """The snapshot a Job uploads or reconciles: its only source of expected proof."""
    bound = provenance._bound(connection, job_id)
    return bound[0] if bound else None


def proven_absent(connection: Any, attempt_id: str | None) -> bool:
    """True when this attempt was settled by S3's positive proof of absence."""
    if not attempt_id:
        return False
    return connection.execute(
        "SELECT 1 FROM upload_attempts WHERE id=%s AND outcome='no_write' AND no_write_operation=%s",
        (attempt_id, provenance.ABSENT_OPERATION),
    ).fetchone() is not None
