"""Vault management routes: creation, bucket, protection stop, deletion, cleanups, usage and reconnect."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from pydantic import MISSING, BaseModel, ConfigDict, Field

from ..api_models import ResponseModel, passthrough
from ..database import db
from ..errors import Conflict, NotFound, VaultNotFound
from ..services.source_layout import source_volume_inventory
from ..services import source_directories as source_directories_service
from ..services import vault_relocation as vault_relocation_service
from ..services import protection_stop as protection_stop_service
from ..services import vault_deletion as vault_deletion_service
from ..services import duplicate_cleanup as duplicate_cleanup_service
from ..services import mirror_cleanup as mirror_cleanup_service
from ..services import minimum_storage as minimum_storage_service
from ..services import bucket_metrics as bucket_metrics_service
from ..services import billed_cost as billed_cost_service
from ..services import vault_buckets as vault_buckets_service
from ..services.bucket_provisioning import RetentionLock
from ..services.vaults import create_vault, list_vault_inventory
from ..services.runtime_status import runtime_status, scan_lock_for_vault, status_lock
from ..storage import now_iso, scan_vault, s3_client
from .deps import (
    _OPERATIONAL_VAULT,
    _actor,
    _request_locale,
    current_user,
    require_recent_reauth,
)


router = APIRouter()

# Registered after the Job routes: two routers keep the historical registration
# order, and with it the order of the OpenAPI paths.
inventory_router = APIRouter()


class VaultSelfServiceCreate(BaseModel):
    """Self-service vault creation payload (issues #7, #6, and #150).

    Labels are accepted. Adoption adds a
    constrained ``volume_alias`` + ``relative_path`` pair — never an absolute
    filesystem path or S3 identity.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    creation_mode: str = Field(default="empty", pattern="^(empty|adopt)$")
    volume_alias: str | None = Field(default=None, min_length=1, max_length=120)
    relative_path: str | None = Field(default=None, max_length=1024)
    # The new Vault Bucket's Governance Retention Lock in days, or none (#736).
    # Chosen only here: it cannot be changed from FrostVault afterwards.
    retention_lock_days: Literal[30, 90, 180, 365] | None = None


class VaultReconnect(BaseModel):
    """Explicitly accept a Source Volume and directory for an existing Vault."""

    model_config = ConfigDict(extra="forbid")

    volume_alias: str = Field(min_length=1, max_length=120)
    relative_path: str = Field(default="", max_length=1024)
    reason: str = Field(min_length=3, max_length=500)


class StopProtectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cloud_disposition: str = Field(pattern="^(keep|delete)$")
    confirmation: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=500)


class VaultDeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=500)
    # «Also delete the bucket», only for a dedicated Vault Bucket (#739).
    delete_bucket: bool = False


