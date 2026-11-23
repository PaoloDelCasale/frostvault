"""Storage Plan, storage estimate and Vault move routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..api_models import response_model
from ..config import settings
from ..services.audit_events import audit_log
from ..database import db
from ..errors import BadRequest, Conflict, VaultNotFound
from ..services.cost_estimates import estimate_storage_month, list_prices
from ..services.lifecycle_policies import (
    load_vault_plan,
    set_vault_plan,
    sync_lifecycle_rules_for_bucket,
)
from ..services.lifecycle_profiles import (
    GUIDED_PROFILES,
    LifecycleProfile,
    LifecycleTransition,
    guided_profile,
    profile_to_dict,
    validate_lifecycle_profile,
)
from ..services import vault_moves
from ..storage import now_iso, s3_client
from .deps import _actor, current_user, current_vault


router = APIRouter()


class LifecycleTransitionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: int = Field(strict=True, ge=0)
    storage_class: str = Field(min_length=1, max_length=40)


class LifecycleProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transitions: list[LifecycleTransitionUpdate] = Field(default_factory=list)

    def to_profile(self) -> LifecycleProfile:
        return LifecycleProfile(
            transitions=tuple(
                LifecycleTransition(
                    days=item.days,
                    storage_class=item.storage_class.upper(),
                )
                for item in self.transitions
            ),
        )


class LifecycleProfileSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guided_profile: str | None = Field(default=None, min_length=1, max_length=80)
    profile: LifecycleProfileUpdate | None = None

    @model_validator(mode="after")
    def validate_selection(self) -> "LifecycleProfileSelection":
        if (self.guided_profile is None) == (self.profile is None):
            raise ValueError("provide exactly one of guided_profile or profile")
        return self


class StorageEstimateRequest(BaseModel):
    """Monthly storage estimate for an explicit size or a Vault's cloud data."""

    model_config = ConfigDict(extra="forbid")

    size_bytes: int | None = Field(default=None, ge=0)
    vault_id: int | None = Field(default=None, ge=1)
    storage_class: str = Field(default="STANDARD", min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_target(self) -> "StorageEstimateRequest":
        if (self.size_bytes is None) == (self.vault_id is None):
            raise ValueError("Provide exactly one of size_bytes or vault_id")
        return self


@router.post(
    "/api/general/cost-estimates/storage",
    response_model=response_model("StorageEstimateResponse"),
)
def storage_cost_estimate(
    action: StorageEstimateRequest,
    _: dict[str, Any] = Depends(current_user),
):
    size_bytes = action.size_bytes
    if action.vault_id is not None:
        with db() as connection:
            if not connection.execute(
                "SELECT id FROM vaults WHERE id=%s", (action.vault_id,)
            ).fetchone():
                raise VaultNotFound()
            # Every Archive Version has a known size; the Vault is the file's.
            usage = connection.execute(
                """
                SELECT COALESCE(SUM(av.size), 0) AS total
                FROM archive_versions av JOIN files f ON f.id = av.file_id
                WHERE f.vault_id=%s AND av.availability='available'
                """,
                (action.vault_id,),
            ).fetchone()
        size_bytes = int(usage["total"] or 0)
    return estimate_storage_month(
        list_prices(settings.aws_region),
        size_bytes=int(size_bytes or 0),
        storage_class=action.storage_class,
    ).as_dict()


def _selected_lifecycle_profile(
    action: LifecycleProfileSelection,
) -> tuple[LifecycleProfile, str | None]:
    if action.guided_profile is not None:
        return guided_profile(action.guided_profile), action.guided_profile
    if action.profile is None:  # Defensive; Pydantic enforces exactly one selection.
        raise BadRequest("api.storage_plan.profile_required")
    profile = action.profile.to_profile()
    validation = validate_lifecycle_profile(profile)
    if not validation.ok:
        raise BadRequest("api.storage_plan.invalid", errors="; ".join(validation.errors))
    return profile, None


def _lifecycle_payload(connection: Any, vault: dict[str, Any]) -> dict[str, Any]:
    return {
        "profile": profile_to_dict(load_vault_plan(connection, vault["id"])),
        "guided_profiles": {
            name: profile_to_dict(profile)
            for name, profile in GUIDED_PROFILES.items()
        },
    }


class VaultMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vault_id: int = Field(gt=0)
    target_class: str = Field(min_length=1, max_length=40)
    acknowledge_costs: bool = False


@router.get("/api/vault/move", response_model=response_model("VaultMovePreview"))
def preview_vault_move(vault: dict[str, Any] = Depends(current_vault)):
    with db() as connection:
        return vault_moves.preview(connection, vault["id"])


@router.post("/api/vault/move", response_model=response_model("VaultMoveResponse"))
def move_vault(action: VaultMoveRequest, vault: dict[str, Any] = Depends(current_vault),
               user: dict[str, Any] = Depends(current_user)):
    if action.vault_id != vault["id"]:
        raise Conflict("api.vault_move.vault_changed")
    with db() as connection:
        return vault_moves.schedule(connection, vault_id=vault["id"], target=action.target_class,
            user_id=user["id"], acknowledged=action.acknowledge_costs, timestamp=now_iso())


@router.get("/api/vault/lifecycle", response_model=response_model("LifecycleResponse"))
def own_vault_lifecycle(vault: dict[str, Any] = Depends(current_vault)):
    with db() as connection:
        return _lifecycle_payload(connection, vault)


@router.put("/api/vault/lifecycle", response_model=response_model("LifecycleResponse"))
def update_vault_storage_plan(
    action: LifecycleProfileSelection,
    request: Request,
    vault: dict[str, Any] = Depends(current_vault),
    user: dict[str, Any] = Depends(current_user),
):
    profile, guided_name = _selected_lifecycle_profile(action)
    with db() as connection:
        validation = set_vault_plan(connection, vault["id"], profile)
        sync_pending = False
        try:
            sync_lifecycle_rules_for_bucket(
                connection,
                s3_client(region=vault.get("s3_region")),
                bucket=vault["s3_bucket"],
            )
        except Exception:
            # Durable plan intent is retried by the established scan sync.
            sync_pending = True
        payload = _lifecycle_payload(connection, vault)
        audit_log(
            "vault_storage_plan_updated",
            connection=connection,
            vault_id=vault["id"],
            actor=_actor(request),
            guided_profile=guided_name,
            custom_profile=guided_name is None,
        )
    return {
        "message": "Vault storage plan saved",
        "sync_pending": sync_pending,
        "warnings": list(validation.warnings),
        **payload,
    }
