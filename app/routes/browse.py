"""Browse and overview routes: files, statistics, Problems, notifications, Recovery Kit and metadata backups."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..api_models import JsonObjectResponse, response_model
from ..config import settings
from ..i18n import translate
from ..catalog import ArchiveCatalog
from ..database import db
from ..errors import NotFound
from ..services.source_layout import vault_local_access
from ..services import audit_events as audit_event_store
from ..services import metadata_backups as metadata_backup_service
from ..services import recovery_kit as recovery_kit_service
from ..services import notifications as notification_service
from ..services.vaults import VAULT_ROW_SQL
from ..services.runtime_status import runtime_status, snapshot_runtime_status_for_stats, status_lock
from ..services.vault_paths import safe_relative_path
from .deps import (
    LOGICAL_PATH_ERROR_RESPONSES,
    _OPERATIONAL_VAULT,
    _actor,
    _request_locale,
    current_user,
    current_vault,
    normalize_directory,
    require_recent_reauth,
)


router = APIRouter()


def _protection_observation(connection: Any, vault: dict[str, Any], access: Any) -> tuple[Any, bool]:
    """Where the Vault is shown unconfirmed, and whether upload is manual.

    The same rule as the local release gate (ADR-0042, #684): global causes
    leave the whole Vault unconfirmed, a named unreadable path only itself,
    its contents and the folders above it.
    """
    from ..services.operation_policies import get_policy, unconfirmed_scope

    unconfirmed = unconfirmed_scope(
        connection, int(vault["id"]), lifecycle=vault.get("lifecycle", "active"),
        local_operations_allowed=access.local_operations_allowed,
    )
    manual_upload = not get_policy(connection, int(vault["id"])).auto_upload
    return unconfirmed, manual_upload


@router.get(
    "/api/files",
    response_model=response_model("FilesResponse"),
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def list_files(
    q: str = "",
    state: str = "",
    directory: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=10, le=500),
    vault: dict[str, Any] = Depends(current_vault),
):
    """List Vault Files for the authenticated Owner's selected Vault."""
    directory = normalize_directory(directory, api=True)
    access = vault_local_access(vault.get("source_root") or "")
    with db() as connection:
        unconfirmed, manual_upload = _protection_observation(connection, vault, access)
        catalog = ArchiveCatalog(connection, protection_unconfirmed=unconfirmed)
        listing = catalog.list_files_page(
            vault["id"],
            search=q,
            directory=directory,
            state=state,
            page=page,
            page_size=page_size,
        )
        items = listing["items"]
        total = listing["total"]

    return {
        "items": items,
        "total": total,
        "page": page,
        "directory": directory,
        "mode": listing["mode"],
        "aggregate_status": listing.get("aggregate_status", "ready"),
        "manual_upload": manual_upload,
    }


@router.get("/api/files/detail", response_model=response_model("ArchiveListItem"), responses=LOGICAL_PATH_ERROR_RESPONSES)
def file_detail(path: str, vault: dict[str, Any] = Depends(current_vault)):
    path = safe_relative_path(path).as_posix()
    with db() as connection:
        access = vault_local_access(vault.get("source_root") or "")
        unconfirmed, _ = _protection_observation(connection, vault, access)
        catalog = ArchiveCatalog(connection, protection_unconfirmed=unconfirmed)
        item = catalog.get_file_item(vault["id"], path)
        if item is None:
            raise NotFound("api.file_not_found")
        return item


@router.get("/api/files/search", response_model=response_model("FilesResponse"))
def search_all_files(
    q: str = "", state: str = "", page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=10, le=500),
    _: dict[str, Any] = Depends(current_user),
):
    """Search name/path across all protected Vaults, independent of Session choice."""
    with db() as connection:
        vaults = connection.execute(f"{VAULT_ROW_SQL} WHERE {_OPERATIONAL_VAULT}").fetchall()
        scoped = []
        for vault in vaults:
            access = vault_local_access(vault.get("source_root") or "")
            unconfirmed, _manual = _protection_observation(connection, vault, access)
            scoped.append({**vault, "unconfirmed": unconfirmed})
        return ArchiveCatalog(connection).search_protected_vaults(
            scoped, search=q, state=state, page=page, page_size=page_size,
        )


@router.get("/api/upload-activity", response_model=response_model("UploadActivityResponse"))
def upload_activity_overview(_: dict[str, Any] = Depends(current_user)):
    from ..services import source_devices, throughput
    from ..services.upload_activity import upload_activity
    with db() as connection:
        result = upload_activity(connection)
    # Speeds and one expected finish per Vault, from in-memory samples (#755).
    throughput.annotate(result)
    # Uploads held back while the server is busy (#713).
    priority = source_devices.server_priority().status()
    return {**result, "server_priority": {"throttled": priority["throttled"],
                                          "since": priority["since"]}}


