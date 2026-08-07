from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .api_models import JsonObjectResponse, documented_schemas, response_model
from .branding import PRODUCT_NAME
from .config import Settings, settings, validate_settings
from .audit import audit_log
from .i18n import (
    DEFAULT_LOCALE,
    LOCALE_COOKIE_NAME,
    available_locales,
    catalog as locale_catalog,
    normalize_locale,
    present_job_message,
    resolve_locale,
    translate,
)
from .backoff import (
    BackoffError,
    guard as backoff_guard,
    reauth_account_key,
    reauth_ip_key,
    record_failure,
    record_success,
)
from .breakglass import is_break_glass_allowed
from .catalog import ArchiveCatalog, VaultFileNotFound
from .database import INTEGRITY_ERRORS, db, initialize_database
from .migrate_on_start import ensure_schema_current
from .services.source_layout import (
    get_sources_root,
    prepare_sources_layout,
    reconcile_source_volume_identities,
    source_volume_inventory,
    validate_nested_mounts_after_identity,
    vault_local_access,
)
from .services import source_areas as source_areas_service
from .services import vault_relocation as vault_relocation_service
from .services import vault_decommission as vault_decommission_service
from .oidc import OidcError, begin_login, complete_login
from .oidc_configuration import (
    OidcConfigurationConflict,
    OidcConfigurationError,
    activate_oidc_draft,
    disable_oidc,
    oidc_host_addresses,
    oidc_configuration_response,
    rotate_oidc_secret,
    save_oidc_draft,
    validate_oidc_draft,
)
from .proxy import parse_networks, resolve_client_ip
from .invites import (
    InviteError,
    create_invite,
    list_pending_invites,
    redeem_invite,
    resolve_invite,
    revoke_invite,
)
from .lookup_rate_limit import check_lookup_rate_limit
from .security import DUMMY_PASSWORD_HASH, hash_password, verify_password
from .services.vault_governance import (
    GovernanceError,
    assign_member_role,
    notify_owner_of_admin_action,
    primary_owner,
    remove_member,
    transfer_primary_ownership,
)
from .services.vault_quotas import (
    QuotaBlocked,
    QuotaLimits,
    evaluate_current_quota,
    get_limits,
    set_limits,
    usage_snapshot,
)
from .services.vault_recovery import (
    RecoveryCustodyRequired,
    RecoveryError,
    build_recovery_export,
    confirm_recovery_custody,
    export_recovery_secret,
)
from .services.vault_roles import can_operate, is_owner
from .services import audit_events as audit_event_store
from .services import health as health_service
from .services import metadata_backups as metadata_backup_service
from .services import metrics as metrics_service
from .services import notifications as notification_service
from .services import user_administration as user_admin_service
from .services import worker_errors as worker_error_store
from .services.catalog_event_stream import (
    coalesced_catchup_signal,
    iter_catalog_event_sse,
)
from .system_settings import (
    InvalidSystemSetting,
    StaleSystemSettings,
    apply_system_settings,
    effective_settings,
    effective_system_setting,
    system_settings_response,
)
from .services.restore_estimates import (
    SUPPORTED_RESTORE_TIERS,
    estimate_restore,
    is_high_impact_restore,
    normalize_restore_tier,
)
from .services.operation_policies import (
    OperationPolicy,
    get_policy,
    preview_glob_rules,
    set_policy,
)
from .services.cost_estimates import (
    PriceBook,
    activate_price_book,
    estimate_storage_month,
    get_active_price_book,
    list_price_books,
    upsert_price_book,
)
from .services.lifecycle_policies import (
    clear_folder_override,
    create_policy,
    list_vault_policies,
    load_policy_assignments,
    set_folder_override,
    set_policy_profile,
    set_vault_default_policy,
    sync_lifecycle_rules_for_bucket,
)
from .services.lifecycle_profiles import (
    GUIDED_PROFILES,
    LifecycleProfile,
    LifecycleTransition,
    guided_profile,
    validate_lifecycle_profile,
)
from .services import cloud_deletion as cloud_deletion_service
from .services.vaults import (
    InvalidVaultName,
    VaultAdoptionError,
    VaultCreationError,
    VaultProvisioningUnavailable,
    VaultSlugTaken,
    create_admin_vault,
    create_vault_for_user,
    list_admin_vaults,
)
from .sessions import (
    SessionTransitionError,
    create_session,
    csrf_token_for,
    current_offline_cache_generation,
    is_reauth_recent,
    mark_reauthenticated,
    resolve_session,
    revoke_session,
    rotate_session,
    set_session_vault,
)
from .services.fs_preflight import build_stats_filesystem_payload
from .services.storage_classes import (
    cold_class_warning,
    list_storage_class_options,
    normalize_storage_class,
    source_requires_restore_for_class_change,
    validate_manual_target_class,
)
from .storage import (
    background_loop,
    cancel_jobs,
    cleanup_abandoned_restore_files,
    filesystem_watch_loop,
    now_iso,
    reconcile_interrupted_jobs,
    runtime_status,
    safe_local_path,
    scan_lock_for_vault,
    snapshot_runtime_status_for_stats,
    status_lock,
    safe_relative_path,
    InvalidLogicalPath,
    scan_vault,
    s3_client,
    storage_class_requires_restore,
)


def _spa_dist_dir() -> Path:
    return Path(settings.frontend_dist_dir)


def _spa_index_response() -> FileResponse:
    index_path = _spa_dist_dir() / "index.html"
    if not index_path.is_file():
        raise HTTPException(
            status_code=503,
            detail=(
                "frontend/dist/index.html is missing. "
                "Build the SPA with: cd frontend && npm ci && npm run build"
            ),
        )
    return FileResponse(
        index_path,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_settings()
    prepare_sources_layout()
    ensure_schema_current()
    initialize_database()
    # Identity must be reconciled before scans, workers, or watchers start.
    reconcile_source_volume_identities()
    validate_nested_mounts_after_identity()
    # Legacy healthy roots are enrolled before any watcher or Job can touch
    # them. A root already missing at upgrade stays ambiguous and cannot be
    # rebound by the relocation workflow.
    vault_relocation_service.reconcile_vault_root_identities()
    cleanup_abandoned_restore_files()
    reconcile_interrupted_jobs()
    with db() as connection:
        vault_decommission_service.reconcile_interrupted_jobs(connection)
    runtime = _runtime_settings()
    vault_decommission_service.reconcile_all(
        local_delete_enabled=runtime.allow_local_delete,
        purge_delay_seconds=runtime.cloud_purge_delay_seconds,
    )
    tasks = [asyncio.create_task(background_loop())]
    if settings.filesystem_watch_enabled:
        tasks.append(asyncio.create_task(filesystem_watch_loop()))
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title=PRODUCT_NAME, lifespan=lifespan)


def _openapi_schema() -> dict[str, Any]:
    if app.openapi_schema is None:
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        for name, documented in documented_schemas().items():
            existing = components.get(name)
            # Never replace a canonical inbound Pydantic contract. Named
            # pass-through response components are identifiable by their open
            # object schema and are enriched with the documented field shape.
            if existing is None or existing.get("additionalProperties") is True:
                components[name] = documented
        app.openapi_schema = schema
    return app.openapi_schema


app.openapi = _openapi_schema


# Deliberate API error contracts for domain validation. Keep these separate
# from FastAPI's generic validation response so unrelated programming errors
# still surface as 500s.
LOGICAL_PATH_ERROR_RESPONSES = {
    422: {
        "description": "The logical path is empty, absolute, or traverses outside the Vault",
        "content": {
            "application/json": {
                "schema": {
                    "$ref": "#/components/schemas/ApiErrorResponse"
                }
            }
        },
    }
}
SCOPED_NOT_FOUND_RESPONSES = {
    404: {
        "description": "The requested Vault-scoped resource is absent, stale, or foreign",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["detail"],
                    "properties": {"detail": {"type": "string"}},
                    "additionalProperties": False,
                }
            }
        },
    }
}


def _request_locale(request: Any | None = None) -> str:
    if request is None or not hasattr(request, "cookies"):
        return DEFAULT_LOCALE
    return resolve_locale(
        cookie_value=request.cookies.get(LOCALE_COOKIE_NAME),
        accept_language=(
            request.headers.get("accept-language")
            if hasattr(request, "headers")
            else None
        ),
    )


def _set_locale_cookie(response: Response, locale: str) -> None:
    # Write only allowlisted constants — never echo raw client locale text.
    resolved = normalize_locale(locale)
    cookie_value = "it" if resolved == "it" else "en"
    response.set_cookie(
        LOCALE_COOKIE_NAME,
        cookie_value,
        max_age=365 * 24 * 60 * 60,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _api_message(request: Request, key: str, **params: Any) -> dict[str, Any]:
    locale = _request_locale(request)
    return {
        "message": translate(key, locale=locale, **params),
        "message_key": key,
    }


SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
# Login has no session yet, so it cannot carry a synchronizer token; it is
# protected by the strict Origin check plus break-glass network gating.
CSRF_EXEMPT_PATHS = {"/api/login"}


def _trusted_proxies() -> list[Any]:
    try:
        return parse_networks(settings.trusted_proxies)
    except ValueError:
        return []


def _client_ip(request: Request) -> str | None:
    peer = request.client.host if request.client else None
    return resolve_client_ip(
        peer=peer,
        forwarded_for=request.headers.get("x-forwarded-for"),
        trusted_proxies=_trusted_proxies(),
    )


def _allowed_hosts() -> list[str]:
    return [host.strip() for host in settings.allowed_hosts.split(",") if host.strip()]


def _origin_allowed(origin: str) -> bool:
    parsed = urlsplit(origin)
    if not parsed.hostname:
        return False
    if parsed.hostname not in _allowed_hosts():
        return False
    if settings.cookie_secure and parsed.scheme != "https":
        return False
    return True


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    allowed_hosts = _allowed_hosts()
    if allowed_hosts:
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in allowed_hosts:
            return JSONResponse({"detail": "Host not allowed"}, status_code=400)
    if request.method not in SAFE_METHODS:
        origin = request.headers.get("origin")
        if origin is not None and allowed_hosts and not _origin_allowed(origin):
            return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
        if request.url.path not in CSRF_EXEMPT_PATHS:
            token = _read_session_cookie(request)
            if token:
                with db() as connection:
                    expected = csrf_token_for(connection, token)
                if expected is not None:
                    header = request.headers.get("x-csrf-token") or ""
                    if not secrets.compare_digest(header, expected):
                        return JSONResponse(
                            {"detail": "CSRF token missing or invalid"},
                            status_code=403,
                        )
    return await call_next(request)


def _read_session_cookie(request: Request) -> str | None:
    return request.cookies.get(settings.session_cookie_name)


def _set_session_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        raw_token,
        max_age=settings.session_absolute_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        settings.session_cookie_name,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    response.delete_cookie(
        settings.csrf_cookie_name,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _set_csrf_cookie(response: Response, csrf_token: str) -> None:
    # Readable by JavaScript on purpose: the frontend echoes it back in the
    # X-CSRF-Token header (synchronizer token pattern).
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf_token,
        max_age=settings.session_absolute_seconds,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER = "X-FrostVault-Offline-Cache-Authorization"
OFFLINE_FILE_CACHE_AUTHORIZATION_DESCRIPTION = (
    "Optional opaque cache authorization from GET /api/me. A bundled PWA sends "
    "it only for a current offline-cache lease. If the persisted Session "
    "authorization changed because of logout, expiry, OIDC token rotation, or "
    "Vault selection, this endpoint returns 409 and the value must not be reused."
)
OFFLINE_FILE_CACHE_FILES_RESPONSES = {
    200: {
        "description": "File listing with the persisted cache authorization used to validate it.",
        "headers": {
            OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER: {
                "description": (
                    "The current opaque persisted cache authorization. Workbox may "
                    "cache a 200 response only when it exactly matches the request "
                    "header and the active /api/me authorization."
                ),
                "schema": {"type": "string", "minLength": 1},
            },
            "Vary": {
                "description": (
                    "Includes X-FrostVault-Offline-Cache-Authorization so an "
                    "intermediary cache cannot reuse a listing across authorizations."
                ),
                "schema": {"type": "string"},
            },
        },
    },
    409: {
        "description": (
            "The supplied cache authorization is stale, or the Session/Vault "
            "changed while the listing was built. Discard the payload and fetch "
            "fresh /api/me authority before retrying."
        ),
        "headers": {
            "Cache-Control": {
                "description": "Always no-store; a stale authorization conflict is never reusable.",
                "schema": {"type": "string", "enum": ["no-store"]},
            }
        },
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["detail"],
                    "properties": {
                        "detail": {
                            "type": "string",
                            "enum": ["Offline cache authorization changed"],
                        }
                    },
                }
            }
        },
    },
}


def _offline_cache_authorization_changed() -> HTTPException:
    """Return the non-cacheable conflict used by guarded file listings."""
    return HTTPException(
        409,
        "Offline cache authorization changed",
        headers={"Cache-Control": "no-store"},
    )


def current_user(request: Request) -> dict[str, Any]:
    token = _read_session_cookie(request)
    if not token:
        raise HTTPException(401, "Authentication required")
    with db() as connection:
        session = resolve_session(connection, token)
    if not session:
        raise HTTPException(401, "Invalid session")
    request.state.session = session
    return session["user"]


def _offline_file_cache_generation(request: Request, vault_id: int | None) -> str:
    """Read the live persisted cache authorization for this Session/Vault."""
    with db() as connection:
        generation = current_offline_cache_generation(
            connection,
            request.state.session["id"],
            vault_id,
        )
    if not generation:
        raise HTTPException(401, "Invalid session")
    return generation


def _validate_offline_file_cache_generation(
    request: Request,
    vault_id: int,
    supplied: str | None = None,
) -> str:
    """Validate the supplied header against the persisted Session generation.

    The header is optional for ordinary API consumers. When the bundled PWA
    supplies it, this lookup deliberately bypasses ``request.state.session``:
    a separate process may have committed logout, expiry, OIDC token rotation,
    or Vault selection after authentication dependency resolution.
    """
    with db() as connection:
        expected = current_offline_cache_generation(
            connection,
            request.state.session["id"],
            vault_id,
        )
        # A concrete Vault mismatch is a 409 transition, not an expired or
        # revoked credential. Querying without the Vault constraint separates
        # those two cases without trusting the process-local request snapshot.
        session_generation = (
            current_offline_cache_generation(
                connection,
                request.state.session["id"],
                None,
            )
            if expected is None
            else expected
        )
    if expected is None:
        if session_generation is None:
            raise HTTPException(401, "Invalid session")
        raise _offline_cache_authorization_changed()
    if supplied is None:
        supplied = request.headers.get(OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER)
    if supplied and not secrets.compare_digest(supplied, expected):
        raise _offline_cache_authorization_changed()
    return expected


def _offline_file_cache_generation_is_current(
    request: Request,
    vault_id: int,
    expected: str,
) -> bool:
    with db() as connection:
        current = current_offline_cache_generation(
            connection,
            request.state.session["id"],
            vault_id,
        )
    return bool(current and secrets.compare_digest(current, expected))


def _set_offline_file_cache_generation(response: Response, generation: str) -> None:
    response.headers[OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER] = generation
    # CacheStorage is separated by generation as well, but this makes an
    # accidental HTTP cache key conservative for non-Workbox clients too.
    vary = [
        value.strip()
        for value in response.headers.get("Vary", "").split(",")
        if value.strip()
    ]
    if OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER not in vary:
        vary.append(OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER)
    response.headers["Vary"] = ", ".join(vary)


