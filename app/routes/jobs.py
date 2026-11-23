"""Job and operation routes: Job lists, scan, catalog events, upload, recover, free space and cloud deletion."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..api_models import JsonObjectResponse, response_model
from ..config import settings
from ..i18n import DEFAULT_LOCALE, format_datetime, translate
from ..catalog import ArchiveCatalog
from ..database import db
from ..errors import Conflict, Invalid, NotFound, Unavailable, VaultNotFound
from ..services.source_layout import vault_local_access
from ..services.runtime_status import scan_waiting, vault_operation_key
from ..services import audit_events as audit_event_store
from ..services.catalog_event_stream import coalesced_catchup_signal, iter_catalog_event_sse
from ..services.restore_estimates import SUPPORTED_RESTORE_TIERS
from ..services import cloud_deletion as cloud_deletion_service
from ..services import job_admission
from ..services import job_queue
from ..services.runtime_status import runtime_status
from ..services.vault_paths import safe_relative_path
from ..storage import now_iso, queue_scan
from .deps import (
    LOGICAL_PATH_ERROR_RESPONSES,
    SCOPED_NOT_FOUND_RESPONSES,
    _actor,
    _api_message,
    _request_locale,
    current_user,
    normalize_directory,
    require_recent_reauth,
    vault_in_path,
)


router = APIRouter()


class FileAction(BaseModel):
    path: str
    is_directory: bool = False
    archive_version_id: str | None = None
    restore_tier: str | None = None
    restore_days: int | None = Field(default=None, ge=1, le=30)


class UploadAction(FileAction):
    request_id: uuid.UUID | None = None


class UploadReconciliationAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    is_directory: bool = False
    remediation_id: str | None = Field(default=None,min_length=1,max_length=128)


class CloudOnlyReplacementAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str


class RecoverySelectionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=1024)
    is_directory: bool = False


class RecoverRequest(FileAction):
    model_config = ConfigDict(extra="forbid")
    path: str = ""
    selection: list[RecoverySelectionItem] | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unambiguous_scope(self):
        if self.selection is not None and (self.path or self.is_directory or self.archive_version_id):
            raise ValueError("Use either selection or a single path/version")
        return self


class RecoverEstimateRequest(RecoverRequest):
    pass


class RecoverApproveAction(BaseModel):
    group_id: str = Field(min_length=1, max_length=64)


class GroupCancelAction(BaseModel):
    group_id: str = Field(min_length=1, max_length=64)


class JobCancelAction(GroupCancelAction):
    action: str = Field(min_length=1, max_length=20)


class CloudDeletionListing(BaseModel):
    """A file-list view, resolved by the server like ``/api/files`` (#723)."""

    model_config = ConfigDict(extra="forbid")

    directory: str = Field(default="", max_length=1024)
    q: str = Field(default="", max_length=1024)
    state: str = Field(default="", max_length=32)


class CloudDeletionPreviewRequest(BaseModel):
    """Exact files (``path``/``paths``), recursive folders (``is_directory`` or
    ``directories``) or a whole list view (``listing``), mixed freely.

    One exact file is all-or-nothing; any other selection covers only its
    eligible files (#723). ``duplicates_only`` keeps only Cloud Duplicates.
    """

    model_config = ConfigDict(extra="forbid")

    path: str | None = Field(default=None, min_length=1, max_length=1024)
    is_directory: bool = False
    paths: list[str] | None = None
    directories: list[str] | None = None
    listing: CloudDeletionListing | None = None
    duplicates_only: bool = False

    @model_validator(mode="after")
    def validate_selection(self) -> "CloudDeletionPreviewRequest":
        if not (self.path or self.paths or self.directories or self.listing):
            raise ValueError("Select at least one file or folder")
        return self


class CloudPurgeRequest(CloudDeletionPreviewRequest):
    confirmation: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)
    # The preview's fingerprint; required for any selection but one exact file.
    fingerprint: str | None = Field(default=None, max_length=128)


