"""Reversible cloud archival and permanent purge gates (issue #10).

Public seam for vault cloud-deletion settings, selection preview,
confirmation phrases, scheduling, cancellation, delay acceleration, and
catalog side-effects. Workers call into this module for item expansion and
post-delete bookkeeping; S3 calls stay in ``app.storage``.
"""
from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .audit_events import record_audit_event
from .notifications import enqueue_notification
from .vault_governance import primary_owner


class CloudDeletionError(Exception):
    """Base error for cloud deletion gates."""


class CloudDeletionDisabled(CloudDeletionError):
    """Raised when the vault setting blocks cloud deletion."""


class ConfirmationRequired(CloudDeletionError):
    """Raised when vault-name / phrase confirmation is missing or wrong."""


class ReasonRequired(CloudDeletionError):
    """Raised when a permanent purge is requested without a reason."""


@dataclass(frozen=True)
class DeletionPreview:
    object_count: int
    version_count: int
    delete_marker_count: int
    byte_count: int

    def as_dict(self) -> dict[str, int]:
        return {
            "object_count": self.object_count,
            "version_count": self.version_count,
            "delete_marker_count": self.delete_marker_count,
            "byte_count": self.byte_count,
        }


@dataclass(frozen=True)
class ScheduledDeletion:
    group_id: str
    job_ids: list[int]
    preview: DeletionPreview
    pending_until: str | None = None


@dataclass(frozen=True)
class CancelledDeletion:
    cancelled_count: int
    group_id: str


@dataclass(frozen=True)
class AcceleratedDeletion:
    accelerated_count: int
    group_id: str


