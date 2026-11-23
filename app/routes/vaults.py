"""Vault management routes: creation, bucket, protection stop, deletion, cleanups, usage and reconnect."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..api_models import response_model
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
from ..sessions import set_session_vault
from ..services.runtime_status import runtime_status, scan_lock_for_vault, status_lock
from ..storage import now_iso, scan_vault, s3_client
from .deps import (
    _OPERATIONAL_VAULT,
    _actor,
    _api_message,
    _request_locale,
    current_user,
    require_recent_reauth,
)


router = APIRouter()

# Registered after the Job routes: two routers keep the historical registration
# order, and with it the order of the OpenAPI paths.
inventory_router = APIRouter()


class VaultSelection(BaseModel):
    vault_id: int


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


@router.post("/api/vaults/{vault_id}/pause", response_model=response_model("VaultPauseResponse"))
def pause_vault(vault_id: int, request: Request, user: dict[str, Any] = Depends(current_user)):
    return _set_vault_pause(vault_id, True, request, user)


@router.post("/api/vaults/{vault_id}/resume", response_model=response_model("VaultPauseResponse"))
def resume_vault(vault_id: int, request: Request, user: dict[str, Any] = Depends(current_user)):
    return _set_vault_pause(vault_id, False, request, user)


def _set_vault_pause(vault_id: int, paused: bool, request: Request, user: dict[str, Any]):
    from ..services import vault_pause
    with db() as connection:
        return vault_pause.set_paused(connection, vault_id=vault_id, paused=paused,
                                       locale=_request_locale(request))


@router.get("/api/vaults", response_model=response_model("VaultsResponse"))
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


@router.post("/api/vaults", status_code=201, response_model=response_model("VaultCreateResponse"))
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


@router.get("/api/vaults/{vault_id}/bucket", response_model=response_model("VaultBucketStatus"))
def vault_bucket_status(vault_id: int, _: dict[str, Any] = Depends(current_user)):
    """The Vault's bucket and, while it is being created, the creation state (#735)."""
    with db() as connection:
        state = vault_buckets_service.status(connection, vault_id)
    if state is None:
        raise VaultNotFound()
    return state


@router.get("/api/vaults/{vault_id}/retention-lock", response_model=response_model("RetentionLockStatus"))
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


@router.post("/api/vaults/{vault_id}/bucket/retry", response_model=response_model("VaultBucketStatus"))
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


@router.post("/api/vaults/{vault_id}/creation/cancel",
          response_model=response_model("VaultCreationCancelled"))
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