@router.get("/api/vaults/{vault_id}/jobs", response_model=response_model("JobsResponse"), responses=LOGICAL_PATH_ERROR_RESPONSES)
def jobs(
    request: Request,
    vault: dict[str, Any] = Depends(vault_in_path),
    *,
    action: Annotated[list[str] | None, Query()] = None,
    active_only: bool = False,
    path: str | None = None,
    groups_before_id: Annotated[int | None, Query(ge=1)] = None,
):
    """At most 50 recent Jobs and 50 complete group summaries, never all members.

    Action, active and exact-file filters select groups before the bounded cut;
    their summaries still account for every member in this Vault. The cursor
    pages summaries only; individual members use the group-detail endpoint.
    """
    from ..services.job_queries import list_jobs
    logical_path = safe_relative_path(path).as_posix() if path is not None else None
    with db() as connection:
        return list_jobs(connection, vault_id=vault["id"], locale=_request_locale(request),
                         actions=action, active_only=active_only, path=logical_path,
                         groups_before_id=groups_before_id)


@router.get("/api/vaults/{vault_id}/job-groups/{group_id}/jobs", response_model=response_model("FileJobHistoryResponse"))
def job_group_detail(
    request: Request, group_id: str,
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=50),
    vault: dict[str, Any] = Depends(vault_in_path),
):
    """Newest-first keyset page of one group's Jobs, scoped to this Vault."""
    from ..services.job_queries import group_jobs
    with db() as connection:
        return group_jobs(connection, vault_id=vault["id"], group_id=group_id,
                          locale=_request_locale(request), before_id=before_id, limit=limit)


@router.get("/api/vaults/{vault_id}/jobs/{job_id}", response_model=response_model("JobRecord"))
def job_detail(request: Request, job_id: int, vault: dict[str, Any] = Depends(vault_in_path)):
    from ..services.job_queries import job_record
    with db() as connection:
        record = job_record(connection, vault_id=vault["id"], job_id=job_id,
                            locale=_request_locale(request))
        if record is None:
            raise NotFound("api.job_not_found")
    return record


@router.get("/api/vaults/{vault_id}/files/jobs", response_model=response_model("FileJobHistoryResponse"), responses=LOGICAL_PATH_ERROR_RESPONSES)
def file_job_history(request: Request, path: str = Query(), before_id: int | None = Query(default=None,ge=1),
                     limit: int = Query(default=50,ge=1,le=50), vault: dict[str, Any] = Depends(vault_in_path)):
    from ..services.job_queries import file_jobs
    logical_path = safe_relative_path(path).as_posix()
    with db() as connection:
        history = file_jobs(connection, vault_id=vault["id"], path=logical_path,
                            locale=_request_locale(request), before_id=before_id, limit=limit)
        if history is None:
            raise NotFound("api.file_not_found")
    return history


@router.post("/api/vaults/{vault_id}/scan", status_code=202, response_model=response_model("ScanResponse"))
async def start_scan(
    request: Request,
    vault: dict[str, Any] = Depends(vault_in_path),
):
    status = runtime_status.get(vault["id"], {})
    if status.get("scanning"):
        return {**_api_message(request, "api.scan_in_progress"), "queued": False}
    # A Vault held by another operation queues the scan instead of dropping it
    # (#759): it starts as soon as the Vault is free and the runtime status
    # says what it waits for.
    queue_scan(vault)
    # Read after queueing: the request is waiting only while something holds
    # the Vault; once taken by a scan, or with the Vault free, it starts.
    waiting = scan_waiting(int(vault["id"]))
    if not waiting["scan_queued"] or waiting["scan_waiting_for"] is None:
        return {**_api_message(request, "api.scan_started", name=vault["name"]), "queued": False}
    locale = _request_locale(request)
    return {**_api_message(
        request, "api.scan_queued", name=vault["name"],
        operation=translate(vault_operation_key(waiting["scan_waiting_for"]), locale=locale),
        since=format_datetime(waiting["scan_waiting_since"], locale),
    ), "queued": True, "waiting_for": waiting["scan_waiting_for"],
        "waiting_since": waiting["scan_waiting_since"]}