PHRASE_WORDS = (
    "amber",
    "birch",
    "cedar",
    "dawn",
    "ember",
    "fern",
    "grove",
    "harbor",
    "iris",
    "jade",
    "kelp",
    "lotus",
    "maple",
    "north",
    "orchid",
    "pine",
    "quartz",
    "river",
    "sage",
    "tide",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def generate_confirmation_phrase() -> str:
    first = secrets.choice(PHRASE_WORDS)
    second = secrets.choice(PHRASE_WORDS)
    number = secrets.randbelow(90) + 10
    return f"{first}-{second}-{number}"


def confirmation_matches(
    *,
    provided: str,
    vault_name: str,
    generated_phrase: str,
) -> bool:
    candidate = (provided or "").strip()
    if not candidate:
        return False
    return candidate in {vault_name.strip(), generated_phrase.strip()}


def delete_marker_explanation() -> str:
    """UI/docs copy: a Delete Marker hides a key; it never holds object bytes."""
    return (
        "A Delete Marker is a reversible cloud marker that hides the current key. "
        "It does not contain Archive Version data and cannot transition object data "
        "between storage classes. Noncurrent Archive Versions remain recoverable "
        "until a separate permanent purge."
    )


def is_cloud_deletion_enabled(connection: Any, vault_id: int) -> bool:
    row = connection.execute(
        "SELECT cloud_deletion_enabled FROM vaults WHERE id=%s",
        (vault_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Vault {vault_id} not found")
    return bool(row["cloud_deletion_enabled"])


def set_cloud_deletion_enabled(
    connection: Any,
    *,
    vault_id: int,
    enabled: bool,
    actor_user_id: int,
) -> bool:
    stamp = now_iso()
    connection.execute(
        """
        UPDATE vaults
        SET cloud_deletion_enabled=%s
        WHERE id=%s
        """,
        (enabled, vault_id),
    )
    record_audit_event(
        connection,
        event="cloud_deletion.setting_changed",
        actor_user_id=actor_user_id,
        vault_id=vault_id,
        outcome="success",
        enabled=bool(enabled),
        at=stamp,
    )
    _notify_vault_owners(
        connection,
        vault_id=vault_id,
        event="cloud_deletion.setting_changed",
        title="Cloud deletion setting updated",
        body=(
            "Cloud deletion was enabled for this vault."
            if enabled
            else "Cloud deletion was disabled for this vault."
        ),
        actor_user_id=actor_user_id,
    )
    return bool(enabled)


def _path_filter_clauses(
    *,
    paths: list[str],
    is_directory: bool,
) -> tuple[str, list[Any]]:
    if not paths:
        raise ValueError("At least one path is required")
    if len(paths) == 1 and not is_directory:
        return "fp.path=%s", [paths[0]]
    if len(paths) == 1 and is_directory:
        escaped = (
            paths[0]
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        return "(fp.path=%s OR fp.path LIKE %s ESCAPE '\\')", [
            paths[0],
            f"{escaped}/%",
        ]
    clauses: list[str] = []
    params: list[Any] = []
    for path in paths:
        if is_directory:
            escaped = (
                path.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            clauses.append("(fp.path=%s OR fp.path LIKE %s ESCAPE '\\')")
            params.extend([path, f"{escaped}/%"])
        else:
            clauses.append("fp.path=%s")
            params.append(path)
    return f"({' OR '.join(clauses)})", params


def _selected_files(
    connection: Any,
    *,
    vault_id: int,
    paths: list[str],
    is_directory: bool,
) -> list[dict[str, Any]]:
    path_sql, path_params = _path_filter_clauses(
        paths=paths, is_directory=is_directory
    )
    return connection.execute(
        f"""
        SELECT vf.id AS vault_file_id, fp.path
        FROM vault_files vf
        JOIN file_paths fp
          ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
        WHERE vf.vault_id=%s
          AND vf.status IN ('active', 'retired')
          AND {path_sql}
        ORDER BY lower(fp.path)
        """,
        [vault_id, *path_params],
    ).fetchall()


def preview_selection(
    connection: Any,
    *,
    vault_id: int,
    paths: list[str],
    is_directory: bool = False,
) -> DeletionPreview:
    files = _selected_files(
        connection,
        vault_id=vault_id,
        paths=paths,
        is_directory=is_directory,
    )
    if not files:
        return DeletionPreview(0, 0, 0, 0)
    file_ids = [row["vault_file_id"] for row in files]
    placeholders = ", ".join(["%s"] * len(file_ids))
    versions = connection.execute(
        f"""
        SELECT COUNT(*) AS total, COALESCE(SUM(size), 0) AS bytes
        FROM archive_versions
        WHERE vault_file_id IN ({placeholders})
          AND availability <> 'purged'
        """,
        file_ids,
    ).fetchone()
    markers = connection.execute(
        f"""
        SELECT COUNT(*) AS total
        FROM delete_markers
        WHERE vault_file_id IN ({placeholders})
        """,
        file_ids,
    ).fetchone()
    return DeletionPreview(
        object_count=len(files),
        version_count=int(versions["total"] or 0),
        delete_marker_count=int(markers["total"] or 0),
        byte_count=int(versions["bytes"] or 0),
    )


def _require_enabled(connection: Any, vault_id: int) -> None:
    if not is_cloud_deletion_enabled(connection, vault_id):
        raise CloudDeletionDisabled(
            "Cloud deletion is disabled for this vault"
        )


def _vault_name(connection: Any, vault_id: int) -> str:
    row = connection.execute(
        "SELECT name FROM vaults WHERE id=%s",
        (vault_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Vault {vault_id} not found")
    return str(row["name"])


def _notify_vault_owners(
    connection: Any,
    *,
    vault_id: int,
    event: str,
    title: str,
    body: str,
    actor_user_id: int | None,
    job_id: int | None = None,
) -> None:
    owner = primary_owner(connection, vault_id)
    recipients: set[int] = set()
    if owner is not None:
        recipients.add(int(owner["user_id"]))
    members = connection.execute(
        """
        SELECT user_id FROM vault_members
        WHERE vault_id=%s AND role='owner'
        """,
        (vault_id,),
    ).fetchall()
    for member in members:
        recipients.add(int(member["user_id"]))
    for user_id in recipients:
        enqueue_notification(
            connection,
            user_id=user_id,
            event=event,
            title=title,
            body=body,
            vault_id=vault_id,
            job_id=job_id,
        )


def schedule_cloud_archive(
    connection: Any,
    *,
    vault_id: int,
    paths: list[str],
    is_directory: bool,
    actor_user_id: int,
    requested_at: str,
) -> ScheduledDeletion:
    _require_enabled(connection, vault_id)
    files = _selected_files(
        connection,
        vault_id=vault_id,
        paths=paths,
        is_directory=is_directory,
    )
    if not files:
        raise ValueError("No Vault Files matched the selection")
    preview = preview_selection(
        connection,
        vault_id=vault_id,
        paths=paths,
        is_directory=is_directory,
    )
    group_id = str(uuid.uuid4())
    group_path = paths[0] if len(paths) == 1 else f"selection:{len(paths)}"
    job_ids: list[int] = []
    for row in files:
        # Skip files that already have no available current content to hide.
        latest = connection.execute(
            """
            SELECT id, object_key FROM archive_versions
            WHERE vault_file_id=%s AND availability='available'
            ORDER BY version_number DESC
            LIMIT 1
            """,
            (row["vault_file_id"],),
        ).fetchone()
        if latest is None:
            continue
        pending = connection.execute(
            """
            SELECT id FROM jobs
            WHERE vault_file_id=%s
              AND status NOT IN ('completed', 'failed', 'cancelled')
            """,
            (row["vault_file_id"],),
        ).fetchone()
        if pending:
            continue
        job = connection.execute(
            """
            INSERT INTO jobs(
                vault_id, vault_file_id, archive_version_id, path,
                action, status, requested_by, requested_at, updated_at,
                group_id, group_path, total_bytes, transferred_bytes
            ) VALUES (
                %s, %s, %s, %s,
                'cloud-archive', 'queued', %s, %s, %s,
                %s, %s, 0, 0
            )
            RETURNING id
            """,
            (
                vault_id,
                row["vault_file_id"],
                latest["id"],
                row["path"],
                actor_user_id,
                requested_at,
                requested_at,
                group_id,
                group_path,
            ),
        ).fetchone()
        job_ids.append(int(job["id"]))
        record_audit_event(
            connection,
            event="cloud_deletion.archive_requested",
            actor_user_id=actor_user_id,
            vault_id=vault_id,
            job_id=int(job["id"]),
            outcome="requested",
            path=row["path"],
            group_id=group_id,
        )
    if not job_ids:
        raise ValueError("No eligible Vault Files for reversible archival")
    _notify_vault_owners(
        connection,
        vault_id=vault_id,
        event="cloud_deletion.archive_requested",
        title="Reversible cloud archival requested",
        body=(
            f"Reversible archival queued for {len(job_ids)} Vault File(s). "
            + delete_marker_explanation()
        ),
        actor_user_id=actor_user_id,
        job_id=job_ids[0],
    )
    return ScheduledDeletion(
        group_id=group_id,
        job_ids=job_ids,
        preview=preview,
    )


def schedule_cloud_purge(
    connection: Any,
    *,
    vault_id: int,
    paths: list[str],
    is_directory: bool,
    actor_user_id: int,
    requested_at: str,
    confirmation: str,
    reason: str,
    generated_phrase: str,
    delay_seconds: int,
) -> ScheduledDeletion:
    _require_enabled(connection, vault_id)
    vault_name = _vault_name(connection, vault_id)
    if not confirmation_matches(
        provided=confirmation,
        vault_name=vault_name,
        generated_phrase=generated_phrase,
    ):
        raise ConfirmationRequired(
            "Confirm with the vault name or the generated phrase"
        )
    cleaned_reason = (reason or "").strip()
    if not cleaned_reason:
        raise ReasonRequired("A reason is required for permanent purge")
    if delay_seconds < 1:
        raise ValueError("delay_seconds must be positive")

    files = _selected_files(
        connection,
        vault_id=vault_id,
        paths=paths,
        is_directory=is_directory,
    )
    if not files:
        raise ValueError("No Vault Files matched the selection")
    preview = preview_selection(
        connection,
        vault_id=vault_id,
        paths=paths,
        is_directory=is_directory,
    )
    if preview.version_count == 0 and preview.delete_marker_count == 0:
        raise ValueError("Selection has nothing to purge")

    requested_dt = datetime.fromisoformat(requested_at)
    pending_until = (requested_dt + timedelta(seconds=delay_seconds)).isoformat()
    group_id = str(uuid.uuid4())
    group_path = paths[0] if len(paths) == 1 else f"selection:{len(paths)}"
    job_ids: list[int] = []

    for row in files:
        pending = connection.execute(
            """
            SELECT id FROM jobs
            WHERE vault_file_id=%s
              AND status NOT IN ('completed', 'failed', 'cancelled')
            """,
            (row["vault_file_id"],),
        ).fetchone()
        if pending:
            continue
        job = connection.execute(
            """
            INSERT INTO jobs(
                vault_id, vault_file_id, archive_version_id, path,
                action, status, requested_by, requested_at, updated_at,
                group_id, group_path, total_bytes, transferred_bytes,
                pending_until, reason, confirmation_phrase
            ) VALUES (
                %s, %s, NULL, %s,
                'cloud-purge', 'pending_delay', %s, %s, %s,
                %s, %s, %s, 0,
                %s, %s, %s
            )
            RETURNING id
            """,
            (
                vault_id,
                row["vault_file_id"],
                row["path"],
                actor_user_id,
                requested_at,
                requested_at,
                group_id,
                group_path,
                preview.byte_count,
                pending_until,
                cleaned_reason,
                confirmation.strip(),
            ),
        ).fetchone()
        job_id = int(job["id"])
        job_ids.append(job_id)
        _expand_purge_items(
            connection,
            job_id=job_id,
            vault_id=vault_id,
            vault_file_id=row["vault_file_id"],
            updated_at=requested_at,
        )
        record_audit_event(
            connection,
            event="cloud_deletion.purge_requested",
            actor_user_id=actor_user_id,
            vault_id=vault_id,
            job_id=job_id,
            outcome="requested",
            path=row["path"],
            reason=cleaned_reason,
            pending_until=pending_until,
            group_id=group_id,
            preview=preview.as_dict(),
        )

    if not job_ids:
        raise ValueError("No eligible Vault Files for permanent purge")

    _notify_vault_owners(
        connection,
        vault_id=vault_id,
        event="cloud_deletion.purge_requested",
        title="Permanent cloud purge requested",
        body=(
            f"Permanent purge of {preview.version_count} Archive Version(s) and "
            f"{preview.delete_marker_count} Delete Marker(s) is delayed until "
            f"{pending_until}. Cancel before then to prevent all deletion calls."
        ),
        actor_user_id=actor_user_id,
        job_id=job_ids[0],
    )
    return ScheduledDeletion(
        group_id=group_id,
        job_ids=job_ids,
        preview=preview,
        pending_until=pending_until,
    )


def _expand_purge_items(
    connection: Any,
    *,
    job_id: int,
    vault_id: int,
    vault_file_id: str,
    updated_at: str,
) -> None:
    versions = connection.execute(
        """
        SELECT id, object_key, provider_version_id, size
        FROM archive_versions
        WHERE vault_file_id=%s AND availability <> 'purged'
        ORDER BY version_number
        """,
        (vault_file_id,),
    ).fetchall()
    for version in versions:
        connection.execute(
            """
            INSERT INTO cloud_deletion_items(
                job_id, vault_id, vault_file_id, kind, archive_version_id,
                delete_marker_id, object_key, provider_version_id, size_bytes,
                status, updated_at
            ) VALUES (
                %s, %s, %s, 'version', %s,
                NULL, %s, %s, %s,
                'pending', %s
            )
            """,
            (
                job_id,
                vault_id,
                vault_file_id,
                version["id"],
                version["object_key"],
                version["provider_version_id"],
                version["size"],
                updated_at,
            ),
        )
    markers = connection.execute(
        """
        SELECT id, object_key, provider_version_id
        FROM delete_markers
        WHERE vault_file_id=%s
        ORDER BY created_at, id
        """,
        (vault_file_id,),
    ).fetchall()
    for marker in markers:
        connection.execute(
            """
            INSERT INTO cloud_deletion_items(
                job_id, vault_id, vault_file_id, kind, archive_version_id,
                delete_marker_id, object_key, provider_version_id, size_bytes,
                status, updated_at
            ) VALUES (
                %s, %s, %s, 'delete_marker', NULL,
                %s, %s, %s, 0,
                'pending', %s
            )
            """,
            (
                job_id,
                vault_id,
                vault_file_id,
                marker["id"],
                marker["object_key"],
                marker["provider_version_id"],
                updated_at,
            ),
        )


def accelerate_cloud_purge(
    connection: Any,
    *,
    vault_id: int,
    group_id: str,
    actor_user_id: int,
    accelerated_at: str,
) -> AcceleratedDeletion:
    """Skip the cancellable delay and queue permanent purge for immediate work."""
    rows = connection.execute(
        """
        SELECT id, status, path FROM jobs
        WHERE vault_id=%s AND group_id=%s
          AND action='cloud-purge'
          AND status='pending_delay'
        """,
        (vault_id, group_id),
    ).fetchall()
    if not rows:
        raise ValueError("No delayed permanent purge jobs in this group")
    job_ids = [int(row["id"]) for row in rows]
    placeholders = ", ".join(["%s"] * len(job_ids))
    connection.execute(
        f"""
        UPDATE jobs
        SET status='queued',
            pending_until=%s,
            message=%s,
            message_key=%s,
            message_params=%s,
            updated_at=%s
        WHERE id IN ({placeholders})
          AND status='pending_delay'
        """,
        [
            accelerated_at,
            "Purge delay skipped; starting permanent deletion",
            "job.cloud_purge_accelerated",
            "{}",
            accelerated_at,
            *job_ids,
        ],
    )
    for row in rows:
        record_audit_event(
            connection,
            event="cloud_deletion.purge_accelerated",
            actor_user_id=actor_user_id,
            vault_id=vault_id,
            job_id=int(row["id"]),
            outcome="accelerated",
            path=row["path"],
            previous_status=row["status"],
            group_id=group_id,
        )
    _notify_vault_owners(
        connection,
        vault_id=vault_id,
        event="cloud_deletion.purge_accelerated",
        title="Permanent cloud purge accelerated",
        body=(
            "The cancellable purge delay was skipped; permanent deletion "
            "calls will start as soon as a worker picks up the Job."
        ),
        actor_user_id=actor_user_id,
        job_id=job_ids[0],
    )
    return AcceleratedDeletion(
        accelerated_count=len(job_ids),
        group_id=group_id,
    )


def cancel_cloud_deletion(
    connection: Any,
    *,
    vault_id: int,
    group_id: str,
    actor_user_id: int,
    cancelled_at: str,
) -> CancelledDeletion:
    rows = connection.execute(
        """
        SELECT id, action, status FROM jobs
        WHERE vault_id=%s AND group_id=%s
          AND action IN ('cloud-archive', 'cloud-purge')
          AND status NOT IN ('completed', 'failed', 'cancelled')
        """,
        (vault_id, group_id),
    ).fetchall()
    if not rows:
        raise ValueError("No cancellable cloud deletion jobs in this group")
    job_ids = [int(row["id"]) for row in rows]
    placeholders = ", ".join(["%s"] * len(job_ids))
    connection.execute(
        f"""
        UPDATE jobs
        SET status='cancelled',
            message=%s,
            updated_at=%s
        WHERE id IN ({placeholders})
          AND status NOT IN ('completed', 'failed', 'cancelled')
        """,
        [
            "Cloud deletion cancelled before execution",
            cancelled_at,
            *job_ids,
        ],
    )
    connection.execute(
        f"""
        UPDATE cloud_deletion_items
        SET status='skipped', updated_at=%s
        WHERE job_id IN ({placeholders}) AND status='pending'
        """,
        [cancelled_at, *job_ids],
    )
    for row in rows:
        record_audit_event(
            connection,
            event="cloud_deletion.cancelled",
            actor_user_id=actor_user_id,
            vault_id=vault_id,
            job_id=int(row["id"]),
            outcome="cancelled",
            action=row["action"],
            previous_status=row["status"],
            group_id=group_id,
        )
    _notify_vault_owners(
        connection,
        vault_id=vault_id,
        event="cloud_deletion.cancelled",
        title="Cloud deletion cancelled",
        body="The delayed cloud deletion was cancelled; no deletion calls were made.",
        actor_user_id=actor_user_id,
        job_id=job_ids[0],
    )
    return CancelledDeletion(cancelled_count=len(job_ids), group_id=group_id)


def mark_item_deleted(
    connection: Any,
    *,
    item_id: int,
    updated_at: str,
) -> None:
    item = connection.execute(
        "SELECT * FROM cloud_deletion_items WHERE id=%s",
        (item_id,),
    ).fetchone()
    if item is None:
        raise ValueError(f"cloud deletion item {item_id} not found")
    connection.execute(
        """
        UPDATE cloud_deletion_items
        SET status='deleted', error_message=NULL, updated_at=%s
        WHERE id=%s
        """,
        (updated_at, item_id),
    )
    if item["kind"] == "version" and item["archive_version_id"]:
        connection.execute(
            """
            UPDATE archive_versions
            SET availability='purged', availability_checked_at=%s
            WHERE id=%s
            """,
            (updated_at, item["archive_version_id"]),
        )


def mark_item_failed(
    connection: Any,
    *,
    item_id: int,
    error_message: str,
    updated_at: str,
) -> None:
    connection.execute(
        """
        UPDATE cloud_deletion_items
        SET status='failed', error_message=%s, updated_at=%s
        WHERE id=%s
        """,
        (error_message, updated_at, item_id),
    )


def pending_items_for_job(connection: Any, job_id: int) -> list[dict[str, Any]]:
    return connection.execute(
        """
        SELECT * FROM cloud_deletion_items
        WHERE job_id=%s AND status IN ('pending', 'failed')
        ORDER BY id
        """,
        (job_id,),
    ).fetchall()


def finalize_purge_job(
    connection: Any,
    *,
    job_id: int,
    vault_file_id: str,
    actor_user_id: int | None,
    updated_at: str,
) -> str:
    """Mark job completed/failed and retire the Vault File tombstone when done."""
    summary = connection.execute(
        """
        SELECT
            SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
            SUM(CASE WHEN status='deleted' THEN 1 ELSE 0 END) AS deleted
        FROM cloud_deletion_items
        WHERE job_id=%s
        """,
        (job_id,),
    ).fetchone()
    pending = int(summary["pending"] or 0)
    failed = int(summary["failed"] or 0)
    deleted = int(summary["deleted"] or 0)
    job = connection.execute(
        "SELECT vault_id FROM jobs WHERE id=%s",
        (job_id,),
    ).fetchone()
    vault_id = int(job["vault_id"]) if job else None

    if pending == 0 and failed == 0:
        remaining = connection.execute(
            """
            SELECT COUNT(*) AS total FROM archive_versions
            WHERE vault_file_id=%s AND availability <> 'purged'
            """,
            (vault_file_id,),
        ).fetchone()["total"]
        if int(remaining or 0) == 0:
            connection.execute(
                """
                UPDATE vault_files
                SET status='purged', retired_at=COALESCE(retired_at, %s)
                WHERE id=%s
                """,
                (updated_at, vault_file_id),
            )
        connection.execute(
            """
            UPDATE jobs
            SET status='completed',
                message=%s,
                transferred_bytes=total_bytes,
                updated_at=%s
            WHERE id=%s
            """,
            (
                f"Permanently purged {deleted} cloud object version(s)/marker(s)",
                updated_at,
                job_id,
            ),
        )
        record_audit_event(
            connection,
            event="cloud_deletion.purge_completed",
            actor_user_id=actor_user_id,
            vault_id=vault_id,
            job_id=job_id,
            outcome="success",
            deleted=deleted,
        )
        if vault_id is not None:
            _notify_vault_owners(
                connection,
                vault_id=vault_id,
                event="cloud_deletion.purge_completed",
                title="Permanent cloud purge completed",
                body=f"Permanently purged {deleted} version(s)/marker(s).",
                actor_user_id=actor_user_id,
                job_id=job_id,
            )
        return "completed"

    message = (
        f"Permanent purge partially failed: {failed} failure(s), "
        f"{deleted} deleted, {pending} pending"
    )
    connection.execute(
        """
        UPDATE jobs
        SET status='failed', message=%s, updated_at=%s
        WHERE id=%s
        """,
        (message, updated_at, job_id),
    )
    record_audit_event(
        connection,
        event="cloud_deletion.purge_partial_failure",
        actor_user_id=actor_user_id,
        vault_id=vault_id,
        job_id=job_id,
        outcome="partial_failure",
        failed=failed,
        deleted=deleted,
        pending=pending,
    )
    if vault_id is not None:
        _notify_vault_owners(
            connection,
            vault_id=vault_id,
            event="cloud_deletion.purge_partial_failure",
            title="Permanent cloud purge partial failure",
            body=message,
            actor_user_id=actor_user_id,
            job_id=job_id,
        )
    return "failed"


def record_archive_completed(
    connection: Any,
    *,
    job_id: int,
    vault_id: int,
    path: str,
    marker_version_id: str,
    actor_user_id: int | None,
    updated_at: str,
) -> None:
    record_audit_event(
        connection,
        event="cloud_deletion.archive_completed",
        actor_user_id=actor_user_id,
        vault_id=vault_id,
        job_id=job_id,
        outcome="success",
        path=path,
        delete_marker_version_id=marker_version_id,
    )
    # Best-effort notify (parity with local cleanup observability): a notify
    # failure must not roll back the Delete Marker catalog row or fail the job
    # into a retry that issues another delete_object.
    try:
        _notify_vault_owners(
            connection,
            vault_id=vault_id,
            event="cloud_deletion.archive_completed",
            title="Reversible cloud archival completed",
            body=(
                f"A Delete Marker hid {path}. "
                + delete_marker_explanation()
            ),
            actor_user_id=actor_user_id,
            job_id=job_id,
        )
    except Exception as exc:
        try:
            from . import worker_errors as worker_error_store

            worker_error_store.record_worker_error(
                connection,
                component="cloud_archive_observability",
                exc=exc,
                vault_id=vault_id,
                job_id=job_id,
                event="cloud_deletion.archive_completed",
            )
        except Exception:
            pass
