"""Browse and overview routes: files, statistics, Problems, notifications, Recovery Kit and metadata backups."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import MISSING, BaseModel, ConfigDict, Field, RootModel

from ..api_models import ResponseModel, passthrough
from ..config import settings
from ..i18n import translate
from ..database import db
from ..errors import NotFound
from ..services.source_layout import vault_local_access
from ..services import audit_events as audit_event_store
from ..services.file_listing import FileListing
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
    normalize_directory,
    require_recent_reauth,
    vault_in_path,
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


# -- Response models (ADR-0056) -------------------------------------------------
#
# A route's answer is declared with ``passthrough(Model)``: production neither
# validates nor filters with it, the tests' HTTP client does. A field that is
# not in every answer is ``MISSING`` by default, so the document says "optional"
# without inventing a value. A model is closed where its keys are fixed here or
# in one service function; it stays open where the catalog adds keys.


class FileRecovery(ResponseModel):
    """Recovery in progress of a cloud-only file (#646): its Job and the cloud restore, which continues and is charged after the Job is stopped."""

    model_config = ConfigDict(extra="allow")
    state: Literal["queued", "awaiting_approval", "waiting_for_cloud", "ready", "downloading"]
    active: bool = Field(description="A Recovery Job is in progress; false when only the cloud restore continues or a restored copy is readable.")
    tier: Literal["Expedited", "Standard", "Bulk"] | None
    days: int | None
    requested_at: str | None
    ready_by: str | None = Field(description="Request instant plus the tier's restore hours; an estimate.")
    expires_at: str | None = Field(description="End of the readable restored copy.")


class VaultFileListItem(ResponseModel):
    """Vault File protection observation: pending, protected, problem, unconfirmed, cloud_only, stopped (an upload the Owner stopped, kept out of automatic upload until the content changes, never pending), excluded (skipped by automatic upload, never pending) or unsupported (a symlink or special file that cannot be archived, never a problem)."""

    # Open: the protection projection (``project_file``) adds its own keys.
    model_config = ConfigDict(extra="allow")
    upload_verification_requested: Literal["s3_checksum", "read_back"] | None | MISSING = MISSING
    verification_method: Literal["s3_checksum", "read_back"] | None | MISSING = MISSING
    verification_fallback_reason: str | None | MISSING = MISSING
    verification_egress_bytes: int | None | MISSING = MISSING
    last_known_state: str | MISSING = MISSING
    last_known_at: str | None | MISSING = MISSING
    previous_snapshot_protected: bool | MISSING = MISSING
    availability: str | None | MISSING = MISSING
    cleanup_eligible: bool | MISSING = MISSING
    cloud_exists: float | bool | MISSING = MISSING
    cloud_size: float | None | MISSING = MISSING
    integrity: str | None | MISSING = MISSING
    local_exists: float | bool | MISSING = MISSING
    local_file_type: str | None | MISSING = MISSING
    local_size: float | None | MISSING = MISSING
    name: str
    path: str
    recover_eligible: bool | MISSING = MISSING
    state: str
    storage_class: str | None | MISSING = MISSING
    type: Literal["file"]
    upload_eligible: bool | MISSING = MISSING
    modified_at: str | None | MISSING = MISSING
    vault_id: int | MISSING = MISSING
    vault_name: str | MISSING = MISSING
    cloud_only_conflict: bool | MISSING = MISSING
    in_excluded_folder: bool | MISSING = Field(default=MISSING, description="The file is inside an Excluded Folder: the Vault does not look there, so it keeps its last known state and is not observed.")
    id: str | MISSING = MISSING
    current_version_id: str | None | MISSING = Field(default=MISSING, description="Explicit Current Snapshot reference; null when the file has none.")
    pending_version_id: str | None | MISSING = Field(default=MISSING, description="Explicit replacement in progress; never recoverable.")
    recoverable: bool | MISSING = Field(default=MISSING, description="Current exists, is verified and available.")
    reconciliation_block: str | None | MISSING = MISSING
    cleanup: Literal["waiting", "in_progress", "blocked"] | None | MISSING = Field(default=MISSING, description="Exact cleanup of superseded versions.")
    recovery: FileRecovery | None | MISSING = MISSING


class DirectoryListItem(ResponseModel):
    """Directory protection counts as returned by /api/files browse mode. State is the most actionable observation, not Mixed."""

    # Open: the directory aggregates add their own counters.
    model_config = ConfigDict(extra="allow")
    last_known_states: dict[str, float] | MISSING = MISSING
    last_known_at: str | None | MISSING = MISSING
    available_actions: dict[str, float] | MISSING = MISSING
    cloud_size: float | MISSING = MISSING
    item_count: float
    local_size: float | MISSING = MISSING
    name: str
    path: str
    state: str
    state_counts: dict[str, float] | MISSING = MISSING
    storage_class: str | None | MISSING = MISSING
    storage_class_count: float | MISSING = MISSING
    total_size: float
    type: Literal["directory"]


class ArchiveListItem(RootModel[VaultFileListItem | DirectoryListItem]):
    # A root model cannot derive from ``ResponseModel``: it defers its build itself.
    model_config = ConfigDict(defer_build=True)


class FilesResponse(ResponseModel):
    # Open: the search answer is the catalog's own dictionary.
    model_config = ConfigDict(extra="allow")
    manual_upload: bool | MISSING = MISSING
    aggregate_status: Literal["ready", "loading", "stale"] | MISSING = Field(default=MISSING, description="Directory aggregate readiness for this listing: ready, loading (no projection yet), or stale (showing prior projection while rebuild/dirty work converges).")
    directory: str
    items: list[ArchiveListItem]
    mode: str
    page: float
    total: float


@router.get(
    "/api/vaults/{vault_id}/files",
    response_model=passthrough(FilesResponse),
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def list_files(
    q: str = "",
    state: str = "",
    directory: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=10, le=500),
    vault: dict[str, Any] = Depends(vault_in_path),
):
    """List the Vault Files of the Vault in the path."""
    directory = normalize_directory(directory, api=True)
    access = vault_local_access(vault.get("source_root") or "")
    with db() as connection:
        unconfirmed, manual_upload = _protection_observation(connection, vault, access)
        listing = FileListing(connection, protection_unconfirmed=unconfirmed).list_files_page(
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


@router.get("/api/vaults/{vault_id}/files/detail", response_model=passthrough(ArchiveListItem), responses=LOGICAL_PATH_ERROR_RESPONSES)
def file_detail(path: str, vault: dict[str, Any] = Depends(vault_in_path)):
    path = safe_relative_path(path).as_posix()
    with db() as connection:
        access = vault_local_access(vault.get("source_root") or "")
        unconfirmed, _ = _protection_observation(connection, vault, access)
        item = FileListing(connection, protection_unconfirmed=unconfirmed).get_file_item(vault["id"], path)
        if item is None:
            raise NotFound("api.file_not_found")
        return item


@router.get("/api/files/search", response_model=passthrough(FilesResponse))
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
        return FileListing(connection).search_protected_vaults(
            scoped, search=q, state=state, page=page, page_size=page_size,
        )


class UploadActivityStage(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    files: int = Field(ge=0)
    bytes: int = Field(ge=0)
    transferred_bytes: int = Field(ge=0)


class BulkUploadAdmissionActivity(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    requests: int = Field(ge=0)
    selected_files: int = Field(ge=0)
    processed_files: int = Field(ge=0)
    admitted_files: int = Field(ge=0)


class UploadPhaseSpeed(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    bytes_per_second: float | None = Field(ge=0)
    files_per_second: float | None = Field(ge=0)
    average_bytes_per_second: float | None = Field(ge=0)


class VerificationPhaseSpeed(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    bytes_per_second: float | None = Field(ge=0)
    average_bytes_per_second: float | None = Field(ge=0)


class ThroughputSample(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    at: str
    upload_bytes_per_second: float = Field(ge=0)
    verification_bytes_per_second: float = Field(ge=0)


class UploadThroughput(ResponseModel):
    """Measured speeds of a Vault's two adoption phases, from in-memory samples lost at restart (#755). Bytes per second; null until measured."""

    model_config = ConfigDict(extra="forbid")
    sample_seconds: int = Field(ge=1)
    instant_seconds: int = Field(ge=1)
    average_seconds: int = Field(ge=1)
    history_seconds: int = Field(ge=1)
    upload: UploadPhaseSpeed
    verification: VerificationPhaseSpeed
    history: list[ThroughputSample]


class ExpectedFinish(ResponseModel):
    """The later of the active phases at their average speed, and the phase that limits it (#755)."""

    model_config = ConfigDict(extra="forbid")
    at: str
    seconds: int = Field(ge=0)
    limited_by: Literal["upload", "verification"]


class UploadActivitySummary(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    stability_wait: UploadActivityStage
    queued: UploadActivityStage
    uploading: UploadActivityStage
    verifying: UploadActivityStage
    bulk_admission: BulkUploadAdmissionActivity | MISSING = MISSING
    awaiting_admission_files: int = Field(ge=0)
    next_stable_at: str | None
    throughput: UploadThroughput
    expected_finish: ExpectedFinish | None
    stopped_since: str | None = Field(description="When work that should move last made progress, once nothing has moved for five minutes; the expected finish is then null (#759).")


class UploadActivityVault(UploadActivitySummary):
    vault_id: int
    vault_name: str
    paused: bool


class ServerPriority(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    throttled: bool
    since: str | None


class UploadActivityResponse(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    observed_at: str
    global_: UploadActivitySummary = Field(alias="global")
    vaults: list[UploadActivityVault]
    server_priority: ServerPriority | MISSING = MISSING


@router.get("/api/upload-activity", response_model=passthrough(UploadActivityResponse))
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


class GeneralOverviewVault(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    name: str
    monthly_estimate: float
    open_problem_count: int
    uuid: str
    lifecycle: Literal["provisioning", "active", "reconnect_required", "stopping", "tombstone"]


class CloudPreflightSnapshot(ResponseModel):
    # Not closed: the gate answers with its whole stored row.
    status: Literal["unknown", "ready", "blocked"]
    remediation: str | None
    next_probe_at: str | None


class MetadataBackupReference(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    created_at: str | None
    verified_at: str | None


class MetadataBackupVerification(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    last_verified: MetadataBackupReference | None
    latest: MetadataBackupReference | None
    pending_run_id: int | None
    automatic: bool
    next_verification_at: str | None


class GeneralOverview(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    vaults: list[GeneralOverviewVault] | MISSING = MISSING
    open_problem_count: float | MISSING = MISSING
    monthly_estimate: float | MISSING = MISSING
    month: str | MISSING = MISSING
    currency: str | MISSING = MISSING
    pricing_effective_at: str | MISSING = MISSING
    previous_month_delta: float | None | MISSING = MISSING
    email_configured: bool | MISSING = MISSING
    runs_as_root: bool | MISSING = MISSING
    dac_override: bool | MISSING = MISSING
    cloud_preflight: CloudPreflightSnapshot | MISSING = MISSING
    last_verified_backup: str | None | MISSING = MISSING
    metadata_backup_verification: MetadataBackupVerification | MISSING = MISSING


@router.get("/api/general", response_model=passthrough(GeneralOverview))
def general_overview(request: Request, user: dict[str, Any] = Depends(current_user)):
    from ..services.essential_interface import overview
    with db() as connection:
        return overview(connection, locale=_request_locale(request))


class NotificationItem(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    vault_id: int | None
    job_id: int | None
    event: str
    title: str
    body: str
    title_key: str | None
    body_key: str | None
    message_params: dict[str, Any]
    in_app_enabled: bool
    dedupe_key: str | None
    created_at: str
    read: bool
    read_at: str | None
    problem_status: Literal["open", "resolved"] | None
    resolved_at: str | None
    resolving: Literal["scan"] | None | MISSING = MISSING


class ProblemsResponse(ResponseModel):
    """One page of the open Problems and how many are open in all."""

    model_config = ConfigDict(extra="forbid")
    items: list[NotificationItem]
    open_problem_count: int


@router.get("/api/problems", response_model=passthrough(ProblemsResponse))
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


class SourceDeviceReads(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    device: str
    reading: int
    waiting: int
    limit: int = Field(description="Reads in flight allowed on this device: 1 on a rotational disk, 3 otherwise, or SOURCE_DEVICE_READ_CONCURRENCY when set (#766).")
    bytes_per_second: int | None = Field(description="Bytes read from this device per second over the last 20 s, by scan verification and uploads (#766); null while unmeasured.")


class VaultRuntimeStatus(ResponseModel):
    # Open: the scan, the component health and the audit add their own fields.
    model_config = ConfigDict(extra="allow")
    last_error: str | None | MISSING = MISSING
    scanning: bool | MISSING = MISSING
    scan_phase: str | None | MISSING = MISSING
    verify_files_total: float | None | MISSING = MISSING
    verify_files_done: float | None | MISSING = MISSING
    verify_bytes_total: float | None | MISSING = MISSING
    verify_bytes_done: float | None | MISSING = MISSING
    traversal_files_seen: float | None | MISSING = MISSING
    traversal_files_per_minute: float | None | MISSING = MISSING
    traversal_files_expected: float | None | MISSING = MISSING
    last_error_source_cause: str | None | MISSING = MISSING
    traversal_lock_wait_seconds: float | None | MISSING = MISSING
    traversal_probe_seconds: float | None | MISSING = MISSING
    traversal_write_seconds: float | None | MISSING = MISSING
    last_error_source: str | None | MISSING = MISSING
    last_error_source_at: str | None | MISSING = MISSING
    server_priority_enabled: bool | MISSING = MISSING
    server_priority_throttled: bool | MISSING = MISSING
    server_priority_throttled_since: str | None | MISSING = MISSING
    source_device_read_limit: int | MISSING = MISSING
    source_devices: list[SourceDeviceReads] | MISSING = MISSING
    scan_queued: bool | MISSING = Field(default=MISSING, description="A requested full scan waits for the Vault (#759).")
    scan_queued_at: str | None | MISSING = MISSING
    scan_waiting_for: str | None | MISSING = Field(default=MISSING, description="The operation the queued scan waits for (null when the Vault is free).")
    scan_waiting_since: str | None | MISSING = Field(default=MISSING, description="Since when that operation holds the Vault.")
    verify_bytes_read: float | None | MISSING = Field(default=MISSING, description="Bytes read by the running verification, partial files included (#759).")


class VaultStorageBytes(ResponseModel):
    model_config = ConfigDict(extra="allow")
    cloud_bytes: float
    local_bytes: float


class StatsSourceVolume(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str | None
    health: str
    local_operations_allowed: bool


class StatsResponse(ResponseModel):
    # Open: the catalog summary is spread into the answer.
    model_config = ConfigDict(extra="allow")
    manual_upload: bool | MISSING = MISSING
    protection_state: str | None | MISSING = MISSING
    last_known_states: dict[str, float] | MISSING = MISSING
    active_jobs: float
    runtime: VaultRuntimeStatus
    states: dict[str, float]
    storage: VaultStorageBytes
    source_volume: StatsSourceVolume


@router.get("/api/vaults/{vault_id}/stats", response_model=passthrough(StatsResponse))
def stats(vault: dict[str, Any] = Depends(vault_in_path), request: Request = None):  # type: ignore[assignment]
    source_root = vault.get("source_root") or ""
    # Reconcile Source Volume identity before anything can preflight or walk the
    # configured Vault tree. Catalog summaries remain available when local work
    # is suspended, so they are intentionally collected after this gate too.
    access = vault_local_access(source_root)
    with db() as connection:
        # Cheap SQL aggregates only — never materialize every Vault File row.
        unconfirmed, manual_upload = _protection_observation(connection, vault, access)
        summary = FileListing(connection, protection_unconfirmed=unconfirmed).summary(vault["id"])
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


class NotificationsResponse(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[NotificationItem]
    unread_count: int
    open_problem_count: int
    email_configured: bool
    has_more: bool


@router.get("/api/notifications", response_model=passthrough(NotificationsResponse))
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


@router.post("/api/notifications/read", response_model=passthrough(NotificationItem))
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


class MarkAllNotificationsReadResponse(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    marked_count: int
    unread_count: int


@router.post("/api/notifications/read-all", response_model=passthrough(MarkAllNotificationsReadResponse))
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


class SystemBucket(ResponseModel):
    """The installation's System Bucket for metadata backups (#734)."""

    model_config = ConfigDict(extra="forbid")
    name: str
    region: str
    origin: Literal["configured", "created"]
    ready: bool


class MetadataRestoreNotice(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    backup_created_at: str
    updated_files: int


class RecoveryKitStatus(ResponseModel):
    model_config = ConfigDict(extra="allow")
    configured: bool
    fingerprint: str | None
    system_bucket: SystemBucket | None | MISSING = Field(default=MISSING, description="The current System Bucket (#734), ready or not; null while there is none.")
    awaiting_backup: bool | MISSING = Field(default=MISSING, description="A System Bucket exists but no metadata backup has succeeded in it yet: the kit is not offered (#734).")
    custody_acknowledged: bool
    acknowledged_at: str | None
    metadata_restore: MetadataRestoreNotice | None


@router.get("/api/recovery-kit", response_model=passthrough(RecoveryKitStatus))
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


@router.post("/api/recovery-kit/custody", response_model=passthrough(RecoveryKitStatus))
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


class MetadataBackupRun(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    created_at: str
    finished_at: str | None
    reason: str
    backend: str
    status: str
    digest_sha256: str | None
    database_sha256: str | None
    local_path: str | None
    s3_key: str | None
    s3_bucket: str | None = Field(description="Bucket of the off-host copy; null for local-only and pre-#734 runs.")
    size_bytes: int | None
    error_message: str | None
    verified_at: str | None


class MetadataBackupStatus(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    last_status: str
    last_run: MetadataBackupRun | None
    succeeded_count: int
    failed_count: int
    verification: MetadataBackupVerification = Field(description="Last verified backup and next automatic verification (#648).")
    system_bucket: SystemBucket | None = Field(description="Where off-host copies go; null while there is none (#734).")
    off_host: bool = Field(description="Whether backups must also reach the System Bucket.")


class MetadataBackupsResponse(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    status: MetadataBackupStatus
    runs: list[MetadataBackupRun]


@router.get("/api/general/metadata-backups", response_model=passthrough(MetadataBackupsResponse))
def list_metadata_backups(_: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        status = metadata_backup_service.backup_status(connection)
        runs = metadata_backup_service.list_backup_artifacts(connection)
    return {"status": status, "runs": runs}


class MetadataBackupRunResult(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    reason: str
    path: str
    digest_sha256: str
    database_sha256: str
    backend: str
    s3_key: str | None
    s3_bucket: str | None
    size_bytes: int
    filename: str
    created_at: str


@router.post("/api/general/metadata-backups/run", response_model=passthrough(MetadataBackupRunResult))
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