@router.get("/api/vaults/{vault_id}/catalog/revision", response_model=response_model("CatalogRevisionResponse"))
def catalog_revision_snapshot(
    after_revision: int = Query(0, ge=0),
    vault: dict[str, Any] = Depends(vault_in_path),
):
    """One-shot catch-up for focus/online recovery without holding a stream."""
    signal = coalesced_catchup_signal(
        vault_id=int(vault["id"]),
        after_revision=after_revision,
    )
    return {
        "vault_id": int(signal["vault_id"]),
        "revision": int(signal["revision"]),
        "domains": list(signal.get("domains") or []),
        "has_gap": bool(signal.get("has_gap")),
        "changed": bool(
            signal.get("has_gap")
            or int(signal["revision"]) > after_revision
        ),
    }


@router.get(
    "/api/vaults/{vault_id}/catalog/events",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": (
                "Server-Sent Events stream of Vault-scoped catalog "
                "invalidation signals (text/event-stream)"
            ),
            "content": {
                "text/event-stream": {
                    "schema": {"type": "string"},
                }
            },
        }
    },
)
async def catalog_events_stream(
    request: Request,
    after_revision: int = Query(0, ge=0),
    subscribe: bool = Query(True),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    vault: dict[str, Any] = Depends(vault_in_path),
    user: dict[str, Any] = Depends(current_user),
):
    """Authenticated SSE stream of coalesced catalog invalidation signals.

    The payload carries only Vault identity, monotonic revision, affected
    domains, and an optional retention gap flag — never filesystem paths.
    Pass ``subscribe=false`` for a finite catch-up response used by tests and
    bounded recovery probes. Open streams observe the durable journal so
    multi-process writers are visible without client idle polling.

    Documented like ``/metrics`` as a non-JSON success body (SSE text stream);
    it is excluded from the JsonObjectResponse OpenAPI contract suite.
    """
    vault_id = int(vault["id"])
    session = request.state.session
    session_id = str(session["id"])
    resume_after = after_revision
    if last_event_id:
        try:
            resume_after = max(resume_after, int(last_event_id))
        except (TypeError, ValueError):
            raise Invalid("api.last_event_id_invalid") from None

    async def event_generator():
        async for chunk in iter_catalog_event_sse(
            vault_id=vault_id,
            session_id=session_id,
            resume_after=resume_after,
            subscribe=subscribe,
            is_disconnected=request.is_disconnected,
            # The path names the Vault: the Session's selection is not consulted.
            explicit_vault=True,
        ):
            yield chunk

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/api/vaults/{vault_id}/files/versions",
    response_model=response_model("FileVersionsResponse"),
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def file_versions(
    path: str = Query(),
    vault: dict[str, Any] = Depends(vault_in_path),
):
    logical_path = safe_relative_path(path).as_posix()
    with db() as connection:
        versions = ArchiveCatalog(connection).list_versions(vault["id"], logical_path)
        from ..services.upload_provenance import describe
        reconciliation = describe(connection, vault_id=vault["id"], path=logical_path)
        from ..services.upload_remediation import file_projection
        remediations = file_projection(connection,vault["id"],logical_path)
    # The response is an internal exact-version pin for recovery, not history.
    recoverable = [row for row in versions if row["recoverable"]][:1]
    default_version_id = recoverable[0]["id"] if recoverable else None
    return {
        "path": logical_path,
        "items": recoverable,
        "recoverable_count": len(recoverable),
        "default_archive_version_id": default_version_id,
        "reconciliation": reconciliation,
        "remediations": remediations,
        "supported_restore_tiers": list(SUPPORTED_RESTORE_TIERS),
        "default_restore_tier": settings.restore_tier,
        "default_restore_days": settings.restore_days,
    }


