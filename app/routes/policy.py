"""Operation Policy and exclusion routes."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from ..api_models import response_model
from ..config import settings
from ..services.audit_events import audit_log
from ..database import db
from ..errors import Conflict, Invalid
from ..services import watch_plan
from ..services.excluded_folders import excluded_folders
from ..services import vault_exclusions
from ..services.directory_aggregates import request_vault_rebuild
from ..services.operation_policies import (
    AUTO_UPLOAD_EXCLUSIONS,
    OperationPolicy,
    get_policy,
    lock_vault_policy,
    preview_glob_rules,
    set_policy,
)
from ..services.cost_estimates import estimate_storage_month, list_prices
from ..services.vault_paths import safe_relative_path
from ..storage import request_full_scan
from .deps import (
    LOGICAL_PATH_ERROR_RESPONSES,
    _actor,
    current_user,
    vault_in_path,
    normalize_directory,
)


router = APIRouter()


class OperationPolicyUpdate(BaseModel):
    """Update supplied fields only; omitted settings retain their current values."""

    model_config = ConfigDict(extra="forbid")

    auto_upload: bool = True
    upload_verification: Literal["s3_checksum", "read_back"] | None = None
    local_release_after_days: Literal[90, 180, 365, 730] | None = None
    stability_seconds: int = Field(default=300, ge=0)
    include_globs: list[str] = Field(default_factory=list)
    exclude_globs: list[str] = Field(default_factory=list)


class OperationPolicyGlobEdit(BaseModel):
    """Add or remove one glob, atomically against the stored policy (#754)."""

    model_config = ConfigDict(extra="forbid")

    field: Literal["include_globs", "exclude_globs"]
    action: Literal["add", "remove"]
    glob: str = Field(min_length=1, max_length=2048)


class VaultItemExclusion(BaseModel):
    """Exclude one Vault file or folder by its own rule (#754)."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    is_directory: bool = False


class GlobPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[str] = Field(default_factory=list)
    include_globs: list[str] = Field(default_factory=list)
    exclude_globs: list[str] = Field(default_factory=list)


@router.get("/api/vaults/{vault_id}/operation-policy", response_model=response_model("OperationPolicy"))
def own_vault_operation_policy(vault: dict[str, Any] = Depends(vault_in_path)):
    with db() as connection:
        policy = get_policy(connection, vault["id"])
    return _operation_policy_response(policy)


def _look_inside_released_folders(vault: dict[str, Any], current: Any) -> None:
    """An exclusion that pruned folders was removed or narrowed (ADR-0044).

    The watcher registers those folders again. When it had registered around
    one of them, that expansion is paired with a full scan by the watcher
    itself (ADR-0027); otherwise a full scan is requested here, now or right
    after the one in progress. Either way the folder's files are catalogued
    again and those gone from disk become missing.
    """
    released = watch_plan.release_excluded(int(vault["id"]), current.covers_directory)
    if not released:
        request_full_scan(dict(vault))


def _save_operation_policy(
    vault: dict[str, Any],
    request: Request,
    change: Any,
    *,
    event: str = "vault_operation_policy_updated",
    **audit_fields: Any,
) -> OperationPolicy:
    """Apply ``change(stored_policy) -> new_policy`` atomically and audit it.

    Every save of a Vault policy goes through here: the read, the change and the
    write are serialized per Vault, so an independent panel or a one-rule edit
    never replays a stale value. A change of the globs refreshes the directory
    rollups; dropping a folder rule makes the Vault look inside again.
    """
    with db() as connection:
        # Independent panels must never replay a stale local-release value
        # when saving upload rules. Serialize read/merge/write per Vault.
        lock_vault_policy(connection, vault["id"])
        before = get_policy(connection, vault["id"])
        stored = set_policy(connection, vault["id"], change(before))
        if (before.include_globs, before.exclude_globs) != (stored.include_globs, stored.exclude_globs):
            # Excluded is derived from these globs (#498); refresh the
            # durable directory rollups, never the catalog evidence.
            request_vault_rebuild(connection, vault["id"])
        # The Vault may look inside folders it did not observe (ADR-0044).
        narrowed_folders = excluded_folders(before).narrowed_by(excluded_folders(stored))
        audit_log(
            event,
            connection=connection,
            vault_id=vault["id"],
            actor=_actor(request),
            outcome="updated",
            policy=stored.as_dict(),
            **audit_fields,
        )
    if narrowed_folders:
        _look_inside_released_folders(vault, excluded_folders(stored))
    return stored


def _operation_policy_response(policy: OperationPolicy) -> dict[str, Any]:
    return {**policy.as_dict(), "global_upload_verification": settings.upload_verification,
            "effective_upload_verification": policy.effective_upload_verification(),
            "fixed_exclude_globs": list(AUTO_UPLOAD_EXCLUSIONS)}


@router.put("/api/vaults/{vault_id}/operation-policy", response_model=response_model("OperationPolicy"))
def update_vault_operation_policy(
    action: OperationPolicyUpdate,
    request: Request,
    vault: dict[str, Any] = Depends(vault_in_path),
    user: dict[str, Any] = Depends(current_user),
):
    update = action.model_dump(exclude_unset=True)

    def change(before: OperationPolicy) -> OperationPolicy:
        return OperationPolicy(**{**before.as_dict(), **update})

    return _operation_policy_response(_save_operation_policy(vault, request, change))


@router.post("/api/vaults/{vault_id}/operation-policy/globs", response_model=response_model("OperationPolicy"))
def edit_vault_operation_glob(
    action: OperationPolicyGlobEdit,
    request: Request,
    vault: dict[str, Any] = Depends(vault_in_path),
):
    """Add or remove one include/exclude glob against the stored policy (#754).

    Adding a glob already present changes nothing; removing one that is no
    longer there is refused with 409, so a stale view cannot replace the list.
    """

    def change(before: OperationPolicy) -> OperationPolicy:
        return vault_exclusions.edit_glob(before, action.field, action.action, action.glob)

    stored = _save_operation_policy(
        vault, request, change, event=f"vault_operation_glob_{'added' if action.action == 'add' else 'removed'}",
        field=action.field, glob=action.glob.strip(),
    )
    return _operation_policy_response(stored)


def _vault_item_path(path: str) -> str:
    return safe_relative_path(path).as_posix().rstrip("/")


@router.get(
    "/api/vaults/{vault_id}/excluded-folders",
    response_model=response_model("ExcludedFolderListing"),
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def vault_excluded_folders(
    directory: str = "",
    vault: dict[str, Any] = Depends(vault_in_path),
):
    """One Vault level read from disk for its Excluded Folders (#754, ADR-0044).

    Outside an Excluded Folder it lists the subfolders a folder rule covers;
    inside one it lists the level's entries, never catalogued nor fingerprinted.
    """
    directory = normalize_directory(directory, api=True)
    with db() as connection:
        policy = get_policy(connection, vault["id"])
    return vault_exclusions.directory_listing(vault, policy, directory)


def _item_exclusion(policy: OperationPolicy, path: str, is_directory: bool) -> dict[str, Any]:
    rule = vault_exclusions.own_rule(path, is_directory=is_directory)
    present = rule is not None and rule in policy.exclude_globs
    reasons = vault_exclusions.exclusion_reasons(policy, path, is_directory=is_directory)
    return {
        "path": path,
        "is_directory": is_directory,
        "own_rule": rule,
        "own_rule_present": present,
        "excluded": reasons["excluded"],
        "rules": [item for item in reasons["rules"] if item != rule],
        "fixed": reasons["fixed"],
        "not_included": reasons["not_included"],
    }


@router.get(
    "/api/vaults/{vault_id}/exclusions/item",
    response_model=response_model("VaultItemExclusionStatus"),
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def vault_item_exclusion(
    path: str,
    is_directory: bool = False,
    vault: dict[str, Any] = Depends(vault_in_path),
):
    """Which rules exclude one item, and its own rule (#754)."""
    path = _vault_item_path(path)
    with db() as connection:
        policy = get_policy(connection, vault["id"])
    return _item_exclusion(policy, path, is_directory)


@router.get(
    "/api/vaults/{vault_id}/exclusions/inclusion-preview",
    response_model=response_model("VaultInclusionPreview"),
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def vault_inclusion_preview(
    path: str,
    is_directory: bool = False,
    vault: dict[str, Any] = Depends(vault_in_path),
):
    """What «Include again» would upload, counted from disk now (#754).

    Only when the item's own rule is what excludes it: the counts honour the
    policy without that rule and stop after a time budget (``complete`` false
    means a minimum). Otherwise only the broader rules are returned.
    """
    path = _vault_item_path(path)
    with db() as connection:
        policy = get_policy(connection, vault["id"])
    status = _item_exclusion(policy, path, is_directory)
    result: dict[str, Any] = {**status, "includable": False, "files": None, "bytes": None,
                              "complete": None, "estimate": None, "folder_rule": False}
    if not status["own_rule_present"]:
        return result
    after = vault_exclusions.edit_glob(policy, "exclude_globs", "remove", status["own_rule"])
    remaining = _item_exclusion(after, path, is_directory)
    result.update(
        rules=remaining["rules"], fixed=remaining["fixed"], not_included=remaining["not_included"],
        includable=True, folder_rule=is_directory,
    )
    try:
        measured = vault_exclusions.measure_upload(vault, after, path, is_directory=is_directory)
    except vault_exclusions.DiskReadError:
        # The rule can still be removed; the amount is unknown.
        return result
    result.update(measured)
    result["estimate"] = estimate_storage_month(
        list_prices(settings.aws_region), size_bytes=measured["bytes"], storage_class="STANDARD",
    ).as_dict()
    return result


@router.post(
    "/api/vaults/{vault_id}/exclusions/exclude",
    response_model=response_model("OperationPolicy"),
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def exclude_vault_item(
    action: VaultItemExclusion,
    request: Request,
    vault: dict[str, Any] = Depends(vault_in_path),
):
    """«Exclude from the Vault»: add the item's own rule (#754).

    A folder gets ``path/**`` (an Excluded Folder), a file its exact path. Cloud
    copies stay; the files become Excluded.
    """
    path = _vault_item_path(action.path)
    rule = vault_exclusions.own_rule(path, is_directory=action.is_directory)
    if rule is None:
        raise Invalid("api.policy.glob_characters")
    if not action.is_directory and "/" not in path:
        # A rule without "/" matches the name in every folder: not this item only.
        raise Invalid("api.policy.root_file_rule")

    def change(before: OperationPolicy) -> OperationPolicy:
        return vault_exclusions.edit_glob(before, "exclude_globs", "add", rule)

    stored = _save_operation_policy(
        vault, request, change, event="vault_item_excluded", path=path,
        is_directory=action.is_directory, glob=rule,
    )
    return _operation_policy_response(stored)


@router.post(
    "/api/vaults/{vault_id}/exclusions/include",
    response_model=response_model("OperationPolicy"),
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def include_vault_item(
    action: VaultItemExclusion,
    request: Request,
    vault: dict[str, Any] = Depends(vault_in_path),
):
    """«Include again»: remove the item's own rule, and only that (#754).

    A broader rule is never removed from here (409): it is edited in the
    Exclusions panel.
    """
    path = _vault_item_path(action.path)
    rule = vault_exclusions.own_rule(path, is_directory=action.is_directory)
    if rule is None:
        raise Conflict("api.policy.no_own_rule")

    def change(before: OperationPolicy) -> OperationPolicy:
        return vault_exclusions.edit_glob(before, "exclude_globs", "remove", rule)

    stored = _save_operation_policy(
        vault, request, change, event="vault_item_included", path=path,
        is_directory=action.is_directory, glob=rule,
    )
    return _operation_policy_response(stored)


@router.post("/api/vaults/{vault_id}/operation-policy/preview-globs", response_model=response_model("GlobPreviewResponse"))
def preview_vault_operation_globs(
    action: GlobPreviewRequest,
    vault: dict[str, Any] = Depends(vault_in_path),
):
    return preview_glob_rules(
        paths=action.paths,
        include_globs=action.include_globs,
        exclude_globs=action.exclude_globs,
    )