class DuplicateCleanupUpdate(BaseModel):
    """Automatic Duplicate Cleanup settings of one Vault (#726); omitted fields keep their value."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    grace_days: int | None = Field(default=None, ge=1, le=90)
    cap_files: int | None = Field(default=None, ge=1, le=1_000_000)
    cap_bytes: int | None = Field(default=None, ge=1, le=1024 ** 5)
    # Schedule only duplicates beyond their Minimum Storage Duration (#731).
    beyond_minimum: bool | None = None


class MirrorCleanupUpdate(BaseModel):
    """Mirror Cleanup settings of one Vault (#771); omitted fields keep their value."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    grace_days: int | None = Field(default=None, ge=1, le=180)
    cap_files: int | None = Field(default=None, ge=1, le=1_000_000)
    cap_bytes: int | None = Field(default=None, ge=1, le=1024 ** 5)
    # Folders relative to the Vault root; an empty list is the whole Vault.
    folders: list[str] | None = Field(default=None, max_length=100)
    beyond_minimum: bool | None = None


# -- Response models (ADR-0056) -------------------------------------------------
#
# A route's answer is declared with ``passthrough(Model)``: production neither
# validates nor filters with it, the tests' HTTP client does. Every model here is
# closed: the route or one service function builds its keys. A field that is not
# in every answer is ``MISSING`` by default, so the document says "optional"
# without inventing a value.

VaultLifecycle = Literal["provisioning", "active", "reconnect_required", "stopping", "tombstone"]


class VaultPauseResponse(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    paused: bool
    message: str
    message_key: str


@router.post("/api/vaults/{vault_id}/pause", response_model=passthrough(VaultPauseResponse))
def pause_vault(vault_id: int, request: Request, user: dict[str, Any] = Depends(current_user)):
    return _set_vault_pause(vault_id, True, request, user)


@router.post("/api/vaults/{vault_id}/resume", response_model=passthrough(VaultPauseResponse))
def resume_vault(vault_id: int, request: Request, user: dict[str, Any] = Depends(current_user)):
    return _set_vault_pause(vault_id, False, request, user)


def _set_vault_pause(vault_id: int, paused: bool, request: Request, user: dict[str, Any]):
    from ..services import vault_pause
    with db() as connection:
        return vault_pause.set_paused(connection, vault_id=vault_id, paused=paused,
                                       locale=_request_locale(request))


class VaultListItem(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    uuid: str
    name: str
    paused: bool
    lifecycle: VaultLifecycle


class VaultsResponse(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[VaultListItem]


@router.get("/api/vaults", response_model=passthrough(VaultsResponse))
def list_vaults(_: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        rows = connection.execute(
            f"""
            SELECT v.id, v.uuid::text AS uuid, v.name, v.paused, v.lifecycle
            FROM vaults v
            WHERE {_OPERATIONAL_VAULT}
            ORDER BY v.name
            """
        ).fetchall()
    return {"items": rows}


class VaultBucketStatus(ResponseModel):
    """A Vault's bucket (#735). While a new Vault is provisioning, the state of its Vault Bucket creation: creating, failed, or settling (verified, active at active_at, 15 minutes after the verification)."""

    model_config = ConfigDict(extra="forbid")
    vault_id: int
    bucket: str
    region: str
    dedicated: bool
    state: Literal["creating", "failed", "settling", "ready"]
    stage: Literal["reserved", "created", "configured", "verified"] | None
    attempts: int
    last_attempt_at: str | None
    error: str | None
    retention_lock_days: int | None
    active_at: str | None = Field(description="When a verified bucket's Vault becomes active (settling only).")
    active_in_seconds: int | None = Field(description="Seconds until active_at, never negative (settling only).")


type VaultCreationMode = Literal["empty", "adopt"]


class VaultCreateResponse(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    uuid: str
    name: str
    creation_mode: VaultCreationMode
    lifecycle: Literal["provisioning", "active"]
    bucket: VaultBucketStatus


@router.post("/api/vaults", status_code=201, response_model=passthrough(VaultCreateResponse))
def create_owner_vault(
    action: VaultSelfServiceCreate,
    background_tasks: BackgroundTasks,
    request: Request,
    _: dict[str, Any] = Depends(current_user),
):
    """Create a Vault from the UI, the only way Vaults are created (#412).

    The server generates the plaintext storage identity and creates the
    Vault's own Vault Bucket (#735): the Vault is active only once the bucket
    is created and verified. ``bucket`` reports that; when it failed the Vault
    stays ``provisioning``, FrostVault retries in the background and the Owner
    may retry at once. Adoption binds any free directory of a custom Source
    Volume in place and, once active, starts an asynchronous local scan.
    ``retention_lock_days`` gives the bucket a Governance Retention Lock
    (#736, ADR-0050), fixed for the Vault's life.
    """
    vault = create_vault(
        action.name,
        creation_mode=action.creation_mode,
        volume_alias=action.volume_alias,
        relative_path=action.relative_path,
        retention_lock=(RetentionLock(days=action.retention_lock_days)
                        if action.retention_lock_days else None),
    )
    if action.creation_mode == "adopt" and vault["lifecycle"] == "active":
        # scan_vault reloads the authoritative row.
        background_tasks.add_task(scan_vault, {"id": vault["id"]})
    payload: dict[str, Any] = {
        "id": vault["id"],
        "uuid": str(vault["uuid"]),
        "name": vault["name"],
        "creation_mode": action.creation_mode,
        "lifecycle": vault["lifecycle"],
        "bucket": vault["bucket"],
    }
    return payload


@router.get("/api/vaults/{vault_id}/bucket", response_model=passthrough(VaultBucketStatus))
def vault_bucket_status(vault_id: int, _: dict[str, Any] = Depends(current_user)):
    """The Vault's bucket and, while it is being created, the creation state (#735)."""
    with db() as connection:
        state = vault_buckets_service.status(connection, vault_id)
    if state is None:
        raise VaultNotFound()
    return state


class RetainedVersions(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    count: int
    bytes: int
    deletable_from: str | None
    deletable_until: str | None
    failed_count: int = Field(description="Retained Versions whose last deletion failed (#749).")
    failed_attempts: int = Field(description="The most consecutive failed deletions of one of them.")
    last_error: str | None = Field(description="The S3 error code of the most recent failure.")
    last_attempt_at: str | None
    next_attempt_at: str | None


class RetentionLockStatus(ResponseModel):
    """A Vault's Governance Retention Lock, fixed at creation (#736), and the superseded versions it holds back until their RetainUntilDate."""

    model_config = ConfigDict(extra="forbid")
    vault_id: int
    days: int | None
    mode: Literal["GOVERNANCE"] | None
    retained: RetainedVersions | None


@router.get("/api/vaults/{vault_id}/retention-lock", response_model=passthrough(RetentionLockStatus))
def vault_retention_lock(vault_id: int, _: dict[str, Any] = Depends(current_user)):
    """The Vault's Retention Lock and the superseded versions it holds back (#736).

    ``days`` is null for a Vault without one. ``retained`` counts the versions
    waiting for their RetainUntilDate before FrostVault deletes them.
    """
    from ..services import retention_lock as retention_lock_service
    with db() as connection:
        if connection.execute("SELECT 1 FROM vaults WHERE id=%s", (vault_id,)).fetchone() is None:
            raise VaultNotFound()
        state = retention_lock_service.status(connection, vault_id)
    return {"vault_id": vault_id, "days": None, "mode": None, "retained": None, **(state or {})}


@router.post("/api/vaults/{vault_id}/bucket/retry", response_model=passthrough(VaultBucketStatus))
def retry_vault_bucket(vault_id: int, background_tasks: BackgroundTasks,
                       _: dict[str, Any] = Depends(current_user)):
    """Retry the creation of a provisioning Vault's bucket now (#735).

    Idempotent: the same saved name, configuration applied again, then
    verified. A Vault whose bucket is ready is returned unchanged.
    """
    with db() as connection:
        before = vault_buckets_service.status(connection, vault_id)
    if before is None:
        raise VaultNotFound()
    state = vault_buckets_service.provision(vault_id)
    if before["state"] != "ready" and state["state"] == "ready":
        # A Vault that became active now gets its first scan (an adopted
        # folder's files); scan_vault reloads the authoritative row.
        background_tasks.add_task(scan_vault, {"id": vault_id})
    return state


class VaultCreationCancelled(ResponseModel):
    """A cancelled Vault creation (#735). The Vault left the catalog; a bucket already created is not deleted: bucket_left says whether it was created (created) or an attempt may have reached AWS (possible)."""

    model_config = ConfigDict(extra="forbid")
    vault_id: int
    vault_name: str
    bucket: str
    region: str
    bucket_left: Literal["created", "possible"]


@router.post("/api/vaults/{vault_id}/creation/cancel",
          response_model=passthrough(VaultCreationCancelled))
def cancel_vault_creation(vault_id: int, request: Request,
                          user: dict[str, Any] = Depends(current_user),
                          _reauth: dict[str, Any] = Depends(require_recent_reauth)):
    """Cancel the creation of a Vault whose bucket is not ready yet (#735).

    Only while the Vault is ``provisioning``: it leaves the catalog and its
    root is released. A bucket FrostVault already created is NOT deleted (it
    has no ``s3:DeleteBucket``): ``bucket`` and ``bucket_left`` name it so the
    Owner can remove it by hand, and the audit record keeps them.
    """
    del user  # authentication gate; reauthentication is checked above
    return vault_buckets_service.cancel_creation(vault_id, actor=_actor(request))


class StopProtectionJob(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    status: str
    pending_until: str | None


class StopProtectionStatus(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    vault_id: int
    vault_name: str
    stopped: bool
    root_released: bool
    cloud_disposition: Literal["keep", "delete"]
    group_id: str | None
    cancellable: bool
    pending_until: str | None
    local_files_blocking_cloud_delete: int
    jobs: list[StopProtectionJob]


@router.get("/api/vaults/{vault_id}/stop-protection", response_model=passthrough(StopProtectionStatus))
def stop_protection_status(vault_id: int, user: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        return protection_stop_service.status(connection, vault_id)


@router.post("/api/vaults/{vault_id}/stop-protection", status_code=202, response_model=passthrough(StopProtectionStatus))
def stop_folder_protection(vault_id: int, action: StopProtectionRequest, request: Request,
                           user: dict[str, Any] = Depends(current_user)):
    if action.cloud_disposition == "delete":
        require_recent_reauth(request, user)
    operation_lock = scan_lock_for_vault(vault_id)
    if not operation_lock.acquire(blocking=False, operation="stop_protection"):
        raise Conflict("api.stop_protection.scan_running")
    try:
        with db() as connection:
            return protection_stop_service.stop_protection(
                connection, vault_id=vault_id, actor=_actor(request),
                cloud_disposition=action.cloud_disposition, confirmation=action.confirmation,
                reason=action.reason)
    finally:
        operation_lock.release()


@router.post("/api/vaults/{vault_id}/stop-protection/cancel-deletion", response_model=passthrough(StopProtectionStatus))
def cancel_stopped_folder_deletion(vault_id: int, request: Request,
                                   user: dict[str, Any] = Depends(current_user)):
    del user  # authentication gate only
    with db() as connection:
        return protection_stop_service.cancel_deletion(connection, vault_id=vault_id,
                                                        actor=_actor(request))


def _vault_deletion_preview(vault_id: int) -> dict[str, Any]:
    """A live listing of the Tombstone's prefix: no listing, no preview (ADR-0047)."""
    with db() as connection:
        target = vault_deletion_service.deletion_target(connection, vault_id)
    try:
        client = s3_client()
    except Exception as exc:
        raise vault_deletion_service.VaultDeletionListingError("S3 is not configured") from exc
    return vault_deletion_service.preview(client, target, database=db)


class RetentionLockPreview(ResponseModel):
    """Files of a deletion a Governance Retention Lock still holds when it executes (#736). Dates are estimates: each version's creation plus the bucket default days. They are deleted from that date, never earlier."""

    model_config = ConfigDict(extra="forbid")
    days: int
    mode: Literal["GOVERNANCE"]
    locked_count: int
    locked_bytes: int
    locked_from: str | None = Field(description="When the first of them can be deleted.")
    locked_until: str | None = Field(description="When the last of them can be deleted.")


class VaultDeletionClass(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    storage_class: str
    version_count: int
    byte_count: int
    early_deletion_cost: float
    early_deletion_version_count: int
    minimum_days: int
    early_deletion_byte_count: int | MISSING = MISSING


class VaultDeletionPreview(ResponseModel):
    # A Vault Deletion request keeps the preview it was confirmed with: one
    # recorded by an earlier release lacks the keys added since (#731, #736).
    model_config = ConfigDict(extra="forbid")
    prefix: str
    object_count: int
    version_count: int
    delete_marker_count: int
    multipart_upload_count: int
    byte_count: int
    by_storage_class: list[VaultDeletionClass]
    early_deletion_cost: float
    currency: str
    pricing_effective_at: str
    region: str
    executes_at: str
    delay_seconds: int
    early_deletion_version_count: int | MISSING = MISSING
    early_deletion_byte_count: int | MISSING = MISSING
    savings_from: str | None | MISSING = MISSING
    early_deletion_estimated_count: int | MISSING = MISSING
    retention_lock: RetentionLockPreview | None | MISSING = MISSING


class VaultDeletionRequestState(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pending", "running", "failed"]
    requested_at: str
    pending_until: str
    reason: str
    attempts: int
    failed_at: str | None
    failure: dict[str, Any] | None
    preview: VaultDeletionPreview
    cancellable: bool
    retryable: bool
    retained_count: int | None = Field(description="Locked versions left after a run (#736): the request waits for them.")
    retained_until: str | None = Field(description="When the last of them can be deleted.")
    next_attempt_at: str | None = Field(description="When the deletion runs again for them.")
    delete_bucket: bool = Field(description="The Owner asked to delete the empty Vault Bucket too (#739).")


class VaultDeletionStatus(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    vault_id: int
    vault_name: str
    lifecycle: VaultLifecycle
    deletable: bool
    local_files: int
    request: VaultDeletionRequestState | None


@router.get("/api/vaults/{vault_id}/vault-deletion", response_model=passthrough(VaultDeletionStatus))
def vault_deletion_status(vault_id: int, _: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        return vault_deletion_service.status(connection, vault_id)


@router.get("/api/vaults/{vault_id}/vault-deletion/preview", response_model=passthrough(VaultDeletionPreview))
def vault_deletion_preview(vault_id: int, _: dict[str, Any] = Depends(current_user)):
    return _vault_deletion_preview(vault_id)


@router.post("/api/vaults/{vault_id}/vault-deletion", status_code=202, response_model=passthrough(VaultDeletionStatus))
def request_vault_deletion(vault_id: int, action: VaultDeletionRequest, request: Request,
                           user: dict[str, Any] = Depends(current_user),
                           _reauth: dict[str, Any] = Depends(require_recent_reauth)):
    """Delete a Vault Tombstone after the seven-day delay (#717, ADR-0047).

    The confirmation lists the prefix again: a request is recorded only with
    a preview that a live listing has just produced.
    """
    preview = _vault_deletion_preview(vault_id)
    with db() as connection:
        return vault_deletion_service.request_deletion(
            connection, vault_id=vault_id, actor=_actor(request),
            confirmation=action.confirmation, reason=action.reason,
            preview_summary=preview, requested_at=now_iso(),
            delete_bucket=action.delete_bucket)


@router.post("/api/vaults/{vault_id}/vault-deletion/cancel", response_model=passthrough(VaultDeletionStatus))
def cancel_vault_deletion(vault_id: int, request: Request,
                          user: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        return vault_deletion_service.cancel(connection, vault_id=vault_id,
                                             actor=_actor(request))


@router.post("/api/vaults/{vault_id}/vault-deletion/retry", response_model=passthrough(VaultDeletionStatus))
def retry_vault_deletion(vault_id: int, request: Request,
                         user: dict[str, Any] = Depends(current_user),
                         _reauth: dict[str, Any] = Depends(require_recent_reauth)):
    with db() as connection:
        return vault_deletion_service.retry(connection, vault_id=vault_id,
                                            actor=_actor(request))


class MinimumStorageDeletionClass(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    storage_class: str
    minimum_days: int
    count: int
    bytes: int
    remaining_cost: float


class MinimumStorageDeletion(ResponseModel):
    """Files of a deletion still within their Minimum Storage Duration when it executes, and what deleting them then still bills: a Cost Estimate from AWS list prices (#731)."""

    model_config = ConfigDict(extra="forbid")
    count: int
    bytes: int
    remaining_cost: float
    savings_from: str | None = Field(description="When the last Minimum Storage Duration of these files ends: deleting from then saves.")
    estimated_count: int = Field(description="How many of them have an estimated Class Entry.")
    by_storage_class: list[MinimumStorageDeletionClass]
    currency: str
    pricing_effective_at: str
    region: str
    disclaimer: str | None
    evaluated_at: str


class CleanupLastOutcome(ResponseModel):
    # Recorded by the daily run; an earlier run recorded fewer counters.
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["scheduled", "none", "capped", "skipped"]
    at: str
    file_count: int | MISSING = MISSING
    byte_count: int | MISSING = MISSING
    group_id: str | MISSING = MISSING
    run_after: str | MISSING = MISSING
    waiting_count: int | MISSING = MISSING
    waiting_bytes: int | MISSING = MISSING
    unseen_count: int | MISSING = MISSING
    held_count: int | MISSING = MISSING
    held_bytes: int | MISSING = MISSING
    minimum_count: int | MISSING = MISSING
    minimum_bytes: int | MISSING = MISSING
    minimum_cost: float | MISSING = MISSING
    savings_from: str | None | MISSING = MISSING


class CleanupPendingGroup(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    group_id: str
    file_count: int
    byte_count: int
    run_after: str | None
    cancellable: bool


class CleanupAmount(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    count: int
    bytes: int


class CleanupWaiting(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    count: int
    bytes: int
    first_eligible_at: str | None
    earliest_deletion_at: str | None


class CleanupTotals(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    deleted_count: int
    deleted_bytes: int
    last_deleted_at: str | None


class DuplicateCleanupStatus(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    grace_days: int
    cap_files: int
    cap_bytes: int
    grace_days_min: int
    grace_days_max: int
    last_run_at: str | None
    next_run_at: str | None
    last_outcome: CleanupLastOutcome | None
    pending: list[CleanupPendingGroup]
    current: CleanupAmount | None
    waiting: CleanupWaiting | None
    ready: CleanupAmount | None
    unseen: CleanupAmount | None
    totals: CleanupTotals | None
    beyond_minimum: bool = Field(description="Schedule only duplicates beyond their Minimum Storage Duration (#731).")
    held: CleanupAmount | None = Field(description="With beyond_minimum on: duplicates the next run keeps until their minimum ends.")
    minimum_storage: MinimumStorageDeletion | None
    retention_lock: RetentionLockPreview | None


@router.get("/api/vaults/{vault_id}/duplicate-cleanup", response_model=passthrough(DuplicateCleanupStatus))
def duplicate_cleanup_status(vault_id: int, _: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        return duplicate_cleanup_service.status(connection, vault_id)


@router.put("/api/vaults/{vault_id}/duplicate-cleanup", response_model=passthrough(DuplicateCleanupStatus))
def update_duplicate_cleanup(vault_id: int, action: DuplicateCleanupUpdate, request: Request,
                             user: dict[str, Any] = Depends(current_user)):
    """Change the Automatic Duplicate Cleanup of a Vault (#726, ADR-0048).

    Turning it on or changing its grace period or cap needs a fresh
    authentication; turning it off never does, nor does making it wait for
    the Minimum Storage Duration (#731), since both only delete less.
    """
    values = action.model_dump(exclude_none=True)
    narrowing = {("enabled", False), ("beyond_minimum", True)}
    if not values or not set(values.items()) <= narrowing:
        require_recent_reauth(request, user)
    with db() as connection:
        return duplicate_cleanup_service.update_settings(
            connection, vault_id=vault_id, actor=_actor(request), values=values)


class MirrorCloudLife(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    days_after_disappearance: int
    storage_class: str
    class_from_day: int
    minimum_days: int
    minimum_until_day: int | None
    beyond_minimum: bool


class MirrorCleanupStatus(DuplicateCleanupStatus):
    folders: list[str]
    cloud_life: MirrorCloudLife


@router.get("/api/vaults/{vault_id}/mirror-cleanup", response_model=passthrough(MirrorCleanupStatus))
def mirror_cleanup_status(vault_id: int, _: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        return mirror_cleanup_service.status(connection, vault_id)


@router.put("/api/vaults/{vault_id}/mirror-cleanup", response_model=passthrough(MirrorCleanupStatus))
def update_mirror_cleanup(vault_id: int, action: MirrorCleanupUpdate, request: Request,
                          user: dict[str, Any] = Depends(current_user)):
    """Change the Mirror Cleanup of a Vault (#771, ADR-0052).

    A change that could delete more (turning it on, a shorter wait, higher
    caps, broader folders, no longer waiting for the Minimum Storage Duration)
    needs a fresh authentication; one that only deletes less never does.
    """
    values = action.model_dump(exclude_none=True)
    if not values:
        require_recent_reauth(request, user)
    with db() as connection:
        return mirror_cleanup_service.update_settings(
            connection, vault_id=vault_id, actor=_actor(request), values=values,
            require_reauth=lambda: require_recent_reauth(request, user))


class MinimumStorageVersions(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    count: int
    bytes: int


class MinimumStorageClass(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    storage_class: str
    minimum_days: int
    within: MinimumStorageVersions = Field(description="Versions still within the Minimum Storage Duration.")
    beyond: MinimumStorageVersions = Field(description="Versions beyond it, or in a class without one.")
    remaining_cost: float
    estimated_count: int


class MinimumStorageRelease(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    month: str
    count: int
    bytes: int


class MinimumStorageSummary(ResponseModel):
    """Minimum Storage Duration of a Vault's versions (#731): a Cost Estimate from AWS list prices."""

    model_config = ConfigDict(extra="forbid")
    vault_id: int
    by_storage_class: list[MinimumStorageClass]
    remaining_cost: float
    releases: list[MinimumStorageRelease] = Field(description="Month by month (YYYY-MM, UTC), the versions whose Minimum Storage Duration ends.")
    estimated_count: int = Field(description="Versions with a minimum whose Class Entry is estimated from the Storage Plan.")
    currency: str
    pricing_effective_at: str
    region: str
    disclaimer: str | None
    evaluated_at: str


@router.get("/api/vaults/{vault_id}/minimum-storage", response_model=passthrough(MinimumStorageSummary))
def minimum_storage_summary(vault_id: int, _: dict[str, Any] = Depends(current_user)):
    """Minimum Storage Duration of a Vault's versions (#731): per storage class,
    those within and beyond it, what the minimum still bills and, month by
    month, when the versions within it are released. A list-price estimate."""
    with db() as connection:
        return minimum_storage_service.load_vault_summary(connection, vault_id)


class CloudUsageEstimateClass(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    storage_class: str
    versions: int
    bytes: int
    monthly_cost: float


class CloudUsageEstimate(ResponseModel):
    """The catalog's versions not known to be missing, by class, with a Cost Estimate from AWS list prices."""

    model_config = ConfigDict(extra="forbid")
    versions: int
    bytes: int
    monthly_cost: float
    currency: str
    region: str
    pricing_effective_at: str
    by_storage_class: list[CloudUsageEstimateClass]


class CloudUsageRead(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    attempted_at: str
    outcome: Literal["ok", "no_data", "access_denied", "failed"]
    error_code: str | None


class CloudUsageDay(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    day: str
    bytes: int
    overhead_bytes: int
    object_count: int | None


class CloudUsageOverhead(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    storage_type: str = Field(description="The CloudWatch StorageType dimension value.")
    kind: Literal["object_overhead", "size_overhead", "staging"]
    billed_as: str = Field(description="The storage class whose price AWS applies to these bytes.")
    bytes: int


class CloudUsageMeasuredClass(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    storage_class: str
    bytes: int = Field(description="The objects' own bytes in this class.")
    overhead_bytes: int = Field(description="Bytes AWS bills on top of the objects of this class.")
    overhead: list[CloudUsageOverhead]


class CloudUsageMeasured(ResponseModel):
    """Daily S3 storage metrics read from CloudWatch GetMetricStatistics (#737)."""

    # A shared or provisioning bucket answers its status only; the newest data
    # point's fields come with ``available``.
    model_config = ConfigDict(extra="forbid")
    status: Literal["available", "pending", "no_data", "access_denied", "failed", "shared_bucket", "provisioning"] = Field(description="available: a data point is saved. pending: not read yet. no_data, access_denied, failed: the last read's outcome, with nothing saved. shared_bucket: an older Vault on a shared bucket, never read. provisioning: the Vault Bucket is being created.")
    last_read: CloudUsageRead | None | MISSING = MISSING
    next_read_after: str | None | MISSING = Field(default=MISSING, description="When the Vault Bucket is read again: 24 hours after an ok read, 6 hours after any other outcome.")
    history: list[CloudUsageDay] | MISSING = Field(default=MISSING, description="Saved daily totals, newest first.")
    day: str | MISSING = Field(default=MISSING, description="UTC day of the newest data point.")
    read_at: str | MISSING = MISSING
    object_count: int | None | MISSING = MISSING
    bytes: int | MISSING = MISSING
    overhead_bytes: int | MISSING = MISSING
    by_storage_class: list[CloudUsageMeasuredClass] | MISSING = MISSING


class VaultCloudUsage(ResponseModel):
    """A Vault's cloud usage (#737): the catalog's Cost Estimate next to the CloudWatch sizes of its Vault Bucket."""

    model_config = ConfigDict(extra="forbid")
    vault_id: int
    bucket: str
    region: str
    dedicated_bucket: bool
    estimate: CloudUsageEstimate
    measured: CloudUsageMeasured


@router.get("/api/vaults/{vault_id}/cloud-usage", response_model=passthrough(VaultCloudUsage))
def vault_cloud_usage(vault_id: int, _: dict[str, Any] = Depends(current_user)):
    """A Vault's cloud usage (#737): the catalog's Cost Estimate by storage
    class next to the bytes and objects CloudWatch measured in its Vault
    Bucket once a day. Reads only the catalog, never AWS."""
    with db() as connection:
        return bucket_metrics_service.vault_usage(connection, vault_id)


class BilledCategories(ResponseModel):
    """Billed amounts by category, in the billing currency; every category is present."""

    model_config = ConfigDict(extra="forbid")
    storage: float
    requests: float
    transitions: float
    retrievals: float
    data_transfer: float
    other: float
    credits: float = Field(description="Credits, refunds and discounts of the bucket (negative).")


BillingExportState = Literal["unavailable", "checking", "not_configured", "waiting", "ok", "error"]
BillingExportError = Literal[
    "s3_unreachable", "s3_throttled", "s3_server_error", "credentials", "access_denied",
    "s3_refused", "parquet", "missing_column", "not_gzip_csv", "short_row", "cost_not_number",
    "currency_missing", "mixed_currencies", "manifest_invalid", "manifest_too_large",
    "manifest_foreign_bucket", "manifest_outside_folder", "no_data_files", "internal",
]


class BillingExportFields(ResponseModel):
    """The billing export's state, shared by the installation and each Vault (#738)."""

    model_config = ConfigDict(extra="forbid")
    state: BillingExportState = Field(description="unavailable: no System Bucket; checking: the first look is running; not_configured: nothing under the prefix; waiting: the export exists without a first manifest; ok: read; error: unreadable (see error_code).")
    bucket: str | None = Field(description="The System Bucket the export is delivered to.")
    prefix: str = Field(description="The S3 path prefix of the export in the System Bucket.")
    checked_at: str | None
    next_check_at: str | None
    read_at: str | None = Field(description="When FrostVault last read the export (only when ok).")
    exported_at: str | None = Field(description="When AWS delivered the newest manifest that was read.")
    data_through: str | None = Field(description="The newest usage end in the export, never after exported_at.")
    currency: str | None = Field(description="The billing currency (ISO 4217).")
    stale: bool = Field(description="ok but not up to date: the export AWS delivered is over 3 days old, or the current month is missing after the 3rd.")
    error_code: BillingExportError | None = Field(description="Why the export could not be read (state error); the UI translates it. s3_unreachable, s3_throttled and s3_server_error are retried after 6 hours, the others after a day.")
    error_detail: str | None = Field(description="An S3 action and AWS error code, column or currency names: never a provider message.")


class VaultBilledPeriod(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    period: str = Field(description="YYYY-MM")
    total: float
    categories: BilledCategories


class VaultBilledCost(BillingExportFields):
    """A Vault's Billed Cost from the billing export (#738), a block of its cloud usage next to the Cost Estimate (#737)."""

    vault_id: int
    vault_bucket: str
    shared: bool = Field(description="The Vault is in a shared bucket: it has no Billed Cost of its own and periods is empty; the bucket is an installation row of GET /api/billing-export.")
    periods: list[VaultBilledPeriod] = Field(description="Newest first; empty unless state is ok, and for a shared bucket.")


@router.get("/api/vaults/{vault_id}/billed-cost", response_model=passthrough(VaultBilledCost))
def vault_billed_cost(vault_id: int, _: dict[str, Any] = Depends(current_user)):
    """The Vault's Billed Cost from the billing export (#738), by billing
    period and category, with the export's status; never a Cost Explorer call."""
    with db() as connection:
        return billed_cost_service.vault_view(connection, vault_id)


class BilledVaultBucket(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    vault_id: int
    name: str | None
    bucket: str
    total: float
    categories: BilledCategories


class BilledInstallationBucket(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "shared"]
    bucket: str
    total: float
    categories: BilledCategories


class BillingExportPeriod(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    period: str = Field(description="YYYY-MM")
    total: float = Field(description="Every row of the period: Vaults and installation.")
    vaults_total: float = Field(description="The Vault rows only: FrostVault's Vault Buckets, not the whole AWS bill.")
    vaults: list[BilledVaultBucket]
    installation: list[BilledInstallationBucket] = Field(description="The System Bucket and shared buckets of older Vaults: not attributed to one Vault.")


class BillingExportStatus(BillingExportFields):
    """The AWS Data Exports (CUR 2.0) billing export and every known bucket's Billed Cost (#738). Never a Cost Explorer figure."""

    periods: list[BillingExportPeriod] = Field(description="Billing periods read, newest first; empty unless state is ok.")


@router.get("/api/billing-export", response_model=passthrough(BillingExportStatus))
def billing_export_status(_: dict[str, Any] = Depends(current_user)):
    """The billing export's status and every known bucket's Billed Cost (#738)."""
    with db() as connection:
        return billed_cost_service.installation_view(connection)


class SourceVolumeInventoryItem(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str
    path: str
    access: str
    health: str
    vault_count: int
    diagnostic: str | None
    diagnostic_code: str | None


class SourceVolumeInventoryResponse(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[SourceVolumeInventoryItem]


@inventory_router.get("/api/general/source-volumes", response_model=passthrough(SourceVolumeInventoryResponse))
def general_source_volumes(user: dict[str, Any] = Depends(current_user)):
    """Operator inventory of discovered Source Volumes (issue #148)."""
    return {"items": source_volume_inventory()}


class SourceDirectoryOccupation(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["vault"]
    vault_name: str


class SourceDirectoryEntry(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    relative_path: str
    navigable: bool
    selectable: bool
    occupation: SourceDirectoryOccupation | None


class SourceDirectoryBrowseResponse(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    volume_alias: str
    relative_path: str
    items: list[SourceDirectoryEntry]


@inventory_router.get("/api/source-volumes/{volume_alias}/browse", response_model=passthrough(SourceDirectoryBrowseResponse))
def browse_source_volume(
    volume_alias: str,
    path: str = Query(default=""),
    reconnect_vault_id: int | None = Query(default=None),
    _: dict[str, Any] = Depends(current_user),
):
    """Lazy directory browser for choosing a Vault root on a Source Volume.

    ``reconnect_vault_id`` treats that Vault's own root as free: Reconnect may
    accept the same folder again (#656)."""
    with db() as connection:
        return source_directories_service.browse_source_directories(
            connection,
            volume_alias=volume_alias,
            relative_path=path,
            exclude_vault_id=reconnect_vault_id,
        )


class GeneralVault(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    uuid: str
    name: str
    source_root: str
    s3_bucket: str
    s3_region: str
    s3_prefix: str
    lifecycle: VaultLifecycle
    paused: bool
    root_released_at: str | None
    dedicated_bucket: bool
    retention_lock_days: int | None
    vault_deletion_status: Literal["pending", "running", "failed"] | None
    bucket_provisioning: VaultBucketStatus | None


class GeneralVaultsResponse(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[GeneralVault]


@inventory_router.get("/api/general/vaults", response_model=passthrough(GeneralVaultsResponse))
def general_vaults(_: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        return {"items": list_vault_inventory(connection)}


class GeneralVaultReconnectResponse(ResponseModel):
    model_config = ConfigDict(extra="forbid")
    vault_id: int
    source_root: str
    # The accepted root waits for its mandatory full scan (``reconnect_required``).
    reconnect_state: Literal["reconnect_required"]
    full_scan_required: bool


@inventory_router.post("/api/vaults/{vault_id}/reconnect", response_model=passthrough(GeneralVaultReconnectResponse))
def reconnect_vault_root(
    vault_id: int,
    action: VaultReconnect,
    background_tasks: BackgroundTasks,
    request: Request,
    owner: dict[str, Any] = Depends(current_user),
):
    """Accept this folder/mount for this Vault only; mandatory cloud/local scan."""
    # Serialize the runtime snapshot, relocation transaction, and scan start.
    # Without this lock a scan can begin after the ``busy`` snapshot but before
    # relocation publishes its persistent suspension state.
    relocation_lock = scan_lock_for_vault(vault_id)
    if not relocation_lock.acquire(blocking=False, operation="reconnect"):
        raise vault_relocation_service.VaultRelocationError("active_jobs")
    try:
        with status_lock:
            busy = bool(runtime_status.get(vault_id, {}).get("scanning"))
        try:
            with db() as connection:
                vault = vault_relocation_service.reconnect_vault_root(
                    connection,
                    vault_id=vault_id,
                    volume_alias=action.volume_alias,
                    relative_path=action.relative_path,
                    actor=_actor(request),
                    reason=action.reason,
                    runtime_busy=busy,
                )
        except vault_relocation_service.VaultRelocationError:
            # A refusal took no acceptance: nothing to release.
            raise
        except Exception:
            # A failed outer transaction/commit must not leave the in-process
            # gate stuck when persistent acceptance rolled back.
            vault_relocation_service.release_relocation_scan_runtime(vault_id)
            raise
    finally:
        relocation_lock.release()
    # BackgroundTasks registration is non-I/O and cannot partially dispatch.
    # A crash before execution is recovered by scan_all_vaults on next start;
    # lifecycle stays reconnect_required until the local scan succeeds.
    background_tasks.add_task(scan_vault, dict(vault))
    return {
        "vault_id": vault_id,
        "source_root": vault["source_root"],
        "reconnect_state": vault.get("lifecycle"),
        "full_scan_required": True,
    }