@router.post(
    "/api/vaults/{vault_id}/recover/estimate",
    response_model=response_model("RecoverEstimateResponse"),
    responses={**LOGICAL_PATH_ERROR_RESPONSES, **SCOPED_NOT_FOUND_RESPONSES},
)
def recover_estimate(
    action: RecoverEstimateRequest,
    vault: dict[str, Any] = Depends(vault_in_path),
):
    return job_admission.estimate_recovery(
        vault["id"],
        path=action.path,
        is_directory=action.is_directory,
        selection=[(item.path, item.is_directory) for item in action.selection] if action.selection else None,
        archive_version_id=action.archive_version_id,
        restore_tier=action.restore_tier,
        restore_days=action.restore_days,
    )


@router.post(
    "/api/vaults/{vault_id}/upload",
    status_code=202,
    response_model=JsonObjectResponse,
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def upload(
    action: UploadAction,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(vault_in_path),
):
    if action.is_directory:
        from ..services import bulk_upload_admission
        path = normalize_directory(action.path, api=True)
        with db() as connection:
            admission = bulk_upload_admission.create(
                connection, vault_id=vault["id"], path=path, user_id=user["id"],
                request_id=str(action.request_id) if action.request_id else None)
        return {"group_id": admission["group_id"], "job_ids": [], "item_count": admission["admitted_count"],
                "total_bytes": admission["total_bytes"], "admission": admission,
                **_api_message(request, "api.upload_admission_started")}
    queued = job_admission.queue_jobs(action.path, "upload", vault["id"], False)
    return {**queued, **_api_message(request, "api.upload_started")}


@router.get("/api/vaults/{vault_id}/upload/admissions/{group_id}", response_model=response_model("BulkUploadAdmissionResponse"))
def upload_admission_status(group_id: uuid.UUID, _: dict[str, Any] = Depends(current_user),
                            vault: dict[str, Any] = Depends(vault_in_path)):
    from ..services import bulk_upload_admission
    with db() as connection:
        result = bulk_upload_admission.get(connection, vault_id=vault["id"], group_id=str(group_id))
    if result is None:
        raise NotFound("api.upload_admission.not_found")
    return result


@router.post("/api/vaults/{vault_id}/upload/reconcile", status_code=202, response_model=response_model("UploadReconciliationResponse"), responses=LOGICAL_PATH_ERROR_RESPONSES)
def reconcile_upload(action: UploadReconciliationAction, user: dict[str, Any] = Depends(current_user), vault: dict[str, Any] = Depends(vault_in_path)):
    """Exact resume only, including protected files for which upload returns 409.

    Common target arbiter; no arbitrary deletion or cancellation resurrection. It
    never PUTs by itself: the Job may send only when S3 positively proves the
    earlier send never landed (ADR-0025), and never on a path with paused protection.
    """
    from ..services.upload_remediation import schedule_resume
    logical_path = safe_relative_path(action.path).as_posix()
    if action.is_directory:
        raise Invalid("api.reconcile.single_file")
    if not vault_local_access(vault["source_root"]).local_operations_allowed:
        raise Unavailable("api.reconcile.volume_unavailable")
    with db() as connection:
        return schedule_resume(connection, vault=vault, path=logical_path, user_id=user["id"], timestamp=datetime.now(timezone.utc).isoformat(), remediation_id=action.remediation_id)


def _retry_failed_uploads(vault_id: int | None) -> dict[str, Any]:
    from ..storage import queue_vault_auto_uploads, _ScanRootMismatch
    with db() as connection:
        vaults = connection.execute(
            "SELECT * FROM vaults" + (" WHERE id=%s" if vault_id is not None else "") + " ORDER BY id",
            (vault_id,) if vault_id is not None else (),
        ).fetchall()
    if vault_id is not None and not vaults:
        raise VaultNotFound()
    count = 0
    for vault in vaults:
        try:
            count += queue_vault_auto_uploads(dict(vault), retry_failed_only=True)
        except (_ScanRootMismatch, OSError):
            # Missing/replaced roots remain ineligible, not implicitly reconnected.
            continue
    return {"item_count": count}


@router.post("/api/upload/retry-failed", status_code=202, response_model=response_model("RetryFailedUploadsResponse"))
def retry_failed_uploads(_: dict[str, Any] = Depends(current_user)):
    """Every Vault of the installation, never the Session's choice."""
    return _retry_failed_uploads(None)


@router.post("/api/vaults/{vault_id}/upload/retry-failed", status_code=202,
             response_model=response_model("RetryFailedUploadsResponse"))
def retry_failed_vault_uploads(vault_id: int = Path(ge=1), _: dict[str, Any] = Depends(current_user)):
    """One Vault's failed Uploads; any existing Vault, operational or not."""
    return _retry_failed_uploads(vault_id)


@router.post(
    "/api/vaults/{vault_id}/files/cloud-only-conflict/replace",
    status_code=202,
    response_model=JsonObjectResponse,
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def replace_cloud_only_copy(
    action: CloudOnlyReplacementAction,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(vault_in_path),
):
    """Confirm that new content replaces the only cloud copy at its path."""
    queued = job_admission.confirm_cloud_only_replacement(
        action.path, vault["id"], _actor(request))
    return {**queued, **_api_message(request, "api.upload_started")}


_JOB_STOPPED_MESSAGE_KEYS = {
    "upload": "job.upload_stopped",
    "recover": "job.recovery_stopped",
    "free-space": "job.cleanup_stopped",
    "rename": "job.rename_stopped",
    "cloud-purge": "job.cloud_purge_stopped",
    "storage-class": "job.storage_class_stopped",
}


def cancel_job_group(
    group_id: str,
    job_action: str,
    vault: dict[str, Any],
    actor: str,
):
    if job_action not in _JOB_STOPPED_MESSAGE_KEYS:
        raise Invalid("api.invalid_operation")
    message_key = _JOB_STOPPED_MESSAGE_KEYS[job_action]
    message = translate(message_key, locale=DEFAULT_LOCALE)
    with db() as connection:
        job_admission.lock_vault_decisions(connection, vault["id"])
        if job_action == "cloud-purge":
            cancelled = cloud_deletion_service.cancel_cloud_deletion(
                connection,
                vault_id=vault["id"],
                group_id=group_id,
                actor=actor,
                cancelled_at=now_iso(),
            )
            job_queue.signal_job_cancellation(
                [
                    int(row["id"])
                    for row in connection.execute(
                        """
                        SELECT id FROM jobs
                        WHERE vault_id=%s AND group_id=%s AND action=%s
                        """,
                        (vault["id"], group_id, job_action),
                    ).fetchall()
                ]
            )
            return {
                "message": message,
                "cancelled_count": cancelled.cancelled_count,
            }
        rows = connection.execute(
            f"""
            SELECT id FROM jobs
            WHERE vault_id=%s AND group_id=%s AND action=%s
              AND {job_queue.OPEN_JOB_SQL}
            """,
            (vault["id"], group_id, job_action),
        ).fetchall()
        job_ids = [int(row["id"]) for row in rows]
        stopped_admissions = 0
        if job_action == "upload":
            from ..services import bulk_upload_admission
            stopped_admissions = bulk_upload_admission.cancel(connection, vault_id=vault["id"], group_id=group_id)
        if not job_ids and not stopped_admissions:
            raise Conflict("api.operation_not_running")
        if job_ids:
            _cancel_active_jobs(connection, vault, job_action, job_ids, actor)
    return {
        "message": message,
        "cancelled_count": len(job_ids),
        "stopped_admissions": stopped_admissions,
    }


def _cancel_active_jobs(
    connection: Any,
    vault: dict[str, Any],
    job_action: str,
    job_ids: list[int],
    actor: str,
) -> None:
    """Cancel already-selected non-terminal Jobs; the caller holds the Vault lock."""
    message_key = _JOB_STOPPED_MESSAGE_KEYS[job_action]
    message = translate(message_key, locale=DEFAULT_LOCALE)
    automatic_cleanup_rows = []
    if job_action == "free-space":
        automatic_cleanup_rows = connection.execute(
            f"""
            SELECT j.id, f.path, j.archive_version_id
            FROM jobs j JOIN files f ON f.id = j.file_id
            WHERE j.vault_id=%s AND j.id = ANY(%s) AND j.action='free-space'
              AND j.origin='automatic'
              AND {job_queue.open_job_sql("j")}
            """,
            (vault["id"], job_ids),
        ).fetchall()
    # The threads of this process stop at their next checkpoint; the rows end
    # for every process.
    job_queue.signal_job_cancellation(job_ids)
    job_queue.JobQueue(connection).cancel_open_jobs(
        vault_id=vault["id"], job_ids=job_ids, message=message,
        message_key=message_key, now=now_iso(),
    )
    if job_action == "upload":
        from ..services import upload_remediation
        for job_id in job_ids:
            upload_remediation.transition(connection, job_id, "cancelled", now_iso())
    # Do not clear archive_versions.restore_state on recover cancel:
    # RestoreObject cannot be cancelled after AWS accepts it, and a later
    # recover must be able to resume polling (REQ-030 / BUG-018).
    for row in automatic_cleanup_rows:
        audit_event_store.record_audit_event(
            connection,
            event="local_cleanup.cancelled",
            actor=actor,
            vault_id=vault["id"],
            job_id=int(row["id"]),
            outcome="cancelled",
            path=row["path"],
            archive_version_id=row["archive_version_id"],
        )


class PendingUploadCancelAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(default="", max_length=1024)


@router.post(
    "/api/vaults/{vault_id}/upload/cancel-pending",
    response_model=response_model("JobCancelResponse"),
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def cancel_pending_uploads(
    action: PendingUploadCancelAction,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(vault_in_path),
):
    """Cancel the uploads of the Vault (or one folder) that have not begun.

    One call replaces thousands of per-file cancels after a burst or a moved
    folder (issue #493). An empty ``path`` means the whole Vault; a folder
    matches itself and its descendants only (``foo`` never matches ``foobar``).
    Only uploads that never started are cancelled: running or verifying ones,
    reconcile-only attempts, jobs with a started or recorded send and jobs of a
    remediation episode are left to their own cancel and Resume, so an ambiguous
    send is never closed for good by a bulk action. A cancelled upload, manual or
    automatic, is not re-queued automatically until the file's content changes,
    like a single cancel (#618).
    """
    folder = normalize_directory(action.path, api=True)
    from ..services import bulk_upload_admission
    from ..services.job_queries import unstarted_upload_job_ids
    with db() as connection:
        job_admission.lock_vault_decisions(connection, vault["id"])
        stopped_admissions = bulk_upload_admission.cancel(connection, vault_id=vault["id"], path=folder)
        job_ids = unstarted_upload_job_ids(connection, vault_id=vault["id"], folder=folder)
        if job_ids:
            _cancel_active_jobs(connection, vault, "upload", job_ids, _actor(request))
    return {
        "message": translate("job.upload_stopped", locale=DEFAULT_LOCALE),
        "cancelled_count": len(job_ids),
        "stopped_admissions": stopped_admissions,
    }


@router.post("/api/vaults/{vault_id}/jobs/cancel", response_model=response_model("JobCancelResponse"))
def cancel_job(
    action: JobCancelAction,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(vault_in_path),
):
    return cancel_job_group(action.group_id, action.action, vault, _actor(request))


@router.post(
    "/api/vaults/{vault_id}/recover",
    status_code=202,
    response_model=JsonObjectResponse,
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def recover(
    action: RecoverRequest,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(vault_in_path),
):
    queued = job_admission.queue_jobs(
        action.path,
        "recover",
        vault["id"],
        action.is_directory,
        archive_version_id=action.archive_version_id,
        restore_tier=action.restore_tier,
        restore_days=action.restore_days,
        recovery_selection=[(item.path, item.is_directory) for item in action.selection] if action.selection else None,
    )
    return {**queued, **_api_message(request, "api.recovery_started")}


@router.post("/api/vaults/{vault_id}/recover/approve", status_code=202, response_model=JsonObjectResponse)
def approve_recover(
    action: RecoverApproveAction,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(vault_in_path),
):
    from ..services import recovery_approval

    with db() as connection:
        job_admission.lock_vault_decisions(connection, vault["id"])
        # Read the clock AFTER the lock: waiting for another decision must never
        # allow a late approval. Keep subsecond precision at the deadline.
        timestamp = datetime.now(timezone.utc).isoformat()
        job_ids = recovery_approval.approve(
            connection, vault_id=vault["id"], group_id=action.group_id, now=timestamp,
        )
    # Commit expiration even when the approval is refused.
    if not job_ids:
        raise Conflict("api.recover_approval.none_waiting")
    return {
        "group_id": action.group_id,
        "job_ids": job_ids,
        "message": "High-impact restore approved",
    }


@router.post(
    "/api/vaults/{vault_id}/free-space",
    status_code=202,
    response_model=JsonObjectResponse,
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def free_space(
    action: FileAction,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(vault_in_path),
):
    queued = job_admission.queue_jobs(
        action.path, "free-space", vault["id"], action.is_directory
    )
    return {**queued, **_api_message(request, "api.free_space_started")}


def _cloud_deletion_selection(action: CloudDeletionPreviewRequest) -> dict[str, Any]:
    if action.paths:
        paths = list(action.paths)
    else:
        paths = [action.path] if action.path else []
    listing = None
    if action.listing is not None:
        listing = cloud_deletion_service.ListingFilter(
            directory=normalize_directory(action.listing.directory, api=True),
            q=action.listing.q, state=action.listing.state)
    return {"paths": paths, "is_directory": action.is_directory,
            "directories": list(action.directories or []), "listing": listing,
            "duplicates_only": action.duplicates_only}


@router.get("/api/cloud-deletion", response_model=response_model("CloudDeletionSettings"))
def cloud_deletion_settings(user: dict[str, Any] = Depends(current_user)):
    return {"purge_delay_seconds": cloud_deletion_service.DELETION_DELAY_SECONDS}


@router.post("/api/vaults/{vault_id}/cloud-deletion/preview", response_model=response_model("CloudDeletionPreview"))
def preview_cloud_deletion(
    action: CloudDeletionPreviewRequest,
    vault: dict[str, Any] = Depends(vault_in_path),
):
    with db() as connection:
        preview = cloud_deletion_service.preview_selection(
            connection, vault_id=vault["id"], **_cloud_deletion_selection(action))
    return {
        **preview.as_dict(),
    }


@router.post("/api/vaults/{vault_id}/cloud-purge", status_code=202, response_model=JsonObjectResponse)
def cloud_purge(
    action: CloudPurgeRequest,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(vault_in_path),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    with db() as connection:
        scheduled = cloud_deletion_service.schedule_cloud_purge(
            connection,
            vault_id=vault["id"],
            **_cloud_deletion_selection(action),
            actor=_actor(request),
            requested_at=now_iso(),
            confirmation=action.confirmation,
            reason=action.reason,
            fingerprint=action.fingerprint,
        )
    return {
        "group_id": scheduled.group_id,
        "job_ids": scheduled.job_ids,
        "preview": scheduled.preview.as_dict(),
        "pending_until": scheduled.pending_until,
        "status": "pending_delay",
        **_api_message(request, "api.cloud_purge_scheduled"),
    }