@router.get("/api/general", response_model=response_model("GeneralOverview"))
def general_overview(request: Request, user: dict[str, Any] = Depends(current_user)):
    from ..services.essential_interface import overview
    with db() as connection:
        return overview(connection, locale=_request_locale(request))


@router.get("/api/problems", response_model=response_model("NotificationsResponse"))
def open_problems(request: Request, page: int = Query(1, ge=1),
                  user: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        notification_service.reconcile_problem_states(connection)
        items = notification_service.open_problems(
            connection, locale=_request_locale(request), page=page)
        return {"items": [{**item, "resolving": _problem_resolving(item)} for item in items],
                "open_problem_count": notification_service.count_open_problems(connection)}


def _problem_resolving(item: dict[str, Any]) -> str | None:
    """The running operation that can close this problem by itself (#711).

    A blocked source closes when a complete scan succeeds: while one runs,
    the Owner reads that it is in progress instead of what to check.
    """
    params = item.get("message_params") or {}
    if (item.get("event") != "protection_blocked" or params.get("component") != "source"
            or not item.get("vault_id")):
        return None
    with status_lock:
        scanning = bool((runtime_status.get(int(item["vault_id"])) or {}).get("scanning"))
    return "scan" if scanning else None


@router.get("/api/stats", response_model=response_model("StatsResponse"))
def stats(vault: dict[str, Any] = Depends(current_vault), request: Request = None):  # type: ignore[assignment]
    source_root = vault.get("source_root") or ""
    # Reconcile Source Volume identity before anything can preflight or walk the
    # configured Vault tree. Catalog summaries remain available when local work
    # is suspended, so they are intentionally collected after this gate too.
    access = vault_local_access(source_root)
    with db() as connection:
        # Cheap SQL aggregates only — never materialize every Vault File row.
        unconfirmed, manual_upload = _protection_observation(connection, vault, access)
        summary = ArchiveCatalog(connection, protection_unconfirmed=unconfirmed).summary(vault["id"])
        from ..services.component_health import (
            load_vault_health,
            runtime_fields_from_health,
        )

        persisted_health = runtime_fields_from_health(
            load_vault_health(connection, int(vault["id"]))
        )
    # Consistent bounded snapshot under the producer lock, then release before
    # any merge/response work. Never dict(runtime_status[...]) unlocked while
    # storage mutates filesystem under status_lock.
    runtime = snapshot_runtime_status_for_stats(int(vault["id"]))
    for key, value in persisted_health.items():
        if key not in runtime or runtime.get(key) in (None, ""):
            runtime[key] = value
    if not runtime.get("last_error"):
        for component in ("source", "cloud", "policy", "audit"):
            error = runtime.get(f"last_error_{component}")
            if error:
                runtime["last_error"] = error
                break
    # The known cause of the last failed source scan, to read it translated (#711).
    from ..services.scan_causes import source_error_cause
    runtime["last_error_source_cause"] = source_error_cause(runtime.get("last_error_source"))
    # Installation-wide read scheduling (#713): server priority and the reads
    # in flight per Source Device.
    from ..services import source_devices
    runtime.update(source_devices.status())
    if unconfirmed.by_path:
        # Only named paths are unconfirmed (ADR-0042, #684): say how many and
        # where to look, instead of the generic incomplete-scan error.
        from ..services.operation_policies import PATH_SCOPED_SOURCE_ERRORS
        runtime["unreadable_paths"] = len(unconfirmed.paths or ())
        scoped = translate(
            "vault.health.unreadable_paths", locale=_request_locale(request),
            count=runtime["unreadable_paths"],
        )
        if not runtime.get("last_error") or runtime["last_error"] in PATH_SCOPED_SOURCE_ERRORS:
            runtime["last_error"] = scoped
        if runtime.get("last_error_source") in PATH_SCOPED_SOURCE_ERRORS:
            runtime["last_error_source"] = scoped
    return {
        **summary,
        "manual_upload": manual_upload,
        "runtime": runtime,
        "source_volume": {
            "alias": access.volume_alias,
            "health": access.volume_health,
            "local_operations_allowed": access.local_operations_allowed,
        },
    }


@router.get("/api/notifications", response_model=response_model("NotificationsResponse"))
def list_notifications(
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    limit: int = Query(50, ge=1, le=200),
    status: Literal["all", "unread", "read"] = Query("all"),
    before_id: int | None = Query(default=None, ge=1),
):
    """List the Owner's in-app notifications.

    Supports server-side ``status`` filtering (``unread`` / ``read`` / ``all``)
    and ``before_id`` cursor pagination so older unread items are never hidden
    behind a mixed newest page (issue #225).
    """
    locale = _request_locale(request)
    with db() as connection:
        # Fetch one extra row (allowed as an internal 201-row sentinel) so the
        # client can offer bounded "load more" even at limit=200.
        fetched = notification_service.list_in_app_notifications(
            connection,
            limit=limit + 1,
            locale=locale,
            status=status,
            before_id=before_id,
        )
        has_more = len(fetched) > limit
        items = fetched[:limit]
        unread_count = notification_service.count_unread_notifications(connection)
        open_problem_count = notification_service.count_open_problems(connection)
    return {"items": items, "unread_count": unread_count, "has_more": has_more,
            "open_problem_count": open_problem_count,
            "email_configured": notification_service.email_configured()}


class NotificationReadAction(BaseModel):
    notification_id: int


@router.post("/api/notifications/read", response_model=JsonObjectResponse)
def mark_notification_read(
    action: NotificationReadAction,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
):
    with db() as connection:
        item = notification_service.mark_notification_read(
            connection,
            notification_id=action.notification_id,
            locale=_request_locale(request),
        )
    if item is None:
        raise NotFound("api.notification_not_found")
    return item


@router.post("/api/notifications/read-all", response_model=JsonObjectResponse)
def mark_all_notifications_read(
    user: dict[str, Any] = Depends(current_user),
):
    """Mark every currently visible unread notification read (issue #225).

    Server-authoritative and idempotent; preserves the same in-app
    visibility checks as single-item mark-read.
    """
    del user  # authentication gate only
    with db() as connection:
        return notification_service.mark_all_notifications_read(connection)


class MetadataBackupRunAction(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class RecoveryKitCustody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fingerprint: str = Field(min_length=24, max_length=24, pattern="^[0-9a-f]+$")


@router.get("/api/recovery-kit", response_model=response_model("RecoveryKitStatus"))
def recovery_kit_status(_: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        return recovery_kit_service.kit_status(connection)


@router.get("/api/recovery-kit/download", response_class=Response, responses={200: {
    "description": "Secret-bearing installation Recovery Kit (never cache)",
    "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
}})
def download_recovery_kit(
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    with db() as connection:
        bucket = recovery_kit_service.kit_bucket(connection)
    payload = recovery_kit_service.build_recovery_kit(bucket=bucket)
    with db() as connection:
        audit_event_store.record_audit_event(connection, event="recovery_kit_downloaded",
                           actor=_actor(request), outcome="success")
    return Response(payload, media_type="application/octet-stream", headers={
        "Content-Disposition": 'attachment; filename="frostvault-recovery-kit.json"',
        "Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff",
    })


@router.post("/api/recovery-kit/custody", response_model=response_model("RecoveryKitStatus"))
def acknowledge_recovery_kit(
    action: RecoveryKitCustody,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
):
    with db() as connection:
        recovery_kit_service.acknowledge_custody(connection, action.fingerprint)
        audit_event_store.record_audit_event(connection, event="recovery_kit_custody_acknowledged",
                           actor=_actor(request), outcome="success",
                           fingerprint=action.fingerprint)
        return recovery_kit_service.kit_status(connection)


@router.get("/api/general/metadata-backups", response_model=JsonObjectResponse)
def list_metadata_backups(_: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        status = metadata_backup_service.backup_status(connection)
        runs = metadata_backup_service.list_backup_artifacts(connection)
    return {"status": status, "runs": runs}


@router.post("/api/general/metadata-backups/run", response_model=JsonObjectResponse)
def run_metadata_backup(
    action: MetadataBackupRunAction,
    request: Request,
    admin: dict[str, Any] = Depends(current_user),
):
    # Resolve the System Bucket before holding a connection for the run (#734).
    object_store = metadata_backup_service.resolve_object_store()
    with db() as connection:
        result = metadata_backup_service.run_metadata_backup(
            connection,
            reason="manual",
            backup_dir=settings.metadata_backup_dir,
            object_store=object_store,
            retention=settings.metadata_backup_retention,
            s3_prefix=settings.metadata_backup_s3_prefix,
        )
        # An explicit export of the metadata database is sensitive. Routine
        # scheduled backups do not create action-log entries.
        audit_event_store.record_audit_event(
            connection,
            event="metadata_backup_created",
            actor=_actor(request),
            outcome="success",
            reason=action.reason,
            digest_sha256=result["digest_sha256"],
        )
    return result


@router.get(
    "/api/general/metadata-backups/download/{run_id}",
    response_class=Response,
    responses={
        200: {
            "description": "Raw metadata backup artifact",
            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
def download_metadata_backup(
    run_id: int,
    request: Request,
    admin: dict[str, Any] = Depends(current_user),
):
    with db() as connection:
        artifact = metadata_backup_service.open_backup_artifact(
            connection,
            run_id,
            backup_dir=settings.metadata_backup_dir,
        )
        audit_event_store.record_audit_event(
            connection,
            event="metadata_backup_downloaded",
            actor=_actor(request),
            outcome="success",
            run_id=run_id,
            digest_sha256=artifact["digest_sha256"],
        )
    return Response(
        content=artifact["body"],
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{artifact["filename"]}"',
            "X-Checksum-SHA256": artifact["digest_sha256"],
        },
    )