@router.get("/api/vaults/{vault_id}/stop-protection", response_model=response_model("StopProtectionStatus"))
def stop_protection_status(vault_id: int, user: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        return protection_stop_service.status(connection, vault_id)


@router.post("/api/vaults/{vault_id}/stop-protection", status_code=202, response_model=response_model("StopProtectionStatus"))
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


@router.post("/api/vaults/{vault_id}/stop-protection/cancel-deletion", response_model=response_model("StopProtectionStatus"))
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


@router.get("/api/vaults/{vault_id}/vault-deletion", response_model=response_model("VaultDeletionStatus"))
def vault_deletion_status(vault_id: int, _: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        return vault_deletion_service.status(connection, vault_id)


@router.get("/api/vaults/{vault_id}/vault-deletion/preview", response_model=response_model("VaultDeletionPreview"))
def vault_deletion_preview(vault_id: int, _: dict[str, Any] = Depends(current_user)):
    return _vault_deletion_preview(vault_id)


@router.post("/api/vaults/{vault_id}/vault-deletion", status_code=202, response_model=response_model("VaultDeletionStatus"))
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


@router.post("/api/vaults/{vault_id}/vault-deletion/cancel", response_model=response_model("VaultDeletionStatus"))
def cancel_vault_deletion(vault_id: int, request: Request,
                          user: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        return vault_deletion_service.cancel(connection, vault_id=vault_id,
                                             actor=_actor(request))


@router.post("/api/vaults/{vault_id}/vault-deletion/retry", response_model=response_model("VaultDeletionStatus"))
def retry_vault_deletion(vault_id: int, request: Request,
                         user: dict[str, Any] = Depends(current_user),
                         _reauth: dict[str, Any] = Depends(require_recent_reauth)):
    with db() as connection:
        return vault_deletion_service.retry(connection, vault_id=vault_id,
                                            actor=_actor(request))


@router.get("/api/vaults/{vault_id}/duplicate-cleanup", response_model=response_model("DuplicateCleanupStatus"))
def duplicate_cleanup_status(vault_id: int, _: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        return duplicate_cleanup_service.status(connection, vault_id)


@router.put("/api/vaults/{vault_id}/duplicate-cleanup", response_model=response_model("DuplicateCleanupStatus"))
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


@router.get("/api/vaults/{vault_id}/mirror-cleanup", response_model=response_model("MirrorCleanupStatus"))
def mirror_cleanup_status(vault_id: int, _: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        return mirror_cleanup_service.status(connection, vault_id)


@router.put("/api/vaults/{vault_id}/mirror-cleanup", response_model=response_model("MirrorCleanupStatus"))
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


@router.get("/api/vaults/{vault_id}/minimum-storage", response_model=response_model("MinimumStorageSummary"))
def minimum_storage_summary(vault_id: int, _: dict[str, Any] = Depends(current_user)):
    """Minimum Storage Duration of a Vault's versions (#731): per storage class,
    those within and beyond it, what the minimum still bills and, month by
    month, when the versions within it are released. A list-price estimate."""
    with db() as connection:
        return minimum_storage_service.load_vault_summary(connection, vault_id)


@router.get("/api/vaults/{vault_id}/cloud-usage", response_model=response_model("VaultCloudUsage"))
def vault_cloud_usage(vault_id: int, _: dict[str, Any] = Depends(current_user)):
    """A Vault's cloud usage (#737): the catalog's Cost Estimate by storage
    class next to the bytes and objects CloudWatch measured in its Vault
    Bucket once a day. Reads only the catalog, never AWS."""
    with db() as connection:
        return bucket_metrics_service.vault_usage(connection, vault_id)


@router.get("/api/vaults/{vault_id}/billed-cost", response_model=response_model("VaultBilledCost"))
def vault_billed_cost(vault_id: int, _: dict[str, Any] = Depends(current_user)):
    """The Vault's Billed Cost from the billing export (#738), by billing
    period and category, with the export's status; never a Cost Explorer call."""
    with db() as connection:
        return billed_cost_service.vault_view(connection, vault_id)


@router.get("/api/billing-export", response_model=response_model("BillingExportStatus"))
def billing_export_status(_: dict[str, Any] = Depends(current_user)):
    """The billing export's status and every known bucket's Billed Cost (#738)."""
    with db() as connection:
        return billed_cost_service.installation_view(connection)


@router.post("/api/vaults/select", response_model=response_model("VaultSelectResponse"))
def select_vault(
    action: VaultSelection,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
):
    with db() as connection:
        allowed = connection.execute(
            f"SELECT 1 FROM vaults v WHERE v.id=%s AND {_OPERATIONAL_VAULT}",
            (action.vault_id,),
        ).fetchone()
        if not allowed:
            raise NotFound("api.protected_vault_not_found")
        updated = set_session_vault(
            connection,
            request.state.session["id"],
            action.vault_id,
            expected_version=request.state.session["transition_version"],
        )
    if not updated:
        # The row transition is conditional on the Session snapshot observed by
        # current_user, so another process cannot be silently overwritten.
        raise Conflict("api.session_changed")
    request.state.session.update(updated)
    return {**_api_message(request, "api.vault_selected")}


@inventory_router.get("/api/general/source-volumes", response_model=response_model("SourceVolumeInventoryResponse"))
def general_source_volumes(user: dict[str, Any] = Depends(current_user)):
    """Operator inventory of discovered Source Volumes (issue #148)."""
    return {"items": source_volume_inventory()}


@inventory_router.get("/api/source-volumes/{volume_alias}/browse", response_model=response_model("SourceDirectoryBrowseResponse"))
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


@inventory_router.get("/api/general/vaults", response_model=response_model("GeneralVaultsResponse"))
def general_vaults(_: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        return {"items": list_vault_inventory(connection)}


@inventory_router.post("/api/vaults/{vault_id}/reconnect", response_model=response_model("GeneralVaultReconnectResponse"))
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