def admin_user(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if not user["is_admin"]:
        raise HTTPException(403, "Administrator access required")
    return user


def _runtime_settings():
    if not isinstance(settings, Settings):
        return effective_settings(None, settings_obj=settings)
    with db() as connection:
        return effective_settings(connection, settings_obj=settings)


class ReauthRequired(HTTPException):
    """Signals the frontend that a fresh Reauthentication is needed."""

    def __init__(self) -> None:
        super().__init__(status_code=403, detail="reauth_required")


@app.exception_handler(ReauthRequired)
async def _reauth_required_handler(_: Request, __: ReauthRequired) -> JSONResponse:
    # Stable marker the frontend keys on to trigger a step-up.
    return JSONResponse({"error": "reauth_required"}, status_code=403)


@app.exception_handler(InvalidLogicalPath)
async def _invalid_logical_path_handler(
    request: Request, error: InvalidLogicalPath
) -> JSONResponse:
    message = translate(error.message_key, locale=_request_locale(request))
    return JSONResponse(
        {
            "detail": message,
            "message_key": error.message_key,
            "message": message,
        },
        status_code=422,
    )


@app.exception_handler(InvalidSystemSetting)
async def _invalid_system_setting_handler(
    _: Request, error: InvalidSystemSetting
) -> JSONResponse:
    return JSONResponse({"detail": str(error)}, status_code=422)


@app.exception_handler(StaleSystemSettings)
async def _stale_system_settings_handler(
    _: Request, error: StaleSystemSettings
) -> JSONResponse:
    return JSONResponse(
        {
            "error": "stale_system_settings",
            "current_revision": error.current_revision,
        },
        status_code=409,
    )


@app.exception_handler(QuotaBlocked)
async def _quota_blocked_handler(_: Request, error: QuotaBlocked) -> JSONResponse:
    # Hard quota decisions are a distinct machine-readable contract. The
    # transaction that raised this exception is rolled back, so no job can be
    # partially admitted.
    return JSONResponse(
        {"error": "quota_blocked", "quota": error.evaluation.as_dict()},
        status_code=409,
    )


@app.exception_handler(RecoveryCustodyRequired)
async def _recovery_custody_handler(
    _: Request, error: RecoveryCustodyRequired
) -> JSONResponse:
    return JSONResponse(
        {"error": "recovery_custody_required", "detail": str(error)},
        status_code=409,
    )


def require_recent_reauth(
    request: Request, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    session = request.state.session
    with db() as connection:
        reauth_window_seconds = effective_system_setting(
            connection,
            "reauth_window_seconds",
            settings_obj=settings,
        )
    if not is_reauth_recent(
        session.get("reauth_at"),
        now=datetime.now(timezone.utc),
        window_seconds=reauth_window_seconds,
    ):
        raise ReauthRequired()
    return user


_GOVERNANCE_ERROR_STATUS: dict[str, tuple[int, str]] = {
    "invalid_role": (422, "Invalid role"),
    "vault_not_found": (404, "Vault not found"),
    "user_not_found": (404, "User not found"),
    "member_not_found": (404, "Vault member not found"),
    "owner_required": (400, "The vault must retain a primary owner; transfer ownership first"),
    "no_current_owner": (409, "Vault has no primary owner to transfer from"),
    "already_owner": (400, "That user is already the primary owner"),
    "ownership_changed": (409, "Vault ownership changed; refresh and try again"),
    "vault_quiesced": (409, "Vault is quiesced for decommission"),
}


def _governance_http_error(exc: GovernanceError) -> HTTPException:
    status_code, message = _GOVERNANCE_ERROR_STATUS.get(
        exc.reason, (400, "Request could not be completed")
    )
    return HTTPException(status_code, message)


_ADMINISTRATION_ERROR_STATUS: dict[str, tuple[int, str]] = {
    "self_deactivation": (400, "You cannot deactivate your own account"),
    "self_demotion": (400, "You cannot remove your own administrator role"),
    "last_admin": (400, "At least one administrator must remain active"),
    "no_changes": (400, "No changes requested"),
    "not_found": (404, "User not found"),
    "identity_not_found": (404, "Linked identity not found"),
    "confirmation_required": (
        400,
        "Confirm the unlink explicitly before removing a linked identity",
    ),
    "would_lock_out": (
        409,
        "Removing this identity would leave the user without any way to sign in",
    ),
}


def _administration_http_error(
    exc: user_admin_service.AdministrationError,
) -> HTTPException:
    status_code, message = _ADMINISTRATION_ERROR_STATUS.get(
        exc.reason, (400, "Request could not be completed")
    )
    return HTTPException(status_code, message)


_INVITE_REVOCATION_ERROR_STATUS: dict[str, tuple[int, str]] = {
    "unknown": (404, "Invite not found"),
    "already_redeemed": (409, "Invite has already been redeemed"),
    "already_revoked": (409, "Invite has already been revoked"),
}


def _invite_revocation_http_error(exc: InviteError) -> HTTPException:
    status_code, message = _INVITE_REVOCATION_ERROR_STATUS.get(
        exc.reason, (400, "Invite could not be revoked")
    )
    return HTTPException(status_code, message)


def current_vault(
    request: Request, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    session = request.state.session
    requested_id = session.get("vault_id")
    with db() as connection:
        vault = None
        if requested_id:
            vault = connection.execute(
                """
                SELECT v.*, vm.role, vm.user_id AS member_user_id
                FROM vaults v
                JOIN vault_members vm ON vm.vault_id=v.id
                WHERE v.id=%s AND vm.user_id=%s AND v.enabled=TRUE
                  AND v.decommission_state='active'
                """,
                (requested_id, user["id"]),
            ).fetchone()
        if not vault:
            vault = connection.execute(
                """
                SELECT v.*, vm.role, vm.user_id AS member_user_id
                FROM vaults v
                JOIN vault_members vm ON vm.vault_id=v.id
                WHERE vm.user_id=%s AND v.enabled=TRUE
                  AND v.decommission_state='active'
                ORDER BY v.name
                LIMIT 1
                """,
                (user["id"],),
            ).fetchone()
        if not vault:
            raise HTTPException(403, "No vault is assigned to this user")
        if session.get("vault_id") != vault["id"]:
            updated = set_session_vault(
                connection,
                session["id"],
                vault["id"],
                expected_generation=session["offline_cache_generation"],
                expected_nonce=session["offline_cache_nonce"],
            )
            if not updated:
                # A concurrent logout, OIDC rotation, expiry, or Vault choice
                # won after current_user resolved this request. Do not let this
                # older process overwrite the newer persisted transition.
                raise _offline_cache_authorization_changed()
            session.update(updated)
    return vault


def owner_vault(vault: dict[str, Any] = Depends(current_vault)) -> dict[str, Any]:
    """Resolve the selected vault only for its primary owner.

    This is the owner self-service seam: operators and viewers cannot discover
    or mutate sharing through the owner routes, even when they are global
    administrators. Administrator overrides remain under ``/api/admin``.
    """
    if not is_owner(vault["role"]):
        raise HTTPException(403, "Only the vault owner can manage sharing")
    return vault


def _owned_vault_for_decommission(vault_id: int, user_id: int) -> dict[str, Any]:
    with db() as connection:
        vault = connection.execute(
            """
            SELECT v.*, vm.role, vm.user_id AS member_user_id
            FROM vaults v
            JOIN vault_members vm ON vm.vault_id=v.id
            WHERE v.id=%s AND vm.user_id=%s AND vm.role='owner'
            """,
            (vault_id, user_id),
        ).fetchone()
    if vault is None:
        raise HTTPException(403, "Only the primary owner can decommission this Vault")
    return vault


def decommission_owner_vault(
    request: Request, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    """Resolve an owned Vault even after it is quiesced or disabled.

    Normal ``current_vault`` deliberately hides non-operational Vaults.  This
    narrow seam keeps decommission progress visible to the primary owner while
    membership/history remains preserved in the tombstone.
    """
    requested_id = request.state.session.get("vault_id")
    with db() as connection:
        vault = None
        if requested_id:
            vault = connection.execute(
                """
                SELECT v.*, vm.role, vm.user_id AS member_user_id
                FROM vaults v
                JOIN vault_members vm ON vm.vault_id=v.id
                WHERE v.id=%s AND vm.user_id=%s AND vm.role='owner'
                """,
                (requested_id, user["id"]),
            ).fetchone()
        if vault is None:
            vault = connection.execute(
                """
                SELECT v.*, vm.role, vm.user_id AS member_user_id
                FROM vaults v
                JOIN vault_members vm ON vm.vault_id=v.id
                WHERE vm.user_id=%s AND vm.role='owner'
                ORDER BY CASE WHEN v.decommission_state='decommissioning' THEN 0 ELSE 1 END,
                         lower(v.name)
                LIMIT 1
                """,
                (user["id"],),
            ).fetchone()
    if vault is None:
        raise HTTPException(403, "Only the primary owner can decommission this Vault")
    return vault


class LoginRequest(BaseModel):
    username: str
    password: str


class LocaleUpdate(BaseModel):
    locale: str


class ReauthRequest(BaseModel):
    password: str


class OidcDraftAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issuer: str = Field(min_length=1, max_length=2048)
    client_id: str = Field(min_length=1, max_length=512)
    client_secret: str = Field(min_length=1, max_length=8192)
    scopes: list[str] = Field(min_length=1, max_length=32)
    login_transaction_ttl_seconds: int = Field(ge=60, le=3600)

    @model_validator(mode="after")
    def validate_oidc_values(self) -> "OidcDraftAction":
        self.issuer = self.issuer.strip()
        self.client_id = self.client_id.strip()
        normalized_scopes = list(
            dict.fromkeys(scope.strip() for scope in self.scopes)
        )
        if not self.issuer or not self.client_id:
            raise ValueError("issuer and client_id must not be blank")
        if any(
            not scope
            or len(scope) > 128
            or any(character.isspace() for character in scope)
            for scope in normalized_scopes
        ):
            raise ValueError("scopes must contain valid OAuth scope tokens")
        if "openid" not in normalized_scopes:
            raise ValueError("scopes must include openid")
        self.scopes = normalized_scopes
        return self


class OidcSecretRotationAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_secret: str = Field(min_length=1, max_length=8192)


class VaultSelection(BaseModel):
    vault_id: int


class VaultSelfServiceCreate(BaseModel):
    """Self-service vault creation payload (issues #7, #6, and #150).

    Labels and encryption_mode are always accepted. Adoption adds a constrained
    ``volume_alias`` + ``relative_path`` pair — never an absolute filesystem
    path, S3 identity, rclone remote, or crypt secret.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    slug: str | None = Field(default=None, min_length=2, max_length=60)
    encryption_mode: str = Field(default="plain", pattern="^(plain|crypt)$")
    creation_mode: str = Field(default="empty", pattern="^(empty|adopt)$")
    volume_alias: str | None = Field(default=None, min_length=1, max_length=120)
    relative_path: str | None = Field(default=None, max_length=1024)


class RecoveryConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    acknowledged: bool = True


class RecoveryExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)


class FileAction(BaseModel):
    path: str
    is_directory: bool = False
    archive_version_id: str | None = None
    restore_tier: str | None = None
    restore_days: int | None = Field(default=None, ge=1, le=30)


class RecoverEstimateRequest(BaseModel):
    path: str
    archive_version_id: str | None = None
    restore_tier: str | None = None
    restore_days: int | None = Field(default=None, ge=1, le=30)


class RecoverApproveAction(BaseModel):
    group_id: str = Field(min_length=1, max_length=64)


class ConfirmRenameAction(BaseModel):
    vault_file_id: str = Field(min_length=36, max_length=36)
    new_path: str = Field(max_length=1024)


class ConfirmFolderRenameAction(BaseModel):
    old_prefix: str = Field(max_length=1024)
    new_prefix: str = Field(max_length=1024)


class GroupCancelAction(BaseModel):
    group_id: str = Field(min_length=1, max_length=64)


class JobCancelAction(GroupCancelAction):
    action: str = Field(min_length=1, max_length=20)


class CloudDeletionSettingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class CloudDeletionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    is_directory: bool = False
    paths: list[str] | None = None


class CloudArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    is_directory: bool = False
    paths: list[str] | None = None


class CloudPurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    is_directory: bool = False
    paths: list[str] | None = None
    confirmation: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)
    generated_phrase: str = Field(min_length=1, max_length=200)


class StorageClassChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(default="", max_length=1024)
    is_directory: bool = False
    whole_vault: bool = False
    target_storage_class: str = Field(min_length=1, max_length=32)
    archive_version_id: str | None = None
    restore_tier: str | None = None
    restore_days: int | None = Field(default=None, ge=1, le=30)
    pin_after: bool = False


class LifecyclePinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(max_length=1024)
    is_directory: bool = False
    pinned: bool


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    display_name: str = Field(min_length=1, max_length=120)
    password: str | None = Field(default=None, min_length=12, max_length=200)
    is_admin: bool = False


class InviteCreate(BaseModel):
    target_user_id: int


class UserUpdate(BaseModel):
    active: bool | None = None
    is_admin: bool | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    password: str | None = Field(default=None, min_length=12, max_length=200)


class VaultCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=2, max_length=60)
    owner_user_id: int
    reason: str = Field(min_length=3, max_length=500)
    encryption_mode: str = Field(default="plain", pattern="^(plain|crypt)$")
    creation_mode: str = Field(default="empty", pattern="^(empty|adopt)$")
    volume_alias: str | None = Field(default=None, min_length=1, max_length=120)
    relative_path: str | None = Field(default=None, max_length=1024)


class VaultRelocate(BaseModel):
    """Constrained destination within the Vault's existing Source Volume."""

    model_config = ConfigDict(extra="forbid")

    volume_alias: str = Field(min_length=1, max_length=120)
    relative_path: str = Field(default="", max_length=1024)
    reason: str = Field(min_length=3, max_length=500)


class VaultDecommissionPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_disposition: str = Field(pattern="^(retain|remove)$")
    cloud_disposition: str = Field(pattern="^(retain|purge)$")


class VaultDecommissionStart(VaultDecommissionPreview):
    confirmation: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=3, max_length=500)
    preview_fingerprint: str = Field(min_length=64, max_length=64)


class MembershipCreate(BaseModel):
    user_id: int
    role: str = "viewer"


class UserLookup(BaseModel):
    username: str = Field(min_length=2, max_length=80)


class AdminMembershipCreate(MembershipCreate):
    # Required for every global-admin override of a vault's sharing, per
    # ADR-0005's reauth-then-audit precedent for sensitive actions.
    reason: str = Field(min_length=3, max_length=500)


class AdminSourceAreaAssign(BaseModel):
    """Admin assignment of an exclusive Source Area (issue #149)."""

    model_config = ConfigDict(extra="forbid")

    user_id: int
    volume_alias: str = Field(min_length=1, max_length=120)
    relative_path: str = Field(default="", max_length=1024)
    reason: str = Field(min_length=3, max_length=500)


class OwnerTransfer(BaseModel):
    new_owner_user_id: int


class AdminOwnerTransfer(OwnerTransfer):
    reason: str = Field(min_length=3, max_length=500)


class VaultQuotaUpdate(BaseModel):
    """Global-admin quota replacement; omitted limits mean unlimited."""

    model_config = ConfigDict(extra="forbid")

    storage_soft_limit_bytes: int | None = Field(default=None, ge=0)
    storage_hard_limit_bytes: int | None = Field(default=None, ge=0)
    concurrency_soft_limit: int | None = Field(default=None, ge=0)
    concurrency_hard_limit: int | None = Field(default=None, ge=0)
    restore_30d_soft_limit_bytes: int | None = Field(default=None, ge=0)
    restore_30d_hard_limit_bytes: int | None = Field(default=None, ge=0)
    reason: str = Field(min_length=3, max_length=500)

    @model_validator(mode="after")
    def validate_order(self) -> "VaultQuotaUpdate":
        for soft, hard in (
            (self.storage_soft_limit_bytes, self.storage_hard_limit_bytes),
            (self.concurrency_soft_limit, self.concurrency_hard_limit),
            (
                self.restore_30d_soft_limit_bytes,
                self.restore_30d_hard_limit_bytes,
            ),
        ):
            if soft is not None and hard is not None and soft > hard:
                raise ValueError(
                    "soft quota limits must be less than or equal to hard limits"
                )
        return self


class SystemSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    overrides: dict[str, Any] = Field(default_factory=dict)
    removals: list[str] = Field(default_factory=list)


class LifecycleTransitionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: int
    storage_class: str = Field(min_length=1, max_length=40)


class LifecycleProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transitions: list[LifecycleTransitionUpdate] = Field(default_factory=list)
    expiration_days: int | None = None
    noncurrent_expiration_days: int | None = None
    noncurrent_transitions: list[LifecycleTransitionUpdate] = Field(
        default_factory=list
    )

    def to_profile(self) -> LifecycleProfile:
        return LifecycleProfile(
            transitions=tuple(
                LifecycleTransition(
                    days=item.days,
                    storage_class=item.storage_class.upper(),
                )
                for item in self.transitions
            ),
            expiration_days=self.expiration_days,
            noncurrent_expiration_days=self.noncurrent_expiration_days,
            noncurrent_transitions=tuple(
                LifecycleTransition(
                    days=item.days,
                    storage_class=item.storage_class.upper(),
                )
                for item in self.noncurrent_transitions
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


class LifecycleDefaultUpdate(LifecycleProfileSelection):
    name: str = Field(default="Vault default", min_length=1, max_length=120)


class LifecycleFolderOverrideUpdate(LifecycleProfileSelection):
    folder_path: str = Field(min_length=1, max_length=500)
    name: str | None = Field(default=None, min_length=1, max_length=120)


class LifecycleFolderOverrideDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder_path: str = Field(min_length=1, max_length=500)


class OperatingWindowModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weekday: int = Field(ge=0, le=6)
    start: str = Field(min_length=4, max_length=5)
    end: str = Field(min_length=4, max_length=5)


class OperationPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_upload: bool = False
    auto_local_cleanup: bool = False
    local_retention_days: int | None = Field(default=None, ge=1)
    stability_seconds: int = Field(default=300, ge=0)
    include_globs: list[str] = Field(default_factory=list)
    exclude_globs: list[str] = Field(default_factory=list)
    bandwidth_limit_kibps: int | None = Field(default=None, ge=0)
    operating_windows: list[OperatingWindowModel] = Field(default_factory=list)


class GlobPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[str] = Field(default_factory=list)
    include_globs: list[str] = Field(default_factory=list)
    exclude_globs: list[str] = Field(default_factory=list)


class CostPriceBookCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    currency: str = Field(default="EUR", min_length=3, max_length=8)
    effective_at: str = Field(min_length=10, max_length=64)
    assumptions: dict[str, Any] = Field(default_factory=dict)
    storage_rates: dict[str, float] = Field(default_factory=dict)
    restore_rates: dict[str, dict[str, float]] = Field(default_factory=dict)
    reason: str = Field(min_length=3, max_length=500)


class CostPriceBookActivate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)


class StorageEstimateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    size_bytes: int = Field(ge=0)
    storage_class: str = Field(default="STANDARD", min_length=1, max_length=64)


def normalize_directory(value: str, *, api: bool = False) -> str:
    """Return a safe catalog directory (the root is an empty string).

    The default preserves the direct helper's historical ``HTTPException``
    seam; routed API calls opt into the shared logical-path contract.
    """
    if not value:
        return ""
    try:
        return safe_relative_path(value).as_posix().rstrip("/")
    except InvalidLogicalPath as exc:
        if api:
            raise
        raise HTTPException(422, "Invalid folder") from exc


def build_directory_items(
    rows: list[dict[str, Any]], directory: str, state_filter: str = ""
) -> list[dict[str, Any]]:
    """Build one file-manager level from the flat file catalog."""
    prefix = f"{directory}/" if directory else ""
    folders: dict[str, dict[str, Any]] = {}
    files: list[dict[str, Any]] = []

    for row in rows:
        relative = row["path"][len(prefix):]
        folder_name, separator, _ = relative.partition("/")
        if separator:
            folder = folders.setdefault(
                folder_name,
                {
                    "item_count": 0,
                    "total_size": 0,
                    "local_size": 0,
                    "cloud_size": 0,
                    "state_counts": {},
                    "action_counts": {
                        "upload": 0,
                        "recover": 0,
                        "free-space": 0,
                        "cloud-archive": 0,
                        "cloud-purge": 0,
                        "storage-class": 0,
                    },
                    "storage_classes": set(),
                    "pinned_count": 0,
                    "matches_filter": False,
                },
            )
            folder["item_count"] += 1
            local_size = row.get("local_size")
            cloud_size = row.get("cloud_size")
            folder["total_size"] += local_size if local_size is not None else (cloud_size or 0)
            folder["local_size"] += local_size or 0
            folder["cloud_size"] += cloud_size or 0
            folder["state_counts"][row["state"]] = folder["state_counts"].get(row["state"], 0) + 1
            # Cloud deletion eligibility mirrors fileHasCloudContent in the SPA:
            # any Vault File with a non-purged Archive Version under this prefix.
            has_cloud = bool(
                row.get("cloud_exists")
                or row["state"] in {"both", "cloud_only", "restoring"}
            )
            action_flags = {
                "upload": row.get("upload_eligible", row["state"] == "local_only"),
                "recover": row.get("recover_eligible", row["state"] == "cloud_only"),
                "free-space": row.get("cleanup_eligible", row["state"] == "both"),
                "cloud-archive": has_cloud,
                "cloud-purge": has_cloud,
                "storage-class": row.get("storage_class_eligible", has_cloud),
            }
            for action, eligible in action_flags.items():
                if eligible:
                    folder["action_counts"][action] += 1
            if row.get("lifecycle_pinned"):
                folder["pinned_count"] += 1
            if row.get("storage_class"):
                folder["storage_classes"].add(row["storage_class"])
            if not state_filter or row["state"] == state_filter:
                folder["matches_filter"] = True
            continue
        if not state_filter or row["state"] == state_filter:
            files.append({**row, "type": "file", "name": relative})

    folder_items = []
    for name, aggregate in folders.items():
        if not aggregate["matches_filter"]:
            continue
        states = aggregate["state_counts"]
        state = next(iter(states)) if len(states) == 1 else "mixed"
        storage_classes = sorted(aggregate["storage_classes"])
        pinned_count = int(aggregate.get("pinned_count") or 0)
        folder_items.append({
            "type": "directory",
            "name": name,
            "path": f"{prefix}{name}",
            "item_count": aggregate["item_count"],
            "total_size": aggregate["total_size"],
            "local_size": aggregate["local_size"],
            "cloud_size": aggregate["cloud_size"],
            "state": state,
            "state_counts": states,
            "storage_class": storage_classes[0] if len(storage_classes) == 1 else None,
            "storage_class_count": len(storage_classes),
            "available_actions": aggregate["action_counts"],
            "lifecycle_pinned": pinned_count == aggregate["item_count"] and pinned_count > 0,
            "lifecycle_pinned_partial": (
                pinned_count > 0 and pinned_count < aggregate["item_count"]
            ),
        })
    folder_items.sort(key=lambda item: item["name"].casefold())
    files.sort(key=lambda item: item["name"].casefold())
    return [*folder_items, *files]


@app.get("/login")
def login_page():
    return _spa_index_response()


@app.post("/api/login", response_model=JsonObjectResponse)
def login(action: LoginRequest, request: Request, response: Response):
    client_ip = _client_ip(request)
    username = action.username.strip().lower()
    if not is_break_glass_allowed(client_ip):
        with db() as connection:
            audit_log(
                "break_glass_denied",
                connection=connection,
                ip=client_ip,
                username=username,
            )
        raise HTTPException(403, "Break-glass login is not allowed from this network")
    # Persist throttle counters by letting the transaction commit first, then map
    # the outcome to a response: raising inside the `with` block would roll the
    # recorded failure back.
    retry_after: int | None = None
    raw_token: str | None = None
    csrf_token: str | None = None
    with db() as connection:
        try:
            backoff_guard(connection, scope="ip", key=client_ip)
            backoff_guard(connection, scope="account", key=username)
        except BackoffError as blocked:
            retry_after = blocked.retry_after
        else:
            user = connection.execute(
                "SELECT * FROM users WHERE lower(username)=lower(%s)",
                (username,),
            ).fetchone()
            eligible = bool(user and user["active"] and user["password_hash"])
            password_hash = (
                user["password_hash"] if eligible else DUMMY_PASSWORD_HASH
            )
            password_valid = verify_password(password_hash, action.password)
            # Always reject the dummy path, even if its hash ever matches the
            # submitted password (or verification is replaced in a test).
            if not eligible or not password_valid:
                record_failure(connection, scope="ip", key=client_ip)
                record_failure(connection, scope="account", key=username)
                audit_log(
                    "break_glass_failed",
                    connection=connection,
                    ip=client_ip,
                    username=username,
                )
            else:
                record_success(connection, scope="ip", key=client_ip)
                record_success(connection, scope="account", key=username)
                raw_token = create_session(
                    connection,
                    user_id=user["id"],
                    auth_method="local",
                    ip=client_ip,
                    user_agent=request.headers.get("user-agent"),
                )
                csrf_token = csrf_token_for(connection, raw_token)
    if retry_after is not None:
        with db() as connection:
            audit_log(
                "auth_backoff_blocked",
                connection=connection,
                flow="break_glass",
                ip=client_ip,
                username=username,
                retry_after=retry_after,
            )
        raise HTTPException(
            429,
            "Too many attempts; try again later",
            headers={"Retry-After": str(retry_after)},
        )
    if raw_token is None:
        raise HTTPException(401, "Incorrect username or password")
    _set_session_cookie(response, raw_token)
    if csrf_token:
        _set_csrf_cookie(response, csrf_token)
    return {**_api_message(request, "api.signed_in")}


@app.post("/api/logout", response_model=JsonObjectResponse)
def logout(request: Request, response: Response):
    token = _read_session_cookie(request)
    if token:
        with db() as connection:
            session = resolve_session(connection, token)
            if session:
                revoke_session(connection, session["id"])
    _clear_session_cookie(response)
    return {**_api_message(request, "api.signed_out")}


@app.post("/api/reauth", response_model=JsonObjectResponse)
def reauth(
    action: ReauthRequest,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
):
    """Local-password Reauthentication with an isolated durable backoff.

    OIDC users have no password hash and must step up through the provider
    (see ``/auth/oidc/reauth``). Its account/IP counters deliberately use
    namespaced keys so a Reauthentication success cannot reset Local Sign-in,
    Invite, or OIDC-related throttling state.
    """
    client_ip = _client_ip(request)
    if not is_break_glass_allowed(client_ip):
        raise HTTPException(403, "Password reauthentication is not allowed here")

    backoff_keys = (
        ("ip", reauth_ip_key(client_ip or "unknown")),
        ("account", reauth_account_key(int(user["id"]))),
    )
    retry_after: int | None = None
    password_valid = False

    # Like Local Sign-in, every counter and audit event must commit before an
    # HTTP error is raised. Otherwise a rejected request rolls back the very
    # state intended to throttle it.
    with db() as connection:
        delays: list[int] = []
        for scope, key in backoff_keys:
            try:
                backoff_guard(connection, scope=scope, key=key)
            except BackoffError as blocked:
                delays.append(blocked.retry_after)
        retry_after = max(delays) if delays else None

        if retry_after is None:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE id=%s", (user["id"],)
            ).fetchone()
            password_valid = bool(
                row
                and row["password_hash"]
                and verify_password(row["password_hash"], action.password)
            )
            if not password_valid:
                for scope, key in backoff_keys:
                    record_failure(connection, scope=scope, key=key)
                # The attempt that reaches the threshold is itself throttled;
                # choose the longest active dimension for an accurate retry.
                delays = []
                for scope, key in backoff_keys:
                    try:
                        backoff_guard(connection, scope=scope, key=key)
                    except BackoffError as blocked:
                        delays.append(blocked.retry_after)
                retry_after = max(delays) if delays else None
                audit_log(
                    "reauth_failed",
                    connection=connection,
                    actor_user_id=user["id"],
                    outcome="failure",
                    flow="reauth",
                    ip=client_ip,
                )
            else:
                for scope, key in backoff_keys:
                    record_success(connection, scope=scope, key=key)
                mark_reauthenticated(connection, request.state.session["id"])
                audit_log(
                    "reauth_succeeded",
                    connection=connection,
                    actor_user_id=user["id"],
                    outcome="success",
                    flow="reauth",
                    ip=client_ip,
                )

        if retry_after is not None:
            audit_log(
                "auth_backoff_blocked",
                connection=connection,
                actor_user_id=user["id"],
                flow="reauth",
                ip=client_ip,
                retry_after=retry_after,
            )

    if retry_after is not None:
        raise HTTPException(
            429,
            "Too many attempts; try again later",
            headers={"Retry-After": str(retry_after)},
        )
    if not password_valid:
        raise HTTPException(401, "Incorrect password")
    return {"message": "Reauthenticated"}


@app.get("/auth/oidc/reauth")
def oidc_reauth(
    request: Request,
    return_to: str | None = Query(default=None),
    user: dict[str, Any] = Depends(current_user),
):
    """OIDC step-up: force a fresh provider login with ``prompt=login``."""
    redirect_uri = str(request.url_for("oidc_callback"))
    with db() as connection:
        try:
            authorization_url = begin_login(
                connection,
                redirect_uri=redirect_uri,
                return_to=_safe_return_to(return_to),
                prompt="login",
                http_client=_oidc_client(),
                host_addresses=_oidc_host_addresses,
            )
        except OidcError as error:
            _raise_oidc_start_error(error)
    return RedirectResponse(authorization_url, status_code=303)


def _oidc_client():
    # Seam for tests to inject a fake provider transport; production uses the
    # OIDC module's own HTTP client.
    return None


def _oidc_host_addresses(hostname: str) -> list[str]:
    return oidc_host_addresses(hostname)


def _raise_oidc_start_error(error: OidcError) -> None:
    if error.reason == "disabled":
        raise HTTPException(404, "OIDC login is not enabled") from error
    raise HTTPException(400, f"OIDC login failed: {error.reason}") from error


def _safe_return_to(candidate: str | None) -> str:
    if not candidate or not candidate.startswith("/"):
        return "/"
    # Reject protocol-relative (`//host`), backslash smuggling (`/\host`), and
    # any embedded control characters so only same-origin paths survive.
    if candidate.startswith("//") or candidate.startswith("/\\"):
        return "/"
    if any(char in candidate for char in ("\\", "\n", "\r", "\t")):
        return "/"
    return candidate


@app.get("/auth/oidc/login")
def oidc_login(
    request: Request,
    return_to: str | None = Query(default=None),
    invite: str | None = Query(default=None),
):
    redirect_uri = str(request.url_for("oidc_callback"))
    client_ip = _client_ip(request)
    # Throttle counters must survive the request, so record them and let the
    # transaction commit before turning any failure into a response.
    retry_after: int | None = None
    invite_error: str | None = None
    authorization_url: str | None = None
    with db() as connection:
        if invite is not None and client_ip:
            try:
                backoff_guard(connection, scope="ip", key=client_ip)
            except BackoffError as blocked:
                retry_after = blocked.retry_after
        if retry_after is None:
            invite_id: int | None = None
            if invite is not None:
                try:
                    invite_id = resolve_invite(connection, invite)["id"]
                except InviteError as error:
                    invite_error = error.reason
                    # Only a completely unknown token looks like a guess; a
                    # real-but-stale token must not throttle its legitimate holder.
                    if error.reason == "unknown" and client_ip:
                        record_failure(connection, scope="ip", key=client_ip)
                        audit_log(
                            "invite_guess",
                            connection=connection,
                            ip=client_ip,
                        )
                else:
                    if client_ip:
                        record_success(connection, scope="ip", key=client_ip)
            if invite_error is None:
                try:
                    authorization_url = begin_login(
                        connection,
                        redirect_uri=redirect_uri,
                        return_to=_safe_return_to(return_to),
                        invite_id=invite_id,
                        http_client=_oidc_client(),
                        host_addresses=_oidc_host_addresses,
                    )
                except OidcError as error:
                    _raise_oidc_start_error(error)
    if retry_after is not None:
        with db() as connection:
            audit_log(
                "auth_backoff_blocked",
                connection=connection,
                flow="invite_redemption",
                ip=client_ip,
                retry_after=retry_after,
            )
        raise HTTPException(
            429,
            "Too many attempts; try again later",
            headers={"Retry-After": str(retry_after)},
        )
    if invite_error is not None:
        raise HTTPException(400, f"Invite cannot be used: {invite_error}")
    return RedirectResponse(authorization_url, status_code=303)


@app.get("/auth/oidc/link")
def oidc_link(
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    redirect_uri = str(request.url_for("oidc_callback"))
    with db() as connection:
        token = create_invite(
            connection, target_user_id=user["id"], created_by=user["id"]
        )
        invite_id = _resolve_invite_id(connection, token)
        try:
            authorization_url = begin_login(
                connection,
                redirect_uri=redirect_uri,
                return_to="/",
                invite_id=invite_id,
                prompt="login",
                http_client=_oidc_client(),
                host_addresses=_oidc_host_addresses,
            )
        except OidcError as error:
            _raise_oidc_start_error(error)
    return RedirectResponse(authorization_url, status_code=303)


def _resolve_invite_id(connection: Any, token: str | None) -> int | None:
    if not token:
        return None
    try:
        return resolve_invite(connection, token)["id"]
    except InviteError as error:
        raise HTTPException(400, f"Invite cannot be used: {error.reason}")


@app.get("/auth/oidc/callback")
def oidc_callback(
    request: Request,
    response: Response,
    state: str = Query(...),
    code: str = Query(...),
):
    redirect_uri = str(request.url_for("oidc_callback"))
    with db() as connection:
        try:
            claims = complete_login(
                connection,
                state=state,
                code=code,
                redirect_uri=redirect_uri,
                http_client=_oidc_client(),
                host_addresses=_oidc_host_addresses,
            )
        except OidcError as error:
            raise HTTPException(400, f"OIDC login failed: {error.reason}")
        user_id = _resolve_or_bind_identity(connection, claims)
        existing = resolve_session(connection, _read_session_cookie(request))
        if existing and existing["user"]["id"] == user_id:
            # Step-up reauthentication: the user proved their identity again for
            # the same account, so refresh the reauth window and rotate the token
            # instead of minting a brand-new Session.
            try:
                raw_token = rotate_session(
                    connection,
                    existing["id"],
                    reauthenticated=True,
                )
            except SessionTransitionError as error:
                # A concurrent logout or expiry wins over an OIDC callback;
                # never issue a freshly rotated cookie for a dead Session.
                raise HTTPException(401, "Invalid session") from error
            csrf_token = existing["csrf_token"]
        elif existing and existing["user"]["id"] != user_id:
            # Do not silently switch the browser into another User when the
            # resolved Identity differs from the active Session (self-link /
            # invite misuse of an already-bound Identity).
            raise HTTPException(
                403,
                "This identity belongs to a different user; sign out first",
            )
        else:
            raw_token = create_session(
                connection,
                user_id=user_id,
                auth_method="oidc",
                ip=_client_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
            csrf_token = csrf_token_for(connection, raw_token)
    redirect = RedirectResponse(
        _safe_return_to(claims.return_to), status_code=303
    )
    _set_session_cookie(redirect, raw_token)
    if csrf_token:
        _set_csrf_cookie(redirect, csrf_token)
    return redirect


def _resolve_or_bind_identity(connection: Any, claims: Any) -> int:
    identity = connection.execute(
        "SELECT user_id FROM user_identities WHERE issuer=%s AND subject=%s",
        (claims.issuer, claims.subject),
    ).fetchone()
    if identity:
        return identity["user_id"]
    if claims.invite_id is None:
        raise HTTPException(403, "This identity is not linked to a user")
    try:
        return redeem_invite(
            connection,
            invite_id=claims.invite_id,
            issuer=claims.issuer,
            subject=claims.subject,
        )
    except InviteError as error:
        raise HTTPException(403, f"Invite cannot be used: {error.reason}")


@app.get("/vaults/new")
def vault_create_page():
    return _spa_index_response()


@app.get("/")
def index():
    return _spa_index_response()


@app.get("/vault/access")
def vault_access_page():
    return _spa_index_response()


@app.get("/admin")
def admin_page():
    return _spa_index_response()


@app.get("/api/me", response_model=response_model("MeResponse"))
def me(request: Request, response: Response, user: dict[str, Any] = Depends(current_user)):
    csrf_token = request.state.session["csrf_token"]
    _set_csrf_cookie(response, csrf_token)
    try:
        vault = current_vault(request, user)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        vault = None
    vault_block = None
    decommission_vault_block = None
    if vault is not None:
        role = vault["role"]
        vault_block = {
            "id": vault["id"],
            "slug": vault["slug"],
            "name": vault["name"],
            "role": role,
            "can_operate": can_operate(role),
            "delete_enabled": _runtime_settings().allow_local_delete and is_owner(role),
            "cloud_deletion_enabled": bool(vault.get("cloud_deletion_enabled"))
            and is_owner(role),
            "is_vault_owner": is_owner(role),
        }
    else:
        selected_id = request.state.session.get("vault_id")
        if selected_id:
            with db() as connection:
                decommission_vault = connection.execute(
                    """
                    SELECT v.id, v.slug, v.name, v.decommission_state,
                           v.root_released_at
                    FROM vaults v
                    JOIN vault_members vm ON vm.vault_id=v.id
                    WHERE v.id=%s AND vm.user_id=%s AND vm.role='owner'
                      AND v.decommission_state<>'active'
                    """,
                    (selected_id, user["id"]),
                ).fetchone()
            if decommission_vault:
                decommission_vault_block = {
                    **decommission_vault,
                    "root_released": bool(decommission_vault["root_released_at"]),
                }
        if decommission_vault_block is None:
            with db() as connection:
                decommission_vault = connection.execute(
                    """
                    SELECT v.id, v.slug, v.name, v.decommission_state,
                           v.root_released_at
                    FROM vaults v
                    JOIN vault_members vm ON vm.vault_id=v.id
                    WHERE vm.user_id=%s AND vm.role='owner'
                      AND v.decommission_state<>'active'
                    ORDER BY CASE WHEN v.decommission_state='decommissioning' THEN 0 ELSE 1 END,
                             v.decommissioned_at DESC, lower(v.name)
                    LIMIT 1
                    """,
                    (user["id"],),
                ).fetchone()
            if decommission_vault:
                decommission_vault_block = {
                    **decommission_vault,
                    "root_released": bool(decommission_vault["root_released_at"]),
                }
    return {
        **user,
        "csrf_token": csrf_token,
        "offline_cache_generation": _offline_file_cache_generation(
            request,
            vault["id"] if vault is not None else None,
        ),
        "auth_method": request.state.session.get("auth_method"),
        "locale": _request_locale(request),
        "locales": list(available_locales()),
        "vault": vault_block,
        "decommission_vault": decommission_vault_block,
    }


@app.get("/api/i18n/catalog", response_model=response_model("I18nCatalogResponse"))
def i18n_catalog(request: Request, locale: str | None = None):
    resolved = normalize_locale(locale) if locale else _request_locale(request)
    return {
        "locale": resolved,
        "locales": list(available_locales()),
        "messages": locale_catalog(resolved),
    }


@app.put("/api/locale", response_model=response_model("LocaleUpdateResponse"))
def update_locale(
    action: LocaleUpdate,
    request: Request,
    response: Response,
    user: dict[str, Any] = Depends(current_user),
):
    del user  # authentication gate only
    resolved = normalize_locale(action.locale)
    if (
        action.locale
        and action.locale.replace("_", "-").split("-", 1)[0].strip().lower()
        not in available_locales()
    ):
        raise HTTPException(400, "Unsupported locale")
    _set_locale_cookie(response, resolved)
    return {
        "locale": resolved,
        "message": translate("api.locale_updated", locale=resolved),
        "message_key": "api.locale_updated",
        "messages": locale_catalog(resolved),
    }


@app.get("/api/vaults", response_model=response_model("VaultsResponse"))
def user_vaults(user: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        rows = connection.execute(
            """
            SELECT v.id, v.slug, v.name, vm.role
            FROM vaults v
            JOIN vault_members vm ON vm.vault_id=v.id
            WHERE vm.user_id=%s AND v.enabled=TRUE
              AND v.decommission_state='active'
            ORDER BY v.name
            """,
            (user["id"],),
        ).fetchall()
    return {"items": rows}


@app.post("/api/vaults", status_code=201, response_model=response_model("VaultCreateResponse"))
def create_own_vault(
    action: VaultSelfServiceCreate,
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(current_user),
):
    """Let an authenticated, already-existing user create their own vault.

    The server generates the storage identity; it never provisions a user
    from identity claims (the caller must already be `current_user`).
    Crypt vaults also receive a one-time recovery export so the owner can
    confirm custody before uploads are admitted. Adoption binds an existing
    Source Area directory in place and starts an asynchronous local scan.
    """
    try:
        vault = create_vault_for_user(
            user["id"],
            action.name,
            action.slug,
            encryption_mode=action.encryption_mode,
            creation_mode=action.creation_mode,
            volume_alias=action.volume_alias,
            relative_path=action.relative_path,
            actor_is_admin=False,
        )
    except VaultSlugTaken as exc:
        raise HTTPException(409, str(exc)) from exc
    except VaultProvisioningUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except InvalidVaultName as exc:
        raise HTTPException(422, str(exc)) from exc
    except VaultAdoptionError as exc:
        raise HTTPException(422, str(exc)) from exc
    except VaultCreationError as exc:
        raise HTTPException(409, str(exc)) from exc
    if action.creation_mode == "adopt":
        # scan_vault reloads the authoritative row; do not hand a background
        # task the self-service recovery ciphertexts.
        background_tasks.add_task(scan_vault, {"id": vault["id"]})
    payload: dict[str, Any] = {
        "id": vault["id"],
        "uuid": vault["uuid"],
        "slug": vault["slug"],
        "name": vault["name"],
        "role": "owner",
        "encryption_mode": vault["encryption_mode"],
        "recovery_custody_confirmed": bool(
            vault.get("recovery_custody_confirmed_at")
        ),
        "creation_mode": action.creation_mode,
    }
    if vault["encryption_mode"] == "crypt":
        payload["recovery_export"] = build_recovery_export(vault)
    return payload


_VAULT_DECOMMISSION_ERROR_STATUS: dict[str, tuple[int, str]] = {
    "not_found": (404, "Vault not found"),
    "not_started": (404, "Vault decommission has not started"),
    "reason_required": (422, "A reason between 3 and 500 characters is required"),
    "confirmation_required": (422, "Type the exact Vault name to confirm"),
    "invalid_disposition": (422, "Invalid decommission disposition"),
    "owner_required": (403, "Only the primary owner can decommission this Vault"),
    "stale_preview": (409, "Vault contents changed; request a new preview"),
    "blocked": (409, "Vault decommission is blocked; review the preview"),
    "decommission_in_progress": (409, "Vault decommission is already in progress"),
    "already_decommissioned": (409, "Vault root has already been released"),
    "state_changed": (409, "Vault lifecycle changed; request a new preview"),
    "cloud_purge_not_cancellable": (409, "Cloud purge delay is no longer cancellable"),
}


def _vault_decommission_http_error(
    exc: vault_decommission_service.VaultDecommissionError,
) -> HTTPException:
    status_code, message = _VAULT_DECOMMISSION_ERROR_STATUS.get(
        exc.reason, (409, "Vault decommission could not be completed")
    )
    return HTTPException(status_code, message)


def _preview_vault_decommission(
    vault_id: int, action: VaultDecommissionPreview
) -> dict[str, Any]:
    with status_lock:
        scan_active = bool(runtime_status.get(vault_id, {}).get("scanning"))
    runtime = _runtime_settings()
    try:
        with db() as connection:
            return vault_decommission_service.build_preview(
                connection,
                vault_id=vault_id,
                local_disposition=action.local_disposition,
                cloud_disposition=action.cloud_disposition,
                local_delete_enabled=runtime.allow_local_delete,
                runtime_scan_active=scan_active,
            )
    except vault_decommission_service.VaultDecommissionError as exc:
        raise _vault_decommission_http_error(exc) from exc


def _start_vault_decommission(
    *,
    vault_id: int,
    action: VaultDecommissionStart,
    actor: dict[str, Any],
    actor_is_admin: bool,
) -> dict[str, Any]:
    runtime = _runtime_settings()
    operation_lock = scan_lock_for_vault(vault_id)
    if not operation_lock.acquire(blocking=False):
        raise HTTPException(409, "A Vault scan is active; request a new preview")
    try:
        with status_lock:
            scan_active = bool(runtime_status.get(vault_id, {}).get("scanning"))
        if scan_active:
            raise HTTPException(409, "A Vault scan is active; request a new preview")
        try:
            with db() as connection:
                vault_decommission_service.start_decommission(
                    connection,
                    vault_id=vault_id,
                    actor_user_id=int(actor["id"]),
                    actor_is_admin=actor_is_admin,
                    local_disposition=action.local_disposition,
                    cloud_disposition=action.cloud_disposition,
                    confirmation=action.confirmation,
                    reason=action.reason,
                    preview_fingerprint=action.preview_fingerprint,
                    local_delete_enabled=runtime.allow_local_delete,
                    purge_delay_seconds=runtime.cloud_purge_delay_seconds,
                    runtime_scan_active=False,
                )
                result = vault_decommission_service.reconcile_one(
                    connection,
                    vault_id=vault_id,
                    local_delete_enabled=runtime.allow_local_delete,
                    purge_delay_seconds=runtime.cloud_purge_delay_seconds,
                )
        except vault_decommission_service.VaultDecommissionError as exc:
            vault_decommission_service.release_runtime_gate(vault_id)
            raise _vault_decommission_http_error(exc) from exc
        except BaseException:
            # The database context rolled back; do not leave an in-process-only
            # suspension without its persistent decommission row.
            vault_decommission_service.release_runtime_gate(vault_id)
            raise
    finally:
        operation_lock.release()
    if result["state"] == "completed":
        vault_decommission_service.release_runtime_gate(vault_id)
    return result


@app.post("/api/vault/decommission/preview", response_model=response_model("VaultDecommissionPreview"))
def preview_own_vault_decommission(
    action: VaultDecommissionPreview,
    vault: dict[str, Any] = Depends(decommission_owner_vault),
):
    return _preview_vault_decommission(int(vault["id"]), action)


@app.get("/api/vault/decommission/status", response_model=response_model("VaultDecommissionStatus"))
def own_vault_decommission_status(
    vault: dict[str, Any] = Depends(decommission_owner_vault),
):
    try:
        with db() as connection:
            return vault_decommission_service.operation_status(
                connection, vault_id=int(vault["id"])
            )
    except vault_decommission_service.VaultDecommissionError as exc:
        raise _vault_decommission_http_error(exc) from exc


@app.post("/api/vault/decommission", status_code=202, response_model=response_model("VaultDecommissionStatus"))
def start_own_vault_decommission(
    action: VaultDecommissionStart,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(decommission_owner_vault),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    return _start_vault_decommission(
        vault_id=int(vault["id"]),
        action=action,
        actor=user,
        actor_is_admin=False,
    )


@app.post("/api/vault/decommission/cloud-purge/cancel", response_model=JsonObjectResponse)
def cancel_own_vault_decommission_cloud_purge(
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(decommission_owner_vault),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    try:
        with db() as connection:
            return vault_decommission_service.cancel_pending_cloud_purge(
                connection,
                vault_id=int(vault["id"]),
                actor_user_id=int(user["id"]),
            )
    except vault_decommission_service.VaultDecommissionError as exc:
        raise _vault_decommission_http_error(exc) from exc


@app.post("/api/vaults/{vault_id}/decommission/preview", response_model=response_model("VaultDecommissionPreview"))
def preview_owned_vault_decommission_by_id(
    vault_id: int,
    action: VaultDecommissionPreview,
    user: dict[str, Any] = Depends(current_user),
):
    _owned_vault_for_decommission(vault_id, int(user["id"]))
    return _preview_vault_decommission(vault_id, action)


@app.get("/api/vaults/{vault_id}/decommission/status", response_model=response_model("VaultDecommissionStatus"))
def owned_vault_decommission_status_by_id(
    vault_id: int,
    user: dict[str, Any] = Depends(current_user),
):
    _owned_vault_for_decommission(vault_id, int(user["id"]))
    try:
        with db() as connection:
            return vault_decommission_service.operation_status(
                connection, vault_id=vault_id
            )
    except vault_decommission_service.VaultDecommissionError as exc:
        raise _vault_decommission_http_error(exc) from exc


@app.post("/api/vaults/{vault_id}/decommission", status_code=202, response_model=response_model("VaultDecommissionStatus"))
def start_owned_vault_decommission_by_id(
    vault_id: int,
    action: VaultDecommissionStart,
    user: dict[str, Any] = Depends(current_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    _owned_vault_for_decommission(vault_id, int(user["id"]))
    return _start_vault_decommission(
        vault_id=vault_id,
        action=action,
        actor=user,
        actor_is_admin=False,
    )


@app.post("/api/vaults/{vault_id}/decommission/cloud-purge/cancel", response_model=JsonObjectResponse)
def cancel_owned_vault_decommission_cloud_purge_by_id(
    vault_id: int,
    user: dict[str, Any] = Depends(current_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    _owned_vault_for_decommission(vault_id, int(user["id"]))
    try:
        with db() as connection:
            return vault_decommission_service.cancel_pending_cloud_purge(
                connection,
                vault_id=vault_id,
                actor_user_id=int(user["id"]),
            )
    except vault_decommission_service.VaultDecommissionError as exc:
        raise _vault_decommission_http_error(exc) from exc


@app.post("/api/vault/recovery/confirm", response_model=response_model("RecoveryConfirmResponse"))
def confirm_vault_recovery_custody(
    action: RecoveryConfirm,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(owner_vault),
):
    if not action.acknowledged:
        raise HTTPException(422, "Recovery custody must be acknowledged")
    try:
        with db() as connection:
            updated = confirm_recovery_custody(connection, vault_id=vault["id"])
    except RecoveryError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "vault_id": updated["id"],
        "recovery_custody_confirmed": True,
        "recovery_custody_confirmed_at": updated["recovery_custody_confirmed_at"],
    }


@app.post("/api/vault/recovery/export", response_model=response_model("RecoveryExportResponse"))
def reexport_vault_recovery_secret(
    action: RecoveryExportRequest,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(owner_vault),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM vaults WHERE id=%s", (vault["id"],)
        ).fetchone()
    if not row or row["encryption_mode"] != "crypt":
        raise HTTPException(422, "Recovery export is only available for crypt vaults")
    if not row["recovery_custody_confirmed_at"]:
        # First-time material is returned at creation; after that owners use
        # confirm + reauth-gated re-export.
        raise HTTPException(
            409,
            "Confirm recovery-secret custody before requesting a re-export",
        )
    try:
        with db() as connection:
            export = export_recovery_secret(
                row,
                actor_id=user["id"],
                reason=action.reason,
                connection=connection,
            )
    except RecoveryError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"recovery_export": export}


@app.post("/api/vaults/select", response_model=response_model("VaultSelectResponse"))
def select_vault(
    action: VaultSelection,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
):
    with db() as connection:
        allowed = connection.execute(
            """
            SELECT 1 FROM vault_members vm
            JOIN vaults v ON v.id=vm.vault_id
            WHERE vm.vault_id=%s AND vm.user_id=%s AND v.enabled=TRUE
              AND v.decommission_state='active'
            """,
            (action.vault_id, user["id"]),
        ).fetchone()
        if not allowed:
            raise HTTPException(403, "Vault access denied")
        updated = set_session_vault(
            connection,
            request.state.session["id"],
            action.vault_id,
            expected_generation=request.state.session["offline_cache_generation"],
            expected_nonce=request.state.session["offline_cache_nonce"],
        )
    if not updated:
        # The row transition is conditional on the Session snapshot observed by
        # current_user, so another process cannot be silently overwritten.
        raise HTTPException(409, "Offline cache authorization changed")
    request.state.session.update(updated)
    return {**_api_message(request, "api.vault_selected")}


@app.get(
    "/api/files",
    response_model=response_model("FilesResponse"),
    responses={**OFFLINE_FILE_CACHE_FILES_RESPONSES, **LOGICAL_PATH_ERROR_RESPONSES},
)
def list_files(
    q: str = "",
    state: str = "",
    directory: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=10, le=500),
    vault: dict[str, Any] = Depends(current_vault),
    request: Request = None,
    response: Response = None,
    offline_cache_authorization: str | None = Header(
        default=None,
        alias=OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER,
        description=OFFLINE_FILE_CACHE_AUTHORIZATION_DESCRIPTION,
    ),
):
    """List Vault Files with an optional persisted offline-cache authorization.

    A request carrying X-FrostVault-Offline-Cache-Authorization is checked
    against the durable Session row before and after the catalog query. A 409
    is intentionally not cacheable authority: discard it, refetch /api/me,
    then retry only with that newly issued generation.
    """
    # Defaults retain the direct catalog test seam; routed requests always
    # receive FastAPI's Request/Response instances and enforce the guard.
    expected_cache_generation = (
        _validate_offline_file_cache_generation(
            request,
            vault["id"],
            offline_cache_authorization,
        )
        if request is not None
        else None
    )
    directory = normalize_directory(directory, api=True)
    started = time.perf_counter()
    with db() as connection:
        catalog = ArchiveCatalog(connection)
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
        rows_materialized = int(catalog.last_listing_rows_materialized)

    metrics_service.set_gauge(
        "directory_listing_duration_seconds",
        float(time.perf_counter() - started),
    )
    metrics_service.set_gauge(
        "directory_listing_rows_materialized",
        float(rows_materialized),
    )

    # The list query may have overlapped a logout or Vault selection. Do not
    # return a cacheable old payload after the server-side Session transitioned.
    if request is not None and expected_cache_generation is not None:
        if not _offline_file_cache_generation_is_current(
            request,
            vault["id"],
            expected_cache_generation,
        ):
            raise _offline_cache_authorization_changed()
        if response is not None:
            _set_offline_file_cache_generation(response, expected_cache_generation)
    return {
        "items": items,
        "total": total,
        "page": page,
        "directory": directory,
        "mode": listing["mode"],
    }


@app.get(
    "/api/file-history",
    response_model=response_model("FileHistoryResponse"),
    responses={**LOGICAL_PATH_ERROR_RESPONSES, **SCOPED_NOT_FOUND_RESPONSES},
)
def file_history(
    path: str,
    vault: dict[str, Any] = Depends(current_vault),
):
    logical_path = safe_relative_path(path).as_posix()
    with db() as connection:
        catalog = ArchiveCatalog(connection)
        observed = catalog.get_file_by_path(vault["id"], logical_path)
        if observed is None:
            raise HTTPException(404, "File not found")
        versions = catalog.list_versions(vault["id"], logical_path)
        path_history = catalog.list_path_history(
            observed["id"], vault_id=vault["id"]
        )
    return {
        "vault_file_id": observed["id"],
        "path": logical_path,
        "path_history": path_history,
        "versions": versions,
    }


@app.get("/api/rename-candidates", response_model=JsonObjectResponse)
def rename_candidates(vault: dict[str, Any] = Depends(current_vault)):
    with db() as connection:
        candidates = ArchiveCatalog(connection).list_rename_candidates(vault["id"])
    return {"items": candidates}


@app.post(
    "/api/confirm-rename",
    status_code=202,
    response_model=JsonObjectResponse,
    responses={**LOGICAL_PATH_ERROR_RESPONSES, **SCOPED_NOT_FOUND_RESPONSES},
)
def confirm_rename(
    action: ConfirmRenameAction,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
):
    if not can_operate(vault["role"]):
        raise HTTPException(403, "Vault is read-only")
    if vault_relocation_service.local_work_suspended(vault):
        raise HTTPException(409, "Local work is suspended pending relocation scan")
    new_path = safe_relative_path(action.new_path).as_posix()
    with db() as connection:
        catalog = ArchiveCatalog(connection)
        try:
            confirmed_file_id = catalog.confirm_file_rename(
                vault_file_id=action.vault_file_id,
                new_path=new_path,
                changed_at=now_iso(),
                vault_id=vault["id"],
            )
        except VaultFileNotFound as exc:
            # Do not reveal whether a supplied ID is foreign, retired, or absent.
            raise HTTPException(404, "Vault File not found") from exc
        audit_log(
            "vault_file_renamed",
            connection=connection,
            vault_id=vault["id"],
            vault_file_id=confirmed_file_id,
            new_path=new_path,
            decision="confirmed",
            actor_id=user["id"],
        )
    try:
        queued = queue_jobs(new_path, "rename", vault["id"], user["id"])
    except HTTPException as exc:
        if exc.status_code == 409:
            return {
                "vault_file_id": confirmed_file_id,
                "path": new_path,
                "message": "Rename confirmed; no cloud migration required",
            }
        raise
    return {
        **queued,
        "vault_file_id": confirmed_file_id,
        "path": new_path,
        "message": "Rename confirmed",
    }


@app.post(
    "/api/confirm-folder-rename",
    status_code=202,
    response_model=JsonObjectResponse,
    responses={**LOGICAL_PATH_ERROR_RESPONSES, **SCOPED_NOT_FOUND_RESPONSES},
)
def confirm_folder_rename(
    action: ConfirmFolderRenameAction,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
):
    if not can_operate(vault["role"]):
        raise HTTPException(403, "Vault is read-only")
    if vault_relocation_service.local_work_suspended(vault):
        raise HTTPException(409, "Local work is suspended pending relocation scan")
    old_prefix = safe_relative_path(action.old_prefix).as_posix()
    new_prefix = safe_relative_path(action.new_prefix).as_posix()
    with db() as connection:
        catalog = ArchiveCatalog(connection)
        try:
            renamed_ids = catalog.confirm_folder_rename(
                vault_id=vault["id"],
                old_prefix=old_prefix,
                new_prefix=new_prefix,
                changed_at=now_iso(),
            )
        except VaultFileNotFound as exc:
            raise HTTPException(404, "Vault File not found") from exc
        audit_log(
            "vault_folder_renamed",
            connection=connection,
            vault_id=vault["id"],
            old_prefix=old_prefix,
            new_prefix=new_prefix,
            file_count=len(renamed_ids),
            decision="confirmed",
            actor_id=user["id"],
        )
    try:
        queued = queue_jobs(
            new_prefix, "rename", vault["id"], user["id"], is_directory=True
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            return {
                "renamed_ids": renamed_ids,
                "message": "Folder rename confirmed; no cloud migration required",
            }
        raise
    return {
        **queued,
        "renamed_ids": renamed_ids,
        "message": "Folder rename confirmed",
    }


@app.get("/api/stats", response_model=response_model("StatsResponse"))
def stats(vault: dict[str, Any] = Depends(current_vault)):
    started = time.perf_counter()
    source_root = vault.get("source_root") or ""
    # Reconcile Source Volume identity before anything can preflight or walk the
    # configured Vault tree. Catalog summaries remain available when local work
    # is suspended, so they are intentionally collected after this gate too.
    access = vault_local_access(source_root)
    with db() as connection:
        # Cheap SQL aggregates only — never materialize every Vault File row.
        summary = ArchiveCatalog(connection).summary(vault["id"])
    allowed_bases = [str(get_sources_root())]
    bootstrap_root = (settings.bootstrap_vault_source_root or "").strip()
    if bootstrap_root:
        allowed_bases.append(bootstrap_root)
    # Identity-unsafe / missing volumes fail closed: no resolve, no walk.
    preflight_allowed = access.volume_health in {"ok", "read_only", "scan_required"}
    # Consistent bounded snapshot under the producer lock, then release before
    # any merge/response work. Never dict(runtime_status[...]) unlocked while
    # storage mutates filesystem under status_lock.
    runtime = snapshot_runtime_status_for_stats(int(vault["id"]))
    scan_filesystem = runtime.get("filesystem") or {}
    # Prefer the producer synopsis (totals/counts + bounded sample). Merge inputs
    # come from the already-bounded copy so the request path never re-walks the
    # legacy collection.
    scan_findings = scan_filesystem.get("findings") or []
    scan_finding_counts = scan_filesystem.get("finding_counts")
    raw_scan_total = scan_filesystem.get("findings_total")
    try:
        scan_findings_total = (
            int(raw_scan_total) if raw_scan_total is not None else None
        )
    except (TypeError, ValueError):
        scan_findings_total = None
    # Filesystem health is a cached/background revision with a bounded synopsis.
    # The request path must not os.walk the Vault root or serialize unbounded
    # findings; single-flight recomputation runs off-thread.
    filesystem_payload = build_stats_filesystem_payload(
        vault_id=int(vault["id"]),
        source_root=str(source_root),
        allowed_bases=allowed_bases,
        volume_alias=access.volume_alias,
        volume_health=access.volume_health,
        local_operations_allowed=access.local_operations_allowed,
        cloud_catalog_allowed=access.cloud_catalog_allowed,
        preflight_allowed=preflight_allowed,
        scan_findings=scan_findings,
        scan_finding_counts=scan_finding_counts
        if isinstance(scan_finding_counts, dict)
        else None,
        scan_findings_total=scan_findings_total,
    )
    metrics_service.set_gauge(
        "stats_last_duration_seconds",
        max(0.0, time.perf_counter() - started),
    )
    return {
        **summary,
        "runtime": runtime,
        "filesystem": filesystem_payload,
        "delete_enabled": _runtime_settings().allow_local_delete and is_owner(vault["role"]),
    }


@app.get("/api/audit-events", response_model=JsonObjectResponse)
def vault_audit_events(vault: dict[str, Any] = Depends(current_vault)):
    """List audit events visible to the current Vault membership role."""
    with db() as connection:
        events = audit_event_store.list_vault_audit_events(
            connection,
            vault["id"],
            include_owner=is_owner(vault["role"]),
        )
    return {"events": events}


@app.get("/api/admin/audit-events", response_model=JsonObjectResponse)
def admin_audit_events(_: dict[str, Any] = Depends(admin_user)):
    """List all audit events for global administrators."""
    with db() as connection:
        events = audit_event_store.list_admin_audit_events(connection)
    return {"events": events}


@app.get("/api/admin/settings", response_model=response_model("SystemSettingsResponse"))
def admin_system_settings(_: dict[str, Any] = Depends(admin_user)):
    with db() as connection:
        return system_settings_response(connection, settings_obj=settings)


@app.patch("/api/admin/settings", response_model=response_model("SystemSettingsResponse"))
def update_admin_system_settings(
    action: SystemSettingsUpdate,
    user: dict[str, Any] = Depends(admin_user),
    _: dict[str, Any] = Depends(require_recent_reauth),
) -> dict[str, Any]:
    with db() as connection:
        return apply_system_settings(
            connection,
            expected_revision=action.revision,
            overrides=action.overrides,
            removals=action.removals,
            updated_by=user["id"],
            settings_obj=settings,
        )


@app.get("/api/admin/oidc-configuration", response_model=response_model("OidcConfigurationResponse"))
def admin_oidc_configuration(
    request: Request,
    _: dict[str, Any] = Depends(admin_user),
):
    with db() as connection:
        return oidc_configuration_response(
            connection,
            settings_obj=settings,
            callback_url=str(request.url_for("oidc_callback")),
        )


@app.put("/api/admin/oidc-configuration/draft", response_model=response_model("OidcConfigurationResponse"))
def save_admin_oidc_draft(
    request: Request,
    action: OidcDraftAction,
    user: dict[str, Any] = Depends(admin_user),
    _: dict[str, Any] = Depends(require_recent_reauth),
):
    with db() as connection:
        try:
            save_oidc_draft(
                connection,
                issuer=action.issuer,
                client_id=action.client_id,
                client_secret=action.client_secret,
                scopes=action.scopes,
                login_transaction_ttl_seconds=(
                    action.login_transaction_ttl_seconds
                ),
                updated_by=user["id"],
                settings_obj=settings,
            )
        except OidcConfigurationError as error:
            raise HTTPException(503, str(error)) from error
        audit_event_store.record_audit_event(
            connection,
            event="oidc_configuration_draft_saved",
            actor_user_id=user["id"],
            outcome="success",
            visibility="admin",
            issuer=action.issuer,
            client_id=action.client_id,
            scopes=action.scopes,
            login_transaction_ttl_seconds=(
                action.login_transaction_ttl_seconds
            ),
            client_secret_replaced=True,
        )
        return oidc_configuration_response(
            connection,
            settings_obj=settings,
            callback_url=str(request.url_for("oidc_callback")),
        )


@app.post("/api/admin/oidc-configuration/draft/validate", response_model=response_model("OidcConfigurationResponse"))
def validate_admin_oidc_draft(
    request: Request,
    user: dict[str, Any] = Depends(admin_user),
    _: dict[str, Any] = Depends(require_recent_reauth),
):
    with db() as connection:
        try:
            status = validate_oidc_draft(
                connection,
                http_client=_oidc_client(),
                host_addresses=_oidc_host_addresses,
            )
        except OidcConfigurationError as error:
            raise HTTPException(409, str(error)) from error
        audit_event_store.record_audit_event(
            connection,
            event="oidc_configuration_draft_validated",
            actor_user_id=user["id"],
            outcome="success" if status == "valid" else "failure",
            visibility="admin",
            validation_status=status,
        )
        response = oidc_configuration_response(
            connection,
            settings_obj=settings,
            callback_url=str(request.url_for("oidc_callback")),
        )
        if status == "invalid":
            return JSONResponse(response, status_code=422)
        return response


@app.post("/api/admin/oidc-configuration/activate", response_model=response_model("OidcConfigurationResponse"))
def activate_admin_oidc_configuration(
    request: Request,
    user: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    with db() as connection:
        try:
            version = activate_oidc_draft(
                connection,
                updated_by=user["id"],
                settings_obj=settings,
            )
        except OidcConfigurationError as error:
            raise HTTPException(409, str(error)) from error
        audit_event_store.record_audit_event(
            connection,
            event="oidc_configuration_activated",
            actor_user_id=user["id"],
            outcome="success",
            visibility="admin",
            version=version,
        )
        return oidc_configuration_response(
            connection,
            settings_obj=settings,
            callback_url=str(request.url_for("oidc_callback")),
        )


@app.post("/api/admin/oidc-configuration/disable", response_model=response_model("OidcConfigurationResponse"))
def disable_admin_oidc_configuration(
    request: Request,
    user: dict[str, Any] = Depends(admin_user),
    _: dict[str, Any] = Depends(require_recent_reauth),
):
    with db() as connection:
        try:
            version = disable_oidc(
                connection,
                updated_by=user["id"],
                settings_obj=settings,
            )
        except OidcConfigurationError as error:
            raise HTTPException(503, str(error)) from error
        audit_event_store.record_audit_event(
            connection,
            event="oidc_configuration_disabled",
            actor_user_id=user["id"],
            outcome="success",
            visibility="admin",
            version=version,
        )
        return oidc_configuration_response(
            connection,
            settings_obj=settings,
            callback_url=str(request.url_for("oidc_callback")),
        )


@app.post("/api/admin/oidc-configuration/rotate-secret", response_model=response_model("OidcConfigurationResponse"))
def rotate_admin_oidc_secret(
    request: Request,
    action: OidcSecretRotationAction,
    user: dict[str, Any] = Depends(admin_user),
    _: dict[str, Any] = Depends(require_recent_reauth),
):
    with db() as connection:
        try:
            version = rotate_oidc_secret(
                connection,
                client_secret=action.client_secret,
                updated_by=user["id"],
                settings_obj=settings,
            )
        except OidcConfigurationConflict as error:
            raise HTTPException(409, str(error)) from error
        except OidcConfigurationError as error:
            raise HTTPException(503, str(error)) from error
        audit_event_store.record_audit_event(
            connection,
            event="oidc_client_secret_rotated",
            actor_user_id=user["id"],
            outcome="success",
            visibility="admin",
            version=version,
            client_secret_replaced=True,
        )
        return oidc_configuration_response(
            connection,
            settings_obj=settings,
            callback_url=str(request.url_for("oidc_callback")),
        )


@app.get("/api/notifications", response_model=JsonObjectResponse)
def list_notifications(
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    limit: int = Query(50, ge=1, le=200),
    status: Literal["all", "unread", "read"] = Query("all"),
    before_id: int | None = Query(default=None, ge=1),
):
    """List in-app notifications for the authenticated user.

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
            user_id=user["id"],
            limit=limit + 1,
            locale=locale,
            status=status,
            before_id=before_id,
        )
        has_more = len(fetched) > limit
        items = fetched[:limit]
        unread_count = notification_service.count_unread_notifications(
            connection, user_id=user["id"]
        )
    return {"items": items, "unread_count": unread_count, "has_more": has_more}


class NotificationReadAction(BaseModel):
    notification_id: int


@app.post("/api/notifications/read", response_model=JsonObjectResponse)
def mark_notification_read(
    action: NotificationReadAction,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
):
    with db() as connection:
        item = notification_service.mark_notification_read(
            connection,
            notification_id=action.notification_id,
            user_id=user["id"],
            locale=_request_locale(request),
        )
    if item is None:
        raise HTTPException(404, "Notification not found")
    return item


@app.post("/api/notifications/read-all", response_model=JsonObjectResponse)
def mark_all_notifications_read(
    user: dict[str, Any] = Depends(current_user),
):
    """Mark every currently visible unread notification read (issue #225).

    Server-authoritative and idempotent; preserves the same membership and
    in-app visibility checks as single-item mark-read.
    """
    with db() as connection:
        return notification_service.mark_all_notifications_read(
            connection, user_id=user["id"]
        )


class PushSubscribeAction(BaseModel):
    endpoint: str = Field(min_length=8, max_length=2000)
    keys: dict[str, str]


@app.get("/api/push/config", response_model=JsonObjectResponse)
def push_config():
    """Public VAPID config; degrades cleanly when push is unconfigured."""
    from .config import push_configured

    if not push_configured():
        return {"configured": False, "vapid_public_key": None}
    return {
        "configured": True,
        "vapid_public_key": settings.vapid_public_key.strip(),
    }


@app.post("/api/push/subscriptions", response_model=JsonObjectResponse)
def subscribe_push(
    action: PushSubscribeAction,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
):
    """Persist a Web Push subscription for the current Session/device."""
    from .config import push_configured

    p256dh = (action.keys.get("p256dh") or "").strip()
    auth = (action.keys.get("auth") or "").strip()
    if not p256dh or not auth:
        raise HTTPException(400, "Push subscription keys are required")
    if not push_configured():
        # Seam 7: unconfigured push must not surface errors to the user.
        return {"configured": False, "accepted": False}
    session = request.state.session
    with db() as connection:
        saved = notification_service.upsert_push_subscription(
            connection,
            user_id=user["id"],
            session_id=session["id"],
            endpoint=action.endpoint.strip(),
            p256dh=p256dh,
            auth=auth,
        )
    return saved


class WebhookEndpointAction(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    enabled: bool = True
    reason: str = Field(min_length=3, max_length=500)


class SmtpEndpointAction(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=587, ge=1, le=65535)
    username: str = ""
    password: str = ""
    from_address: str = Field(min_length=3, max_length=320)
    use_tls: bool = True
    enabled: bool = True
    reason: str = Field(min_length=3, max_length=500)


class VaultNotificationPreferenceAction(BaseModel):
    event: str = Field(min_length=1, max_length=100)
    channel: str = Field(min_length=1, max_length=20)
    enabled: bool = True
    recipient_user_ids: list[int] = Field(default_factory=list)


@app.post("/api/admin/notification-endpoints/webhook", response_model=JsonObjectResponse)
def admin_set_webhook_endpoint(
    action: WebhookEndpointAction,
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    with db() as connection:
        endpoint = notification_service.set_global_webhook_endpoint(
            connection, url=action.url, enabled=action.enabled
        )
        audit_event_store.record_audit_event(
            connection,
            event="notification_endpoint_updated",
            actor_user_id=admin["id"],
            outcome="success",
            visibility="admin",
            kind="webhook",
            reason=action.reason,
            enabled=action.enabled,
        )
    return endpoint


@app.post("/api/admin/notification-endpoints/smtp", response_model=JsonObjectResponse)
def admin_set_smtp_endpoint(
    action: SmtpEndpointAction,
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    with db() as connection:
        endpoint = notification_service.set_global_smtp_endpoint(
            connection,
            host=action.host,
            port=action.port,
            username=action.username,
            password=action.password,
            from_address=action.from_address,
            use_tls=action.use_tls,
            enabled=action.enabled,
        )
        audit_event_store.record_audit_event(
            connection,
            event="notification_endpoint_updated",
            actor_user_id=admin["id"],
            outcome="success",
            visibility="admin",
            kind="smtp",
            reason=action.reason,
            enabled=action.enabled,
            host=action.host,
            # password redacted by audit store
            password=action.password,
        )
    return {"id": endpoint["id"], "kind": "smtp", "enabled": endpoint["enabled"]}


def list_own_vault_notification_preferences(request: Request):
    """List only the acting User's choices for the selected active Vault."""
    # This GET is deliberately registered as a Starlette route rather than an
    # APIRoute: the checked-in OpenAPI artifact is frontend-owned, while the
    # endpoint remains available to the SPA and preserves that artifact's
    # compatibility contract until the frontend client is regenerated.
    user = current_user(request)
    vault = current_vault(request, user)
    with db() as connection:
        items = notification_service.list_user_vault_notification_preferences(
            connection, user_id=user["id"], vault_id=vault["id"]
        )
    return JSONResponse({"items": items})


app.add_route(
    "/api/vault/notification-preferences",
    list_own_vault_notification_preferences,
    methods=["GET"],
)


@app.post("/api/vault/notification-preferences", response_model=JsonObjectResponse)
def set_vault_notification_preference(
    action: VaultNotificationPreferenceAction,
    vault: dict[str, Any] = Depends(current_vault),
    user: dict[str, Any] = Depends(current_user),
):
    # ``recipient_user_ids`` remains accepted for old clients but is deliberately
    # ignored: a personal mutation can never write another User's preference.
    with db() as connection:
        try:
            pref = notification_service.set_user_vault_notification_preference(
                connection,
                user_id=user["id"],
                vault_id=vault["id"],
                event=action.event,
                channel=action.channel,
                enabled=action.enabled,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return pref


@app.get("/api/admin/worker-errors", response_model=JsonObjectResponse)
def admin_worker_errors(_: dict[str, Any] = Depends(admin_user)):
    with db() as connection:
        items = worker_error_store.list_worker_errors(connection)
    return {"items": items}


class MetadataBackupRunAction(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@app.get("/api/admin/metadata-backups", response_model=JsonObjectResponse)
def admin_list_metadata_backups(_: dict[str, Any] = Depends(admin_user)):
    with db() as connection:
        status = metadata_backup_service.backup_status(connection)
        runs = metadata_backup_service.list_backup_artifacts(connection)
    return {"status": status, "runs": runs}


@app.post("/api/admin/metadata-backups/run", response_model=JsonObjectResponse)
def admin_run_metadata_backup(
    action: MetadataBackupRunAction,
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    with db() as connection:
        try:
            result = metadata_backup_service.run_metadata_backup(
                connection,
                reason="manual",
                backup_dir=settings.metadata_backup_dir,
                object_store=metadata_backup_service.default_object_store(),
                retention=_runtime_settings().metadata_backup_retention,
                s3_prefix=_runtime_settings().metadata_backup_s3_prefix,
            )
        except metadata_backup_service.BackupError as exc:
            audit_event_store.record_audit_event(
                connection,
                event="metadata_backup_failed",
                actor_user_id=admin["id"],
                outcome="failure",
                visibility="admin",
                reason=action.reason,
                error=str(exc),
            )
            raise HTTPException(500, str(exc)) from exc
        audit_event_store.record_audit_event(
            connection,
            event="metadata_backup_created",
            actor_user_id=admin["id"],
            outcome="success",
            visibility="admin",
            reason=action.reason,
            digest_sha256=result["digest_sha256"],
            s3_key=result.get("s3_key"),
        )
    return result


@app.get(
    "/api/admin/metadata-backups/download/{run_id}",
    response_class=Response,
    responses={
        200: {
            "description": "Raw metadata backup artifact",
            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
def admin_download_metadata_backup(
    run_id: int,
    admin: dict[str, Any] = Depends(admin_user),
):
    with db() as connection:
        try:
            artifact = metadata_backup_service.open_backup_artifact(
                connection,
                run_id,
                backup_dir=settings.metadata_backup_dir,
            )
        except metadata_backup_service.BackupError as exc:
            raise HTTPException(404, str(exc)) from exc
        audit_event_store.record_audit_event(
            connection,
            event="metadata_backup_downloaded",
            actor_user_id=admin["id"],
            outcome="success",
            visibility="admin",
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


def build_job_groups(
    rows: list[dict[str, Any]],
    *,
    locale: str = DEFAULT_LOCALE,
) -> list[dict[str, Any]]:
    """Aggregate file jobs by operation, weighting progress by transferred bytes."""
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        group_id = row.get("group_id") or f"job-{row['id']}"
        display_message = present_job_message(row, locale)
        group = groups.setdefault(
            group_id,
            {
                "id": group_id,
                "path": row.get("group_path") or row["path"],
                "action": row["action"],
                "status": "completed",
                "message": "",
                "message_key": row.get("message_key"),
                "total_bytes": 0,
                "transferred_bytes": 0,
                "item_count": 0,
                "completed_count": 0,
                "failed_count": 0,
                "cancelled_count": 0,
                "updated_at": row["updated_at"],
                "pending_until": row.get("pending_until"),
                "estimated_cost_eur": row.get("estimated_cost_eur"),
                "estimated_hours": row.get("estimated_hours"),
                "restore_tier": row.get("restore_tier"),
                "restore_days": row.get("restore_days"),
            },
        )
        if row.get("pending_until"):
            group["pending_until"] = row["pending_until"]
        if row.get("estimated_cost_eur") is not None:
            group["estimated_cost_eur"] = row["estimated_cost_eur"]
        if row.get("estimated_hours") is not None:
            group["estimated_hours"] = row["estimated_hours"]
        if row.get("restore_tier"):
            group["restore_tier"] = row["restore_tier"]
        if row.get("restore_days") is not None:
            group["restore_days"] = row["restore_days"]
        total_bytes = int(row.get("total_bytes") or 0)
        transferred = int(row.get("transferred_bytes") or 0)
        group["total_bytes"] += total_bytes
        group["transferred_bytes"] += total_bytes if row["status"] == "completed" else min(transferred, total_bytes)
        group["item_count"] += 1
        if row["status"] == "completed":
            group["completed_count"] += 1
            if not group["message"]:
                group["message"] = display_message
                group["message_key"] = row.get("message_key")
        elif row["status"] == "failed":
            group["failed_count"] += 1
            group["message"] = display_message or translate(
                "job.operation_failed", locale=locale
            )
            group["message_key"] = row.get("message_key") or "job.operation_failed"
        elif row["status"] == "cancelled":
            group["cancelled_count"] += 1
            group["message"] = display_message or translate(
                "job.operation_stopped", locale=locale
            )
            group["message_key"] = row.get("message_key") or "job.operation_stopped"
        else:
            group["status"] = row["status"]
            group["message"] = display_message
            group["message_key"] = row.get("message_key")
        if str(row["updated_at"]) > str(group["updated_at"]):
            group["updated_at"] = row["updated_at"]

    for group in groups.values():
        active_count = (
            group["item_count"]
            - group["completed_count"]
            - group["failed_count"]
            - group["cancelled_count"]
        )
        if active_count == 0:
            group["status"] = (
                "failed"
                if group["failed_count"]
                else "cancelled"
                if group["cancelled_count"]
                else "completed"
            )
        total_bytes = group["total_bytes"]
        finished_count = (
            group["completed_count"] + group["failed_count"] + group["cancelled_count"]
        )
        group["percent"] = round(
            100 * group["transferred_bytes"] / total_bytes
            if total_bytes
            else 100 * finished_count / group["item_count"]
        )
    return list(groups.values())


@app.get("/api/jobs", response_model=response_model("JobsResponse"))
def jobs(request: Request, vault: dict[str, Any] = Depends(current_vault)):
    locale = _request_locale(request)
    with db() as connection:
        recent_rows = connection.execute(
            """
            SELECT id, path, action, status, message, message_key, message_params,
                   requested_at, updated_at, group_id, group_path, total_bytes,
                   transferred_bytes, pending_until, estimated_cost_eur,
                   estimated_hours, restore_tier, restore_days, approved_at
            FROM jobs WHERE vault_id=%s ORDER BY requested_at DESC LIMIT 50
            """,
            (vault["id"],),
        ).fetchall()
        active_groups = connection.execute(
            """
            SELECT DISTINCT group_id FROM jobs
            WHERE vault_id=%s AND group_id IS NOT NULL
              AND status NOT IN ('completed', 'failed', 'cancelled')
            """,
            (vault["id"],),
        ).fetchall()
        group_ids = [row["group_id"] for row in active_groups]
        group_rows: list[dict[str, Any]] = []
        if group_ids:
            placeholders = ", ".join(["%s"] * len(group_ids))
            group_rows = connection.execute(
                f"""
                SELECT id, path, action, status, message, message_key, message_params,
                       requested_at, updated_at, group_id, group_path, total_bytes,
                       transferred_bytes, pending_until, estimated_cost_eur,
                       estimated_hours, restore_tier, restore_days, approved_at
                FROM jobs WHERE vault_id=%s AND group_id IN ({placeholders})
                """,
                [vault["id"], *group_ids],
            ).fetchall()
    rows_by_id = {row["id"]: row for row in [*recent_rows, *group_rows]}
    rows = sorted(rows_by_id.values(), key=lambda row: str(row["requested_at"]), reverse=True)
    localized_rows = [
        {
            **row,
            "message": present_job_message(row, locale),
        }
        for row in rows
    ]
    return {
        "items": localized_rows,
        "groups": build_job_groups(rows, locale=locale),
        "locale": locale,
    }


@app.post("/api/scan", status_code=202, response_model=response_model("ScanResponse"))
async def start_scan(
    request: Request,
    vault: dict[str, Any] = Depends(current_vault),
):
    if not can_operate(vault["role"]):
        raise HTTPException(403, "Vault is read-only")
    status = runtime_status.get(vault["id"], {})
    if status.get("scanning"):
        return {**_api_message(request, "api.scan_in_progress")}
    asyncio.create_task(asyncio.to_thread(scan_vault, vault))
    return {**_api_message(request, "api.scan_started", name=vault["name"])}


@app.get("/api/catalog/revision", response_model=response_model("CatalogRevisionResponse"))
def catalog_revision_snapshot(
    after_revision: int = Query(0, ge=0),
    vault: dict[str, Any] = Depends(current_vault),
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


@app.get(
    "/api/catalog/events",
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
    vault: dict[str, Any] = Depends(current_vault),
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
    user_id = int(user["id"])
    session = request.state.session
    session_id = str(session["id"])
    resume_after = after_revision
    if last_event_id:
        try:
            resume_after = max(resume_after, int(last_event_id))
        except (TypeError, ValueError):
            raise HTTPException(422, "Last-Event-ID must be an integer revision") from None

    async def event_generator():
        async for chunk in iter_catalog_event_sse(
            vault_id=vault_id,
            user_id=user_id,
            session_id=session_id,
            resume_after=resume_after,
            subscribe=subscribe,
            is_disconnected=request.is_disconnected,
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


def queue_jobs(
    path: str,
    action: str,
    vault_id: int,
    user_id: int,
    is_directory: bool = False,
    *,
    archive_version_id: str | None = None,
    restore_tier: str | None = None,
    restore_days: int | None = None,
    target_storage_class: str | None = None,
    whole_vault: bool = False,
) -> dict[str, Any]:
    if whole_vault:
        logical_path = ""
    else:
        logical_path = safe_relative_path(path).as_posix()
    if action not in {"upload", "recover", "free-space", "rename", "storage-class"}:
        raise HTTPException(422, "Invalid operation")
    if action in {"upload", "recover", "free-space", "rename", "storage-class"}:
        with db() as connection:
            vault_row = connection.execute(
                """
                SELECT source_root, relocation_state, decommission_state
                FROM vaults WHERE id=%s
                """,
                (vault_id,),
            ).fetchone()
        if vault_row is None:
            raise HTTPException(404, "Vault not found")
        if vault_decommission_service.local_work_suspended(vault_row):
            raise HTTPException(409, "Vault is quiesced for decommission")
        if vault_relocation_service.local_work_suspended(vault_row):
            raise HTTPException(409, "Local work is suspended pending relocation scan")
        access = vault_local_access(vault_row["source_root"])
        if action in {"upload", "recover", "free-space", "rename"} and not access.local_operations_allowed:
            raise HTTPException(
                503,
                "Local storage for this vault is unavailable"
                + (f" ({access.volume_health})" if access.volume_health else ""),
            )
    if archive_version_id and (
        action not in {"recover", "storage-class"} or is_directory or whole_vault
    ):
        raise HTTPException(
            422,
            "archive_version_id is only valid for single-file recover or storage-class",
        )
    group_id = uuid.uuid4().hex
    estimated_cost_eur = None
    estimated_hours = None
    resolved_tier = None
    resolved_days = None
    resolved_target_class = None
    if action == "storage-class":
        try:
            resolved_target_class = validate_manual_target_class(
                target_storage_class or ""
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    with db() as connection:
        catalog = ArchiveCatalog(connection)
        if action in {"recover", "storage-class"} and not is_directory and not whole_vault:
            versions = catalog.list_versions(vault_id, logical_path)
            if action == "recover":
                selectable = [row for row in versions if row["recoverable"]]
            else:
                selectable = [
                    row
                    for row in versions
                    if row.get("availability") == "available"
                ]
            if archive_version_id:
                selected = next(
                    (
                        row
                        for row in versions
                        if row["id"] == archive_version_id
                    ),
                    None,
                )
                if selected is None or (
                    action == "recover" and not selected["recoverable"]
                ):
                    raise HTTPException(
                        409,
                        (
                            "The selected Archive Version is not recoverable"
                            if action == "recover"
                            else "The selected Archive Version is not available"
                        ),
                    )
                if action == "storage-class" and selected.get("availability") != "available":
                    raise HTTPException(
                        409, "The selected Archive Version is not available"
                    )
            elif len(selectable) == 1:
                archive_version_id = selectable[0]["id"]
                selected = selectable[0]
            elif selectable:
                selected = selectable[0]
                archive_version_id = selected["id"]
            else:
                selected = None
            if action == "storage-class" and selected is not None:
                current_class = normalize_storage_class(selected.get("storage_class"))
                if current_class == resolved_target_class:
                    raise HTTPException(
                        409,
                        "Archive Version is already in the requested storage class",
                    )
            if (
                action in {"recover", "storage-class"}
                and selected is not None
                and (
                    (
                        action == "recover"
                        and storage_class_requires_restore(
                            selected.get("storage_class")
                        )
                    )
                    or (
                        action == "storage-class"
                        and source_requires_restore_for_class_change(
                            selected.get("storage_class"),
                            restore_state=selected.get("restore_state"),
                        )
                    )
                )
            ):
                resolved_days = int(
                    restore_days
                    if restore_days is not None
                    else _runtime_settings().restore_days
                )
                resolved_tier = normalize_restore_tier(
                    restore_tier or _runtime_settings().restore_tier,
                    storage_class=str(selected.get("storage_class") or ""),
                )
                estimate = estimate_restore(
                    size_bytes=int(selected.get("size") or 0),
                    storage_class=str(selected.get("storage_class") or ""),
                    tier=resolved_tier,
                    days=resolved_days,
                )
                estimated_cost_eur = estimate.estimated_cost_eur
                estimated_hours = estimate.estimated_hours
        job_ids, total_bytes, eligible_count = catalog.queue_jobs(
            vault_id=vault_id,
            path=logical_path,
            action=action,
            requested_by=user_id,
            requested_at=now_iso(),
            group_id=group_id,
            is_directory=is_directory,
            archive_version_id=archive_version_id,
            restore_tier=resolved_tier,
            restore_days=resolved_days,
            estimated_cost_eur=estimated_cost_eur,
            estimated_hours=estimated_hours,
            target_storage_class=resolved_target_class,
            whole_vault=whole_vault,
        )
        skipped_same_class = int(getattr(catalog, "last_skipped_same_class", 0) or 0)
        if not eligible_count:
            if action == "storage-class" and skipped_same_class:
                raise HTTPException(
                    409,
                    "All selected Archive Versions are already in the requested storage class",
                )
            raise HTTPException(409, "No files are eligible for this operation")
        quota = catalog.last_quota_evaluation.as_dict()
    if not job_ids:
        raise HTTPException(409, "An operation is already running on the selected files")
    result = {
        "group_id": group_id,
        "job_ids": job_ids,
        "item_count": len(job_ids),
        "total_bytes": total_bytes,
        "quota": quota,
        "archive_version_id": archive_version_id,
        "restore_tier": resolved_tier,
        "restore_days": resolved_days,
        "estimated_cost_eur": estimated_cost_eur,
        "estimated_hours": estimated_hours,
    }
    if action == "storage-class":
        result["target_storage_class"] = resolved_target_class
        result["skipped_same_class"] = skipped_same_class
        warning = cold_class_warning(resolved_target_class or "")
        if warning:
            result["cost_warning"] = warning
        result["requires_restore"] = bool(resolved_tier)
        if resolved_tier:
            result["restore_tier"] = resolved_tier
            result["restore_days"] = resolved_days
            result["estimated_cost_eur"] = estimated_cost_eur
            result["estimated_hours"] = estimated_hours
    return result


@app.get(
    "/api/files/versions",
    response_model=response_model("FileVersionsResponse"),
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def file_versions(
    path: str = Query(),
    vault: dict[str, Any] = Depends(current_vault),
):
    logical_path = safe_relative_path(path).as_posix()
    with db() as connection:
        versions = ArchiveCatalog(connection).list_versions(vault["id"], logical_path)
    recoverable = [row for row in versions if row["recoverable"]]
    default_version_id = recoverable[0]["id"] if recoverable else None
    return {
        "path": logical_path,
        "items": versions,
        "recoverable_count": len(recoverable),
        "default_archive_version_id": default_version_id,
        "supported_restore_tiers": list(SUPPORTED_RESTORE_TIERS),
        "default_restore_tier": _runtime_settings().restore_tier,
        "default_restore_days": _runtime_settings().restore_days,
    }


@app.post(
    "/api/recover/estimate",
    response_model=response_model("RecoverEstimateResponse"),
    responses={**LOGICAL_PATH_ERROR_RESPONSES, **SCOPED_NOT_FOUND_RESPONSES},
)
def recover_estimate(
    action: RecoverEstimateRequest,
    vault: dict[str, Any] = Depends(current_vault),
):
    logical_path = safe_relative_path(action.path).as_posix()
    with db() as connection:
        versions = ArchiveCatalog(connection).list_versions(vault["id"], logical_path)
    if action.archive_version_id:
        selected = next(
            (row for row in versions if row["id"] == action.archive_version_id),
            None,
        )
    else:
        selected = next((row for row in versions if row["recoverable"]), None)
    if selected is None:
        raise HTTPException(404, "No recoverable Archive Version found")
    if not selected["recoverable"]:
        raise HTTPException(409, "The selected Archive Version is not recoverable")
    requires_restore = storage_class_requires_restore(selected.get("storage_class"))
    days = int(
        action.restore_days
        if action.restore_days is not None
        else _runtime_settings().restore_days
    )
    tier = normalize_restore_tier(
        action.restore_tier or _runtime_settings().restore_tier,
        storage_class=str(selected.get("storage_class") or ""),
    )
    estimate_payload = None
    high_impact = False
    if requires_restore:
        with db() as connection:
            book = get_active_price_book(connection)
        from .services.cost_estimates import estimate_restore_cost

        priced = estimate_restore_cost(
            book,
            size_bytes=int(selected.get("size") or 0),
            storage_class=str(selected.get("storage_class") or ""),
            tier=tier,
            days=days,
        )
        high_impact = is_high_impact_restore(
            size_bytes=priced.size_bytes,
            estimated_cost_eur=priced.estimated_cost_eur,
            size_threshold_gib=_runtime_settings().restore_high_impact_gib,
            cost_threshold_eur=_runtime_settings().restore_high_impact_eur,
        )
        estimate_payload = {
            "tier": priced.tier,
            "days": days,
            "estimated_cost_eur": priced.estimated_cost_eur,
            "estimated_hours": priced.estimated_hours,
            "pricing_note": str(
                priced.assumptions.get(
                    "disclaimer",
                    "Internal estimate from configured price data; not an AWS Billing quote.",
                )
            ),
            "price_book_id": priced.price_book_id,
            "price_book_name": priced.price_book_name,
            "pricing_effective_at": priced.pricing_effective_at,
            "assumptions": priced.assumptions,
            "restore_object_irreversible": True,
        }
    return {
        "path": logical_path,
        "archive_version_id": selected["id"],
        "storage_class": selected.get("storage_class"),
        "requires_restore": requires_restore,
        "restore_object_irreversible": requires_restore,
        "high_impact": high_impact,
        "estimate": estimate_payload,
    }


@app.post(
    "/api/upload",
    status_code=202,
    response_model=JsonObjectResponse,
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def upload(
    action: FileAction,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
):
    if not can_operate(vault["role"]):
        raise HTTPException(403, "Vault is read-only")
    queued = queue_jobs(action.path, "upload", vault["id"], user["id"], action.is_directory)
    return {**queued, **_api_message(request, "api.upload_started")}


def cancel_job_group(group_id: str, job_action: str, vault: dict[str, Any]):
    if not can_operate(vault["role"]):
        raise HTTPException(403, "Vault is read-only")
    message_keys = {
        "upload": "job.upload_stopped",
        "recover": "job.recovery_stopped",
        "free-space": "job.cleanup_stopped",
        "rename": "job.rename_stopped",
        "cloud-archive": "job.cloud_archive_stopped",
        "cloud-purge": "job.cloud_purge_stopped",
        "storage-class": "job.storage_class_stopped",
    }
    if job_action not in message_keys:
        raise HTTPException(422, "Invalid operation")
    message_key = message_keys[job_action]
    message = translate(message_key, locale=DEFAULT_LOCALE)
    with db() as connection:
        if job_action in {"cloud-archive", "cloud-purge"}:
            if not is_owner(vault["role"]):
                raise HTTPException(
                    403,
                    "Only the primary owner can cancel cloud deletion",
                )
            try:
                cancelled = cloud_deletion_service.cancel_cloud_deletion(
                    connection,
                    vault_id=vault["id"],
                    group_id=group_id,
                    actor_user_id=vault["member_user_id"],
                    cancelled_at=now_iso(),
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            cancel_jobs(
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
            """
            SELECT id FROM jobs
            WHERE vault_id=%s AND group_id=%s AND action=%s
              AND status NOT IN ('completed', 'failed', 'cancelled')
            """,
            (vault["id"], group_id, job_action),
        ).fetchall()
        job_ids = [int(row["id"]) for row in rows]
        if not job_ids:
            raise HTTPException(409, "The operation is no longer running")
        automatic_cleanup_rows = []
        if job_action == "free-space":
            automatic_cleanup_rows = connection.execute(
                """
                SELECT id, path, requested_by, archive_version_id
                FROM jobs
                WHERE vault_id=%s AND group_id=%s AND action='free-space'
                  AND origin='automatic'
                  AND status NOT IN ('completed', 'failed', 'cancelled')
                """,
                (vault["id"], group_id),
            ).fetchall()
        cancel_jobs(job_ids)
        placeholders = ", ".join(["%s"] * len(job_ids))
        connection.execute(
            f"""
            UPDATE jobs
            SET status='cancelled',
                message=%s,
                message_key=%s,
                message_params=%s,
                updated_at=%s
            WHERE id IN ({placeholders})
              AND status NOT IN ('completed', 'failed', 'cancelled')
            """,
            [message, message_key, "{}", now_iso(), *job_ids],
        )
        # Do not clear archive_versions.restore_state on recover cancel:
        # RestoreObject cannot be cancelled after AWS accepts it, and a later
        # recover must be able to resume polling (REQ-030 / BUG-018).
        for row in automatic_cleanup_rows:
            audit_event_store.record_audit_event(
                connection,
                event="local_cleanup.cancelled",
                actor_user_id=vault.get("member_user_id"),
                vault_id=vault["id"],
                job_id=int(row["id"]),
                outcome="cancelled",
                path=row["path"],
                archive_version_id=row["archive_version_id"],
            )
            notification_service.enqueue_notification(
                connection,
                user_id=int(row["requested_by"]),
                event="local_cleanup.cancelled",
                title="Automatic local cleanup cancelled",
                body=f"The Local Copy cleanup for {row['path']} was cancelled.",
                vault_id=vault["id"],
                job_id=int(row["id"]),
            )
    return {
        "message": message,
        "cancelled_count": len(job_ids),
    }


@app.post("/api/jobs/cancel", response_model=response_model("JobCancelResponse"))
def cancel_job(
    action: JobCancelAction,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
):
    return cancel_job_group(action.group_id, action.action, vault)


@app.post("/api/upload/cancel", response_model=response_model("JobCancelResponse"))
def cancel_upload(
    action: GroupCancelAction,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
):
    """Backward-compatible endpoint for clients using the old upload route."""
    return cancel_job_group(action.group_id, "upload", vault)


@app.post(
    "/api/recover",
    status_code=202,
    response_model=JsonObjectResponse,
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def recover(
    action: FileAction,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
):
    if not can_operate(vault["role"]):
        raise HTTPException(403, "Vault is read-only")
    queued = queue_jobs(
        action.path,
        "recover",
        vault["id"],
        user["id"],
        action.is_directory,
        archive_version_id=action.archive_version_id,
        restore_tier=action.restore_tier,
        restore_days=action.restore_days,
    )
    return {**queued, **_api_message(request, "api.recovery_started")}


@app.post("/api/recover/approve", status_code=202, response_model=JsonObjectResponse)
def approve_recover(
    action: RecoverApproveAction,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    if not is_owner(vault["role"]):
        raise HTTPException(403, "Only the primary owner can approve high-impact restores")
    timestamp = now_iso()
    with db() as connection:
        rows = connection.execute(
            """
            SELECT id FROM jobs
            WHERE vault_id=%s AND group_id=%s AND action='recover'
              AND status='pending_approval'
            """,
            (vault["id"], action.group_id),
        ).fetchall()
        if not rows:
            raise HTTPException(409, "No high-impact restore is waiting for approval")
        job_ids = [int(row["id"]) for row in rows]
        placeholders = ", ".join(["%s"] * len(job_ids))
        connection.execute(
            f"""
            UPDATE jobs
            SET status='queued',
                approved_by=%s,
                approved_at=%s,
                message=%s,
                updated_at=%s
            WHERE id IN ({placeholders})
            """,
            [
                user["id"],
                timestamp,
                "High-impact restore approved; RestoreObject cannot be cancelled after AWS accepts it",
                timestamp,
                *job_ids,
            ],
        )
    return {
        "group_id": action.group_id,
        "job_ids": job_ids,
        "message": "High-impact restore approved",
    }


@app.post(
    "/api/free-space",
    status_code=202,
    response_model=JsonObjectResponse,
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def free_space(
    action: FileAction,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    if not is_owner(vault["role"]):
        raise HTTPException(403, "Vault is read-only")
    if not _runtime_settings().allow_local_delete:
        raise HTTPException(403, "Freeing local space is disabled")
    queued = queue_jobs(
        action.path, "free-space", vault["id"], user["id"], action.is_directory
    )
    return {**queued, **_api_message(request, "api.free_space_started")}


@app.get("/api/storage-classes", response_model=response_model("StorageClassesResponse"))
def get_storage_classes(
    _: dict[str, Any] = Depends(current_user),
    __: dict[str, Any] = Depends(current_vault),
):
    with db() as connection:
        book = get_active_price_book(connection)
    return list_storage_class_options(book)


@app.post(
    "/api/storage-class",
    status_code=202,
    response_model=JsonObjectResponse,
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def change_storage_class(
    action: StorageClassChangeRequest,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    if action.whole_vault:
        if not is_owner(vault["role"]):
            raise HTTPException(
                403, "Only the Vault owner can change storage class for the whole Vault"
            )
    elif not can_operate(vault["role"]):
        raise HTTPException(403, "Vault is read-only")
    queued = queue_jobs(
        action.path,
        "storage-class",
        vault["id"],
        user["id"],
        action.is_directory,
        archive_version_id=action.archive_version_id,
        target_storage_class=action.target_storage_class,
        whole_vault=action.whole_vault,
        restore_tier=action.restore_tier,
        restore_days=action.restore_days,
    )
    if action.pin_after and not action.whole_vault:
        from .services.lifecycle_pins import set_lifecycle_pin
        from .services.lifecycle_policies import refresh_desired_policies

        logical_path = (
            ""
            if action.whole_vault
            else safe_relative_path(action.path).as_posix()
        )
        if logical_path:
            with db() as connection:
                set_lifecycle_pin(
                    connection,
                    vault_id=vault["id"],
                    path=logical_path,
                    is_directory=action.is_directory,
                    pinned_by=user["id"],
                    pinned_at=now_iso(),
                )
                refresh_desired_policies(connection, vault["id"])
            queued["pin_after"] = True
    return {**queued, **_api_message(request, "api.storage_class_started")}


@app.put(
    "/api/lifecycle-pin",
    response_model=JsonObjectResponse,
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def update_lifecycle_pin(
    action: LifecyclePinRequest,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    if not can_operate(vault["role"]):
        raise HTTPException(403, "Vault is read-only")
    logical_path = safe_relative_path(action.path).as_posix()
    from .services.lifecycle_pins import clear_lifecycle_pin, set_lifecycle_pin
    from .services.lifecycle_policies import refresh_desired_policies

    with db() as connection:
        if action.pinned:
            set_lifecycle_pin(
                connection,
                vault_id=vault["id"],
                path=logical_path,
                is_directory=action.is_directory,
                pinned_by=user["id"],
                pinned_at=now_iso(),
            )
        else:
            clear_lifecycle_pin(
                connection, vault_id=vault["id"], path=logical_path
            )
        refresh_desired_policies(connection, vault["id"])
    return {
        "path": logical_path,
        "is_directory": action.is_directory,
        "pinned": action.pinned,
        **_api_message(request, "api.lifecycle_pin_updated"),
    }


def _cloud_deletion_paths(action: CloudDeletionPreviewRequest | CloudArchiveRequest | CloudPurgeRequest) -> list[str]:
    if action.paths:
        return list(action.paths)
    return [action.path]


@app.get("/api/vault/cloud-deletion", response_model=response_model("CloudDeletionSettings"))
def get_cloud_deletion_setting(vault: dict[str, Any] = Depends(current_vault)):
    with db() as connection:
        enabled = cloud_deletion_service.is_cloud_deletion_enabled(
            connection, vault["id"]
        )
    return {
        "enabled": enabled,
        "purge_delay_seconds": _runtime_settings().cloud_purge_delay_seconds,
        "delete_marker_explanation": cloud_deletion_service.delete_marker_explanation(),
        "generated_phrase": cloud_deletion_service.generate_confirmation_phrase(),
        "accepted_single_identity_risk": (
            "This installation may use one IAM identity for ordinary archive "
            "operations and permanent DeleteObjectVersion calls. Restrict that "
            "identity, audit every purge, and prefer a dedicated deletion role "
            "when your threat model requires stricter separation."
        ),
    }


@app.put("/api/vault/cloud-deletion", response_model=response_model("CloudDeletionSettings"))
def update_cloud_deletion_setting(
    action: CloudDeletionSettingUpdate,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    if not is_owner(vault["role"]):
        raise HTTPException(403, "Only the primary owner can change cloud deletion")
    try:
        with db() as connection:
            enabled = cloud_deletion_service.set_cloud_deletion_enabled(
                connection,
                vault_id=vault["id"],
                enabled=action.enabled,
                actor_user_id=user["id"],
            )
    except cloud_deletion_service.CloudDeletionDisabled as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"enabled": enabled}


@app.post("/api/cloud-deletion/preview", response_model=response_model("CloudDeletionPreview"))
def preview_cloud_deletion(
    action: CloudDeletionPreviewRequest,
    vault: dict[str, Any] = Depends(current_vault),
):
    if not is_owner(vault["role"]):
        raise HTTPException(403, "Only the primary owner can preview cloud deletion")
    with db() as connection:
        preview = cloud_deletion_service.preview_selection(
            connection,
            vault_id=vault["id"],
            paths=_cloud_deletion_paths(action),
            is_directory=action.is_directory,
        )
    return {
        **preview.as_dict(),
        "delete_marker_explanation": cloud_deletion_service.delete_marker_explanation(),
    }


@app.post("/api/cloud-archive", status_code=202, response_model=JsonObjectResponse)
def cloud_archive(
    action: CloudArchiveRequest,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    if not is_owner(vault["role"]):
        raise HTTPException(403, "Only the primary owner can archive in the cloud")
    try:
        with db() as connection:
            scheduled = cloud_deletion_service.schedule_cloud_archive(
                connection,
                vault_id=vault["id"],
                paths=_cloud_deletion_paths(action),
                is_directory=action.is_directory,
                actor_user_id=user["id"],
                requested_at=now_iso(),
            )
    except cloud_deletion_service.CloudDeletionDisabled as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "group_id": scheduled.group_id,
        "job_ids": scheduled.job_ids,
        "preview": scheduled.preview.as_dict(),
        "delete_marker_explanation": cloud_deletion_service.delete_marker_explanation(),
        **_api_message(request, "api.cloud_archive_started"),
    }


@app.post("/api/cloud-purge", status_code=202, response_model=JsonObjectResponse)
def cloud_purge(
    action: CloudPurgeRequest,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    if not is_owner(vault["role"]):
        raise HTTPException(
            403,
            "Only the primary owner can approve or execute permanent purge",
        )
    try:
        with db() as connection:
            scheduled = cloud_deletion_service.schedule_cloud_purge(
                connection,
                vault_id=vault["id"],
                paths=_cloud_deletion_paths(action),
                is_directory=action.is_directory,
                actor_user_id=user["id"],
                requested_at=now_iso(),
                confirmation=action.confirmation,
                reason=action.reason,
                generated_phrase=action.generated_phrase,
                delay_seconds=_runtime_settings().cloud_purge_delay_seconds,
            )
    except cloud_deletion_service.CloudDeletionDisabled as exc:
        raise HTTPException(403, str(exc)) from exc
    except cloud_deletion_service.ConfirmationRequired as exc:
        raise HTTPException(422, str(exc)) from exc
    except cloud_deletion_service.ReasonRequired as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "group_id": scheduled.group_id,
        "job_ids": scheduled.job_ids,
        "preview": scheduled.preview.as_dict(),
        "pending_until": scheduled.pending_until,
        "status": "pending_delay",
        **_api_message(request, "api.cloud_purge_scheduled"),
    }


@app.post("/api/cloud-purge/accelerate", status_code=202, response_model=JsonObjectResponse)
def accelerate_cloud_purge(
    action: GroupCancelAction,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    """Skip the cancellable delay and queue permanent purge immediately."""
    if not is_owner(vault["role"]):
        raise HTTPException(
            403,
            "Only the primary owner can accelerate permanent purge",
        )
    try:
        with db() as connection:
            accelerated = cloud_deletion_service.accelerate_cloud_purge(
                connection,
                vault_id=vault["id"],
                group_id=action.group_id,
                actor_user_id=user["id"],
                accelerated_at=now_iso(),
            )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "group_id": accelerated.group_id,
        "accelerated_count": accelerated.accelerated_count,
        "status": "queued",
        **_api_message(request, "api.cloud_purge_accelerated"),
    }


@app.get("/api/admin/users", response_model=response_model("AdminUsersResponse"))
def admin_users(_: dict[str, Any] = Depends(admin_user)):
    """List every User with vault membership and authentication capabilities."""
    with db() as connection:
        return {"items": user_admin_service.list_users(connection)}


@app.post("/api/admin/users", status_code=201, response_model=response_model("AdminUser"))
def create_user(
    action: UserCreate,
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    username = action.username.strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]+", username):
        raise HTTPException(422, "The username can contain letters, numbers, periods, hyphens, and underscores")
    try:
        with db() as connection:
            return user_admin_service.create_user(
                connection,
                username=username,
                display_name=action.display_name.strip(),
                password_hash=(
                    None if action.password is None else hash_password(action.password)
                ),
                is_admin=action.is_admin,
                actor_user_id=admin["id"],
            )
    except INTEGRITY_ERRORS:
        raise HTTPException(409, "Username is already in use")


@app.get("/api/admin/invites", response_model=response_model("AdminInvitesResponse"))
def admin_invites(_: dict[str, Any] = Depends(admin_user)):
    """List Invites that can still be redeemed, without any token material."""
    with db() as connection:
        return {"items": list_pending_invites(connection)}


@app.post("/api/admin/invites", status_code=201, response_model=JsonObjectResponse)
def create_invite_endpoint(
    action: InviteCreate,
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    try:
        with db() as connection:
            token = create_invite(
                connection,
                target_user_id=action.target_user_id,
                created_by=admin["id"],
            )
    except ValueError:
        raise HTTPException(404, "Target user not found")
    return {"token": token}


@app.post("/api/admin/invites/{invite_id}/revoke", response_model=JsonObjectResponse)
def revoke_invite_endpoint(
    invite_id: int,
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    """Withdraw a pending Invite so its token can never be redeemed."""
    try:
        with db() as connection:
            revoked = revoke_invite(
                connection, invite_id=invite_id, actor_user_id=admin["id"]
            )
            audit_event_store.record_audit_event(
                connection,
                event="admin_invite_revoked",
                actor_user_id=admin["id"],
                outcome="success",
                visibility="admin",
                invite_id=revoked["id"],
                target_user_id=revoked["target_user_id"],
            )
    except InviteError as exc:
        raise _invite_revocation_http_error(exc) from exc
    return revoked


@app.patch("/api/admin/users/{user_id}", response_model=JsonObjectResponse)
def update_user(
    user_id: int,
    action: UserUpdate,
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    """Change a User's global role, activation, display name or password."""
    try:
        with db() as connection:
            return user_admin_service.update_user(
                connection,
                user_id=user_id,
                actor_user_id=admin["id"],
                active=action.active,
                is_admin=action.is_admin,
                display_name=(
                    None
                    if action.display_name is None
                    else action.display_name.strip()
                ),
                password_hash=(
                    None if action.password is None else hash_password(action.password)
                ),
            )
    except user_admin_service.AdministrationError as exc:
        raise _administration_http_error(exc) from exc


@app.get("/api/admin/users/{user_id}/identities", response_model=response_model("AdminIdentitiesResponse"))
def admin_user_identities(user_id: int, _: dict[str, Any] = Depends(admin_user)):
    """List the external Identities linked to one User."""
    try:
        with db() as connection:
            return {
                "items": user_admin_service.list_identities(
                    connection, user_id=user_id
                )
            }
    except user_admin_service.AdministrationError as exc:
        raise _administration_http_error(exc) from exc


@app.delete("/api/admin/users/{user_id}/identities/{identity_id}", response_model=JsonObjectResponse)
def admin_unlink_identity(
    user_id: int,
    identity_id: int,
    confirm: bool = False,
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    """Unlink one external Identity after explicit confirmation."""
    try:
        with db() as connection:
            return {
                "items": user_admin_service.unlink_identity(
                    connection,
                    user_id=user_id,
                    identity_id=identity_id,
                    actor_user_id=admin["id"],
                    confirmed=confirm,
                )
            }
    except user_admin_service.AdministrationError as exc:
        raise _administration_http_error(exc) from exc



@app.get("/api/admin/source-volumes", response_model=response_model("SourceVolumeInventoryResponse"))
def admin_source_volumes(user: dict[str, Any] = Depends(admin_user)):
    """Operator inventory of discovered Source Volumes (issue #148)."""
    return {"items": source_volume_inventory()}


_SOURCE_AREA_ERROR_STATUS: dict[str, tuple[int, str]] = {
    "reason_required": (422, "A reason between 3 and 500 characters is required"),
    "user_not_found": (404, "User not found"),
    "not_found": (404, "Source Area not found"),
    "volume_not_found": (404, "Source Volume not found"),
    "volume_unavailable": (409, "Source Volume is not available for assignment"),
    "invalid_volume": (422, "Source Volume is not assignable"),
    "invalid_path": (422, "Source Area path is invalid"),
    "path_missing": (404, "Source Area directory does not exist"),
    "overlap": (409, "Source Area overlaps an existing grant"),
    "occupied": (409, "Occupied Vault roots cannot be browsed"),
    "forbidden": (403, "Path is outside the viewer's Source Areas"),
}


def _source_area_http_error(exc: source_areas_service.SourceAreaError) -> HTTPException:
    status_code, message = _SOURCE_AREA_ERROR_STATUS.get(
        exc.reason, (400, "Request could not be completed")
    )
    return HTTPException(status_code, message)


@app.post("/api/admin/source-areas", status_code=201, response_model=response_model("SourceAreaGrant"))
def admin_assign_source_area(
    action: AdminSourceAreaAssign,
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    """Assign an exclusive Source Area to one User (issue #149)."""
    try:
        with db() as connection:
            return source_areas_service.assign_source_area(
                connection,
                user_id=action.user_id,
                volume_alias=action.volume_alias,
                relative_path=action.relative_path,
                actor_user_id=admin["id"],
                reason=action.reason,
            )
    except source_areas_service.SourceAreaError as exc:
        raise _source_area_http_error(exc) from exc


@app.delete("/api/admin/source-areas/{source_area_id}", response_model=JsonObjectResponse)
def admin_revoke_source_area(
    source_area_id: int,
    reason: str = Query(min_length=3, max_length=500),
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    """Revoke one Source Area without altering existing Vaults (issue #149)."""
    try:
        with db() as connection:
            return source_areas_service.revoke_source_area(
                connection,
                source_area_id=source_area_id,
                actor_user_id=admin["id"],
                reason=reason,
            )
    except source_areas_service.SourceAreaError as exc:
        raise _source_area_http_error(exc) from exc


@app.get("/api/admin/source-areas", response_model=response_model("SourceAreaListResponse"))
def admin_list_source_areas(
    user_id: int | None = None,
    volume_alias: str | None = None,
    _: dict[str, Any] = Depends(admin_user),
):
    """List Source Areas, optionally filtered by User or Source Volume."""
    with db() as connection:
        if user_id is not None:
            items = source_areas_service.list_source_areas_for_user(
                connection, user_id=user_id
            )
        elif volume_alias is not None:
            items = source_areas_service.list_source_areas_for_volume(
                connection, volume_alias=volume_alias
            )
        else:
            items = source_areas_service.list_all_source_areas(connection)
    return {"items": items}


@app.get("/api/admin/source-volumes/{volume_alias}/browse", response_model=response_model("SourceDirectoryBrowseResponse"))
def admin_browse_source_volume(
    volume_alias: str,
    path: str = Query(default=""),
    purpose: str = Query(default="grant", pattern="^(grant|adopt)$"),
    admin: dict[str, Any] = Depends(admin_user),
):
    """Admin lazy directory browser for Source Area assignment."""
    try:
        with db() as connection:
            return source_areas_service.browse_source_directories(
                connection,
                volume_alias=volume_alias,
                relative_path=path,
                viewer_user_id=admin["id"],
                viewer_is_admin=True,
                purpose=purpose,
            )
    except source_areas_service.SourceAreaError as exc:
        raise _source_area_http_error(exc) from exc


@app.get("/api/source-areas", response_model=response_model("SourceAreaListResponse"))
def list_my_source_areas(user: dict[str, Any] = Depends(current_user)):
    """Source Areas granted to the authenticated User."""
    with db() as connection:
        return {
            "items": source_areas_service.list_source_areas_for_user(
                connection, user_id=user["id"]
            )
        }


@app.get("/api/source-volumes/{volume_alias}/browse", response_model=response_model("SourceDirectoryBrowseResponse"))
def browse_my_source_volume(
    volume_alias: str,
    path: str = Query(default=""),
    user: dict[str, Any] = Depends(current_user),
):
    """User lazy directory browser scoped to their Source Areas."""
    try:
        with db() as connection:
            return source_areas_service.browse_source_directories(
                connection,
                volume_alias=volume_alias,
                relative_path=path,
                viewer_user_id=user["id"],
                viewer_is_admin=False,
            )
    except source_areas_service.SourceAreaError as exc:
        raise _source_area_http_error(exc) from exc


@app.get("/api/admin/vaults", response_model=response_model("AdminVaultsResponse"))
def admin_vaults(_: dict[str, Any] = Depends(admin_user)):
    with db() as connection:
        return {"items": list_admin_vaults(connection)}


@app.post("/api/admin/vaults/{vault_id}/decommission/preview", response_model=response_model("VaultDecommissionPreview"))
def preview_admin_vault_decommission(
    vault_id: int,
    action: VaultDecommissionPreview,
    _: dict[str, Any] = Depends(admin_user),
):
    return _preview_vault_decommission(vault_id, action)


@app.get("/api/admin/vaults/{vault_id}/decommission/status", response_model=response_model("VaultDecommissionStatus"))
def admin_vault_decommission_status(
    vault_id: int,
    _: dict[str, Any] = Depends(admin_user),
):
    try:
        with db() as connection:
            return vault_decommission_service.operation_status(
                connection, vault_id=vault_id
            )
    except vault_decommission_service.VaultDecommissionError as exc:
        raise _vault_decommission_http_error(exc) from exc


@app.post("/api/admin/vaults/{vault_id}/decommission", status_code=202, response_model=response_model("VaultDecommissionStatus"))
def start_admin_vault_decommission(
    vault_id: int,
    action: VaultDecommissionStart,
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    return _start_vault_decommission(
        vault_id=vault_id,
        action=action,
        actor=admin,
        actor_is_admin=True,
    )


@app.post("/api/admin/vaults/{vault_id}/decommission/cloud-purge/cancel", response_model=JsonObjectResponse)
def cancel_admin_vault_decommission_cloud_purge(
    vault_id: int,
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    try:
        with db() as connection:
            return vault_decommission_service.cancel_pending_cloud_purge(
                connection,
                vault_id=vault_id,
                actor_user_id=int(admin["id"]),
            )
    except vault_decommission_service.VaultDecommissionError as exc:
        raise _vault_decommission_http_error(exc) from exc


@app.post("/api/admin/vaults", status_code=201, response_model=response_model("AdminVault"))
def create_vault(
    action: VaultCreate,
    background_tasks: BackgroundTasks,
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    with db() as connection:
        owner = connection.execute(
            "SELECT 1 FROM users WHERE id=%s AND active=TRUE",
            (action.owner_user_id,),
        ).fetchone()
    if not owner:
        raise HTTPException(404, "Owner not found")

    try:
        vault = create_admin_vault(
            action.owner_user_id,
            action.name,
            action.slug,
            encryption_mode=action.encryption_mode,
            creation_mode=action.creation_mode,
            volume_alias=action.volume_alias,
            relative_path=action.relative_path,
        )
    except VaultSlugTaken as exc:
        raise HTTPException(409, str(exc)) from exc
    except VaultProvisioningUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except InvalidVaultName as exc:
        raise HTTPException(422, str(exc)) from exc
    except VaultAdoptionError as exc:
        raise HTTPException(422, str(exc)) from exc
    except VaultCreationError as exc:
        raise HTTPException(409, str(exc)) from exc

    if action.creation_mode == "adopt":
        # The admin service result is a public projection; scan_vault needs
        # only this opaque identifier and reloads the persisted Vault itself.
        background_tasks.add_task(scan_vault, {"id": vault["id"]})

    notify_owner_of_admin_action(
        "vault_created",
        vault_id=vault["id"],
        owner_user_id=action.owner_user_id,
        actor_id=admin["id"],
        reason=action.reason,
    )
    return vault


_VAULT_RELOCATION_ERROR_STATUS: dict[str, tuple[int, str]] = {
    "not_found": (404, "Vault not found"),
    "reason_required": (422, "A reason between 3 and 500 characters is required"),
    "invalid_path": (422, "Relocation destination is invalid"),
    "not_directory": (422, "Relocation destination is not a directory"),
    "unsupported_root": (409, "Only custom Source Volume roots can be relocated"),
    "source_present": (409, "Original Vault root still exists; rebind is forbidden"),
    "source_inaccessible": (409, "Original Vault root cannot be proven missing"),
    "different_volume": (409, "Destination must be the same Source Volume"),
    "volume_unavailable": (409, "Expected Source Volume is unavailable"),
    "symlink": (409, "Relocation paths cannot contain symbolic links"),
    "inaccessible": (409, "Relocation destination is inaccessible"),
    "overlap": (409, "Relocation destination overlaps another Vault"),
    "active_jobs": (409, "Vault has active Jobs or a scan"),
    "identity_ambiguous": (409, "Vault root identity is ambiguous; relocation is unavailable"),
    "identity_mismatch": (409, "Destination is not the enrolled Vault directory"),
    "relocation_in_progress": (409, "Vault relocation already requires a full scan"),
    "decommission_in_progress": (409, "Vault decommission prevents relocation"),
}


@app.post("/api/admin/vaults/{vault_id}/relocate", response_model=response_model("AdminVaultRelocationResponse"))
def admin_relocate_vault_root(
    vault_id: int,
    action: VaultRelocate,
    background_tasks: BackgroundTasks,
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    """Relocate, never rebind, a missing root on its verified Source Volume."""
    # Serialize the runtime snapshot, relocation transaction, and scan start.
    # Without this lock a scan can begin after the ``busy`` snapshot but before
    # relocation publishes its persistent suspension state.
    relocation_lock = scan_lock_for_vault(vault_id)
    if not relocation_lock.acquire(blocking=False):
        status_code, message = _VAULT_RELOCATION_ERROR_STATUS["active_jobs"]
        raise HTTPException(status_code, message)
    try:
        with status_lock:
            busy = bool(runtime_status.get(vault_id, {}).get("scanning"))
        try:
            with db() as connection:
                vault = vault_relocation_service.relocate_vault_root(
                    connection,
                    vault_id=vault_id,
                    volume_alias=action.volume_alias,
                    relative_path=action.relative_path,
                    actor_user_id=int(admin["id"]),
                    reason=action.reason,
                    runtime_busy=busy,
                )
        except vault_relocation_service.VaultRelocationError as exc:
            status_code, message = _VAULT_RELOCATION_ERROR_STATUS.get(
                exc.reason, (400, "Vault relocation could not be completed")
            )
            raise HTTPException(status_code, message) from exc
    finally:
        relocation_lock.release()
    # BackgroundTasks registration is non-I/O and cannot partially dispatch.
    # A crash before execution is recovered by scan_all_vaults on next start;
    # relocation_state remains scan_required until local scan success.
    background_tasks.add_task(scan_vault, dict(vault))
    return {
        "vault_id": vault_id,
        "source_root": vault["source_root"],
        "relocation_state": vault["relocation_state"],
        "full_scan_required": True,
    }


@app.post("/api/admin/vaults/{vault_id}/recovery/export", response_model=JsonObjectResponse)
def admin_export_vault_recovery_secret(
    vault_id: int,
    action: RecoveryExportRequest,
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM vaults WHERE id=%s", (vault_id,)
        ).fetchone()
        owner = primary_owner(connection, vault_id) if row else None
    if not row or row["encryption_mode"] != "crypt":
        raise HTTPException(404, "Crypt vault not found")
    if not owner:
        raise HTTPException(409, "Vault has no primary owner")
    try:
        export = export_recovery_secret(
            row,
            actor_id=admin["id"],
            reason=action.reason,
            notify_owner_user_id=int(owner["user_id"]),
            admin_override=True,
        )
    except RecoveryError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"recovery_export": export}


def _quota_payload(connection: Any, vault_id: int) -> dict[str, Any]:
    vault = connection.execute(
        "SELECT id FROM vaults WHERE id=%s", (vault_id,)
    ).fetchone()
    if not vault:
        raise HTTPException(404, "Vault not found")
    evaluation = evaluate_current_quota(connection, vault_id).as_dict()
    return {
        "vault_id": vault_id,
        "limits": get_limits(connection, vault_id).as_dict(),
        "usage": usage_snapshot(connection, vault_id),
        "evaluation": {"state": "evaluated", **evaluation},
    }


@app.get("/api/admin/vaults/{vault_id}/quotas", response_model=response_model("VaultQuotasResponse"))
def admin_vault_quotas(
    vault_id: int, _: dict[str, Any] = Depends(admin_user)
):
    with db() as connection:
        return _quota_payload(connection, vault_id)


@app.put("/api/admin/vaults/{vault_id}/quotas", response_model=response_model("VaultQuotasResponse"))
def update_admin_vault_quotas(
    vault_id: int,
    action: VaultQuotaUpdate,
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    limits = QuotaLimits(
        **action.model_dump(exclude={"reason"})
    )
    with db() as connection:
        try:
            set_limits(connection, vault_id, limits)
        except LookupError as exc:
            raise HTTPException(404, "Vault not found") from exc
        owner = primary_owner(connection, vault_id)
        payload = _quota_payload(connection, vault_id)
    if owner:
        notify_owner_of_admin_action(
            "vault_quotas_changed",
            vault_id=vault_id,
            owner_user_id=owner["user_id"],
            actor_id=admin["id"],
            reason=action.reason,
            limits=limits.as_dict(),
        )
    return payload


@app.get("/api/vault/quotas", response_model=response_model("VaultQuotasResponse"))
def own_vault_quotas(vault: dict[str, Any] = Depends(owner_vault)):
    """Primary-owner read seam; ordinary operators and viewers are denied."""
    with db() as connection:
        return _quota_payload(connection, vault["id"])


@app.get("/api/vault/operation-policy", response_model=response_model("OperationPolicy"))
def own_vault_operation_policy(vault: dict[str, Any] = Depends(owner_vault)):
    with db() as connection:
        return get_policy(connection, vault["id"]).as_dict()


@app.put("/api/vault/operation-policy", response_model=response_model("OperationPolicy"))
def update_vault_operation_policy(
    action: OperationPolicyUpdate,
    vault: dict[str, Any] = Depends(owner_vault),
    user: dict[str, Any] = Depends(current_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    policy = OperationPolicy(
        auto_upload=action.auto_upload,
        auto_local_cleanup=action.auto_local_cleanup,
        local_retention_days=action.local_retention_days,
        stability_seconds=action.stability_seconds,
        include_globs=tuple(action.include_globs),
        exclude_globs=tuple(action.exclude_globs),
        bandwidth_limit_kibps=action.bandwidth_limit_kibps,
        operating_windows=tuple(item.model_dump() for item in action.operating_windows),
    )
    with db() as connection:
        try:
            stored = set_policy(connection, vault["id"], policy)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except LookupError as exc:
            reason = str(exc)
            if reason == "vault_quiesced":
                raise HTTPException(409, "Vault is quiesced for decommission") from exc
            raise HTTPException(404, "Vault not found") from exc
        audit_log(
            "vault_operation_policy_updated",
            connection=connection,
            vault_id=vault["id"],
            actor_user_id=user["id"],
            outcome="updated",
            visibility="vault",
            policy=stored.as_dict(),
        )
    return stored.as_dict()


@app.post("/api/vault/operation-policy/preview-globs", response_model=response_model("GlobPreviewResponse"))
def preview_vault_operation_globs(
    action: GlobPreviewRequest,
    vault: dict[str, Any] = Depends(owner_vault),
):
    try:
        return preview_glob_rules(
            paths=action.paths,
            include_globs=action.include_globs,
            exclude_globs=action.exclude_globs,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/admin/cost-price-books/active", response_model=JsonObjectResponse)
def admin_active_cost_price_book(_: dict[str, Any] = Depends(admin_user)):
    with db() as connection:
        return get_active_price_book(connection).as_dict()


@app.get("/api/admin/cost-price-books", response_model=JsonObjectResponse)
def admin_list_cost_price_books(_: dict[str, Any] = Depends(admin_user)):
    with db() as connection:
        return {"items": [book.as_dict() for book in list_price_books(connection)]}


@app.post("/api/admin/cost-price-books", status_code=201, response_model=JsonObjectResponse)
def admin_create_cost_price_book(
    action: CostPriceBookCreate,
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    with db() as connection:
        created = upsert_price_book(
            connection,
            PriceBook(
                name=action.name,
                currency=action.currency,
                effective_at=action.effective_at,
                assumptions=action.assumptions,
                storage_rates=action.storage_rates,
                restore_rates=action.restore_rates,
            ),
        )
        audit_log(
            "cost_price_book_created",
            connection=connection,
            actor_user_id=admin["id"],
            outcome="created",
            visibility="admin",
            reason=action.reason,
            price_book_id=created.id,
            effective_at=created.effective_at,
        )
    return created.as_dict()


@app.post("/api/admin/cost-price-books/{price_book_id}/activate", response_model=JsonObjectResponse)
def admin_activate_cost_price_book(
    price_book_id: int,
    action: CostPriceBookActivate,
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    with db() as connection:
        try:
            active = activate_price_book(connection, price_book_id)
        except LookupError as exc:
            raise HTTPException(404, "Price book not found") from exc
        audit_log(
            "cost_price_book_activated",
            connection=connection,
            actor_user_id=admin["id"],
            outcome="activated",
            visibility="admin",
            reason=action.reason,
            price_book_id=active.id,
            effective_at=active.effective_at,
        )
    return active.as_dict()


@app.post(
    "/api/admin/cost-estimates/storage",
    response_model=response_model("StorageEstimateResponse"),
)
def admin_storage_cost_estimate(
    action: StorageEstimateRequest,
    _: dict[str, Any] = Depends(admin_user),
):
    with db() as connection:
        book = get_active_price_book(connection)
    return estimate_storage_month(
        book,
        size_bytes=action.size_bytes,
        storage_class=action.storage_class,
    ).as_dict()


def _selected_lifecycle_profile(
    action: LifecycleProfileSelection,
) -> tuple[LifecycleProfile, str | None]:
    if action.guided_profile is not None:
        try:
            return guided_profile(action.guided_profile), action.guided_profile
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if action.profile is None:  # Defensive; Pydantic enforces exactly one selection.
        raise HTTPException(status_code=400, detail="Lifecycle profile is required")
    profile = action.profile.to_profile()
    validation = validate_lifecycle_profile(profile)
    if not validation.ok:
        raise HTTPException(status_code=400, detail="; ".join(validation.errors))
    return profile, None


def _lifecycle_payload(connection: Any, vault: dict[str, Any]) -> dict[str, Any]:
    assignments = load_policy_assignments(connection, vault["id"])
    return {
        "default_policy_id": assignments.default_policy_id,
        "folder_overrides": [
            {"folder_path": folder, "policy_id": policy_id}
            for folder, policy_id in assignments.folder_overrides
        ],
        "policies": list_vault_policies(connection, vault["id"]),
        "guided_profiles": {
            name: {
                "transitions": [
                    {
                        "days": transition.days,
                        "storage_class": transition.storage_class,
                    }
                    for transition in profile.transitions
                ],
                "expiration_days": profile.expiration_days,
                "noncurrent_expiration_days": profile.noncurrent_expiration_days,
                "noncurrent_transitions": [
                    {
                        "days": transition.days,
                        "storage_class": transition.storage_class,
                    }
                    for transition in profile.noncurrent_transitions
                ],
            }
            for name, profile in GUIDED_PROFILES.items()
        },
    }


@app.get("/api/vault/lifecycle", response_model=response_model("LifecycleResponse"))
def own_vault_lifecycle(vault: dict[str, Any] = Depends(owner_vault)):
    with db() as connection:
        return _lifecycle_payload(connection, vault)


@app.put("/api/vault/lifecycle/default", response_model=response_model("LifecycleResponse"))
def update_vault_lifecycle_default(
    action: LifecycleDefaultUpdate,
    vault: dict[str, Any] = Depends(owner_vault),
    user: dict[str, Any] = Depends(current_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    profile, guided_name = _selected_lifecycle_profile(action)
    validation = validate_lifecycle_profile(profile)
    with db() as connection:
        assignments = load_policy_assignments(connection, vault["id"])
        policy_id = assignments.default_policy_id
        if not policy_id:
            policy_id = create_policy(connection, vault_id=vault["id"], name=action.name)
        validation = set_policy_profile(connection, policy_id, profile)
        if not validation.ok:  # Defensive against future validation changes.
            raise HTTPException(status_code=400, detail="; ".join(validation.errors))
        if assignments.default_policy_id is None:
            set_vault_default_policy(connection, vault["id"], policy_id)
        try:
            sync_lifecycle_rules_for_bucket(
                connection,
                s3_client(),
                bucket=vault["s3_bucket"],
            )
        except Exception:
            # Durable policy/tag intent is retried by established reconciliation.
            pass
        payload = _lifecycle_payload(connection, vault)
        audit_log(
            "vault_lifecycle_default_updated",
            connection=connection,
            vault_id=vault["id"],
            actor_user_id=user["id"],
            policy_id=policy_id,
            guided_profile=guided_name,
            custom_profile=guided_name is None,
        )
    return {
        "message": "Vault default lifecycle profile updated",
        "warnings": list(validation.warnings),
        **payload,
    }


@app.put("/api/vault/lifecycle/folder-overrides", response_model=response_model("LifecycleResponse"))
def upsert_vault_lifecycle_folder_override(
    action: LifecycleFolderOverrideUpdate,
    vault: dict[str, Any] = Depends(owner_vault),
    user: dict[str, Any] = Depends(current_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    profile, guided_name = _selected_lifecycle_profile(action)
    validation = validate_lifecycle_profile(profile)
    with db() as connection:
        assignments = load_policy_assignments(connection, vault["id"])
        existing = dict(assignments.folder_overrides).get(
            action.folder_path.replace("\\", "/").strip("/")
        )
        if existing:
            policy_id = existing
        else:
            policy_id = create_policy(
                connection,
                vault_id=vault["id"],
                name=action.name or f"Folder {action.folder_path}",
            )
        validation = set_policy_profile(connection, policy_id, profile)
        if not validation.ok:
            raise HTTPException(status_code=400, detail="; ".join(validation.errors))
        set_folder_override(
            connection,
            vault_id=vault["id"],
            folder_path=action.folder_path,
            policy_id=policy_id,
        )
        try:
            sync_lifecycle_rules_for_bucket(
                connection,
                s3_client(),
                bucket=vault["s3_bucket"],
            )
        except Exception:
            # Durable policy/tag intent is retried by established reconciliation.
            pass
        payload = _lifecycle_payload(connection, vault)
        audit_log(
            "vault_lifecycle_folder_override_updated",
            connection=connection,
            vault_id=vault["id"],
            actor_user_id=user["id"],
            folder_path=action.folder_path,
            policy_id=policy_id,
            guided_profile=guided_name,
            custom_profile=guided_name is None,
        )
    return {
        "message": "Folder lifecycle override updated",
        "warnings": list(validation.warnings),
        **payload,
    }


@app.delete("/api/vault/lifecycle/folder-overrides", response_model=response_model("LifecycleResponse"))
def delete_vault_lifecycle_folder_override(
    action: LifecycleFolderOverrideDelete,
    vault: dict[str, Any] = Depends(owner_vault),
    user: dict[str, Any] = Depends(current_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    with db() as connection:
        clear_folder_override(
            connection,
            vault_id=vault["id"],
            folder_path=action.folder_path,
        )
        payload = _lifecycle_payload(connection, vault)
        audit_log(
            "vault_lifecycle_folder_override_removed",
            connection=connection,
            vault_id=vault["id"],
            actor_user_id=user["id"],
            folder_path=action.folder_path,
        )
    return {"message": "Folder lifecycle override removed", **payload}


@app.get("/api/admin/vaults/{vault_id}/members", response_model=response_model("AdminVaultMembersResponse"))
def vault_members(vault_id: int, _: dict[str, Any] = Depends(admin_user)):
    with db() as connection:
        rows = connection.execute(
            """
            SELECT u.id, u.username, u.display_name, u.active, vm.role
            FROM vault_members vm JOIN users u ON u.id=vm.user_id
            WHERE vm.vault_id=%s ORDER BY lower(u.username)
            """,
            (vault_id,),
        ).fetchall()
    return {"items": rows}


@app.post("/api/admin/vaults/{vault_id}/members", status_code=201, response_model=JsonObjectResponse)
def add_vault_member(
    vault_id: int,
    action: AdminMembershipCreate,
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    """Global-admin override of a vault's sharing.

    The primary owner role can never be handed out here -- only
    :func:`transfer_vault_owner` can change it, preserving the one-owner
    invariant -- so this only ever assigns ``operator``/``viewer``. Per
    ADR-0005's reauth-then-audit precedent, this sensitive override
    requires a ``reason``, is audited, and notifies the vault's owner.
    """
    with db() as connection:
        try:
            assign_member_role(
                connection,
                vault_id=vault_id,
                user_id=action.user_id,
                role=action.role,
                expected_owner_user_id=None,
            )
        except GovernanceError as exc:
            raise _governance_http_error(exc)
        owner = primary_owner(connection, vault_id)
    if owner:
        notify_owner_of_admin_action(
            "vault_membership_changed",
            vault_id=vault_id,
            owner_user_id=owner["user_id"],
            actor_id=admin["id"],
            reason=action.reason,
            member_user_id=action.user_id,
            role=action.role,
        )
    return {"message": "Assignment updated"}


@app.delete("/api/admin/vaults/{vault_id}/members/{user_id}", response_model=JsonObjectResponse)
def remove_vault_member(
    vault_id: int,
    user_id: int,
    reason: str = Query(min_length=3, max_length=500),
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    """Global-admin removal of a vault member (never the primary owner --
    transfer ownership first). Requires ``reason``, is audited, and
    notifies the vault's owner, per ADR-0005's precedent for sensitive
    admin overrides."""
    with db() as connection:
        try:
            remove_member(
                connection,
                vault_id=vault_id,
                user_id=user_id,
                expected_owner_user_id=None,
            )
        except GovernanceError as exc:
            raise _governance_http_error(exc)
        owner = primary_owner(connection, vault_id)
    if owner:
        notify_owner_of_admin_action(
            "vault_membership_changed",
            vault_id=vault_id,
            owner_user_id=owner["user_id"],
            actor_id=admin["id"],
            reason=reason,
            member_user_id=user_id,
            role=None,
        )
    return {"message": "Access removed"}


@app.post("/api/admin/vaults/{vault_id}/transfer-owner", response_model=JsonObjectResponse)
def transfer_vault_owner(
    vault_id: int,
    action: AdminOwnerTransfer,
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    """Global-admin override to transfer primary ownership.

    Atomically demotes the current owner to operator and promotes the
    target (never moving the vault's namespace, local directory, or S3
    objects). Requires ``reason``, is audited, and notifies both the
    outgoing and incoming owner, per ADR-0005's precedent for sensitive
    admin overrides.
    """
    with db() as connection:
        try:
            result = transfer_primary_ownership(
                connection,
                vault_id=vault_id,
                new_owner_user_id=action.new_owner_user_id,
                expected_current_owner_user_id=None,
            )
        except GovernanceError as exc:
            raise _governance_http_error(exc)
    for owner_user_id in {result["previous_owner_id"], result["new_owner_id"]}:
        notify_owner_of_admin_action(
            "vault_ownership_transferred",
            vault_id=vault_id,
            owner_user_id=owner_user_id,
            actor_id=admin["id"],
            reason=action.reason,
            previous_owner_id=result["previous_owner_id"],
            new_owner_id=result["new_owner_id"],
        )
    return {"message": "Ownership transferred", **result}


@app.get("/api/vault/members", response_model=response_model("VaultMembersResponse"))
def list_own_vault_members(vault: dict[str, Any] = Depends(owner_vault)):
    """Owner self-service: view the current vault's sharing."""
    with db() as connection:
        rows = connection.execute(
            """
            SELECT u.id, u.username, u.display_name, vm.role
            FROM vault_members vm JOIN users u ON u.id=vm.user_id
            WHERE vm.vault_id=%s ORDER BY lower(u.username)
            """,
            (vault["id"],),
        ).fetchall()
    return {"items": rows}


@app.post("/api/vault/user-lookup", response_model=response_model("UserLookupResult"))
def lookup_vault_user(
    action: UserLookup,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(owner_vault),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    """Look up one active user without exposing directory information.

    A lookup is deliberately scoped to the selected vault and returns only the
    fields needed to confirm a sharing change. It never returns identities,
    administrator state, activity timestamps, or other memberships.
    """
    username = action.username.strip().lower()
    if not 2 <= len(username) <= 80 or not re.fullmatch(
        r"[A-Za-z0-9._-]+", username
    ):
        raise HTTPException(422, "Enter a valid username")
    with db() as connection:
        retry_after = check_lookup_rate_limit(
            connection,
            backend=settings.db_backend,
            user_id=user["id"],
            client_ip=_client_ip(request) or "unknown",
        )
        if retry_after is not None:
            raise HTTPException(
                429,
                "Too many lookup attempts; try again later",
                headers={"Retry-After": str(retry_after)},
            )
        target = connection.execute(
            """
            SELECT u.id, u.username, u.display_name,
                   vm.role AS current_vault_role
            FROM users u
            LEFT JOIN vault_members vm
              ON vm.vault_id=%s AND vm.user_id=u.id
            WHERE lower(u.username)=lower(%s) AND u.active=TRUE
            """,
            (vault["id"], username),
        ).fetchone()
    if not target:
        # Unknown and inactive users intentionally share the same response.
        raise HTTPException(404, "User not found")
    return {
        "id": target["id"],
        "username": target["username"],
        "display_name": target["display_name"],
        "current_vault_role": target["current_vault_role"],
    }


@app.post("/api/vault/members", status_code=201, response_model=JsonObjectResponse)
def add_own_vault_member(
    action: MembershipCreate,
    vault: dict[str, Any] = Depends(owner_vault),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    """Owner self-service: share the current vault as operator/viewer."""
    with db() as connection:
        try:
            assign_member_role(
                connection,
                vault_id=vault["id"],
                user_id=action.user_id,
                role=action.role,
                expected_owner_user_id=vault["member_user_id"],
            )
        except GovernanceError as exc:
            raise _governance_http_error(exc)
        audit_log(
            "vault_membership_changed",
            connection=connection,
            vault_id=vault["id"],
            member_user_id=action.user_id,
            role=action.role,
            admin_override=False,
        )
    return {"message": "Assignment updated"}


@app.delete("/api/vault/members/{user_id}", response_model=JsonObjectResponse)
def remove_own_vault_member(
    user_id: int,
    vault: dict[str, Any] = Depends(owner_vault),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    """Owner self-service: revoke a member's access to the current vault."""
    with db() as connection:
        try:
            remove_member(
                connection,
                vault_id=vault["id"],
                user_id=user_id,
                expected_owner_user_id=vault["member_user_id"],
            )
        except GovernanceError as exc:
            raise _governance_http_error(exc)
        audit_log(
            "vault_membership_changed",
            connection=connection,
            vault_id=vault["id"],
            member_user_id=user_id,
            role=None,
            admin_override=False,
        )
    return {"message": "Access removed"}


@app.post("/api/vault/transfer-owner", response_model=JsonObjectResponse)
def transfer_own_vault_owner(
    action: OwnerTransfer,
    vault: dict[str, Any] = Depends(owner_vault),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    """Owner self-service: hand primary ownership to another member.

    Atomic role swap on ``vault_members`` alone -- the vault's generated
    namespace, local directory, and S3 objects never move.
    """
    with db() as connection:
        try:
            result = transfer_primary_ownership(
                connection,
                vault_id=vault["id"],
                new_owner_user_id=action.new_owner_user_id,
                expected_current_owner_user_id=vault["member_user_id"],
            )
        except GovernanceError as exc:
            raise _governance_http_error(exc)
        audit_log(
            "vault_ownership_transferred",
            connection=connection,
            vault_id=vault["id"],
            admin_override=False,
            **result,
        )
    return {"message": "Ownership transferred", **result}


@app.get("/health", response_model=JsonObjectResponse)
def health():
    """Process liveness probe — does not inspect dependencies."""
    return {"status": "ok"}


@app.get("/ready", response_model=JsonObjectResponse)
def ready():
    """Dependency-aware readiness: database, worker heartbeat, and config."""
    report = health_service.readiness_report()
    status_code = 200 if report["status"] == "ready" else 503
    return JSONResponse(report, status_code=status_code)


@app.get(
    "/metrics",
    response_class=Response,
    responses={
        200: {
            "description": "Prometheus text exposition",
            "content": {"text/plain": {"schema": {"type": "string"}}},
        }
    },
)
def metrics():
    """Prometheus text exposition (low-cardinality labels only)."""
    return Response(
        content=metrics_service.render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


_SPA_FALLBACK_EXCLUDED_PREFIXES = frozenset({"api", "auth", "static", "assets"})


@app.get("/assets/{asset_path:path}")
def spa_asset(asset_path: str):
    """Serve hashed Vite build assets with long-lived immutable caching."""
    assets_root = (_spa_dist_dir() / "assets").resolve()
    if not assets_root.is_dir():
        raise HTTPException(status_code=404, detail="Not Found")
    # Sanitize before joining so user-controlled path segments never reach the
    # filesystem APIs unchecked (CodeQL path-injection / directory traversal).
    try:
        target = safe_local_path(str(assets_root), asset_path)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not Found") from None
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(
        target,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


def _spa_dist_file_response(full_path: str) -> FileResponse | None:
    """Serve a built file from frontend/dist when it exists (manifest, SW, icons)."""
    if not full_path or full_path.endswith("/"):
        return None
    dist_root = _spa_dist_dir().resolve()
    try:
        target = safe_local_path(str(dist_root), full_path)
    except ValueError:
        return None
    if not target.is_file() or target.name == "index.html":
        return None
    media_type, _ = mimetypes.guess_type(target.name)
    if target.suffix == ".webmanifest":
        media_type = "application/manifest+json"
    elif target.suffix == ".js":
        media_type = "application/javascript; charset=utf-8"
    return FileResponse(
        target,
        media_type=media_type or "application/octet-stream",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    """SPA client-route fallback; never intercepts API, auth, or static assets."""
    head = full_path.split("/", 1)[0]
    if head in _SPA_FALLBACK_EXCLUDED_PREFIXES:
        raise HTTPException(status_code=404, detail="Not Found")
    built = _spa_dist_file_response(full_path)
    if built is not None:
        return built
    return _spa_index_response()
