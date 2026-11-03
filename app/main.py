from __future__ import annotations

import asyncio
import mimetypes
import secrets
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
from .config import settings, settings_snapshot, validate_settings
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
from .catalog import ArchiveCatalog
from .database import db, initialize_database
from .migrate_on_start import ensure_schema_current
from .services.source_layout import (
    get_sources_root,
    prepare_sources_layout,
    reconcile_source_volume_identities,
    source_volume_inventory,
    validate_nested_mounts_after_identity,
    vault_local_access,
)
from .services import source_directories as source_directories_service
from .services import vault_relocation as vault_relocation_service
from .services import vault_decommission as vault_decommission_service
from .oidc import (
    IdentityRejected,
    OidcError,
    begin_login,
    bind_owner_identity,
    complete_login,
)
from .oidc_network import oidc_host_addresses
from .proxy import parse_networks, resolve_client_ip
from .security import DUMMY_PASSWORD_HASH, verify_password
from .services import audit_events as audit_event_store
from .services import catalog_incidents as catalog_incident_service
from .services import health as health_service
from .services import metadata_backups as metadata_backup_service
from .services import metrics as metrics_service
from .services import notifications as notification_service
from .services.catalog_event_stream import (
    coalesced_catchup_signal,
    iter_catalog_event_sse,
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
    estimate_restore_cost,
    estimate_storage_month,
    list_prices,
)
from .services.lifecycle_policies import (
    load_vault_plan,
    set_vault_plan,
    sync_lifecycle_rules_for_bucket,
)
from .services.lifecycle_profiles import (
    GUIDED_PROFILES,
    LifecycleProfile,
    LifecycleTransition,
    guided_profile,
    profile_to_dict,
    validate_lifecycle_profile,
)
from .services import cloud_deletion as cloud_deletion_service
from .services.vaults import (
    InvalidVaultName,
    VaultAdoptionError,
    VaultCreationError,
    VaultProvisioningUnavailable,
    VaultSlugTaken,
    create_vault,
    list_vault_inventory,
)
from .sessions import (
    SessionTransitionError,
    create_session,
    csrf_token_for,
    is_reauth_recent,
    mark_reauthenticated,
    resolve_session,
    revoke_session,
    rotate_session,
    set_session_vault,
)
from .services.storage_classes import (
    cold_class_warning,
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
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        # Authenticated state and catalog listings are network-only, including
        # errors: neither the browser nor an intermediary may retain them.
        response.headers["Cache-Control"] = "no-store"
    return response


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


def _runtime_settings():
    return settings_snapshot(settings)


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


def require_recent_reauth(
    request: Request, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    session = request.state.session
    reauth_window_seconds = settings.reauth_window_seconds
    if not is_reauth_recent(
        session.get("reauth_at"),
        now=datetime.now(timezone.utc),
        window_seconds=reauth_window_seconds,
    ):
        raise ReauthRequired()
    return user


_OPERATIONAL_VAULT = "v.enabled=TRUE AND v.decommission_state='active'"


def current_vault(
    request: Request, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    """Resolve the Session's selected operational Vault.

    Every Vault belongs to the single owner (#412); the selection only picks
    which one the UI is looking at.
    """
    del user  # authentication gate only
    session = request.state.session
    requested_id = session.get("vault_id")
    with db() as connection:
        vault = None
        if requested_id:
            vault = connection.execute(
                f"SELECT v.* FROM vaults v WHERE v.id=%s AND {_OPERATIONAL_VAULT}",
                (requested_id,),
            ).fetchone()
        if not vault:
            vault = connection.execute(
                f"""
                SELECT v.* FROM vaults v WHERE {_OPERATIONAL_VAULT}
                ORDER BY v.name
                LIMIT 1
                """
            ).fetchone()
        if not vault:
            raise HTTPException(403, "No vault exists yet")
        if session.get("vault_id") != vault["id"]:
            updated = set_session_vault(
                connection,
                session["id"],
                vault["id"],
                expected_version=session["transition_version"],
            )
            if not updated:
                # A concurrent logout, OIDC rotation, expiry, or Vault choice
                # won after current_user resolved this request. Do not let this
                # older process overwrite the newer persisted transition.
                raise HTTPException(409, "Session changed; retry the request")
            session.update(updated)
    return vault


def _vault_for_decommission(vault_id: int) -> dict[str, Any]:
    with db() as connection:
        vault = connection.execute(
            "SELECT * FROM vaults WHERE id=%s", (vault_id,)
        ).fetchone()
    if vault is None:
        raise HTTPException(404, "Vault not found")
    return vault


def decommission_vault(
    request: Request, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    """Resolve the selected Vault even after it is quiesced or disabled.

    Normal ``current_vault`` deliberately hides non-operational Vaults.  This
    narrow seam keeps decommission progress visible while history remains
    preserved in the tombstone.
    """
    del user  # authentication gate only
    requested_id = request.state.session.get("vault_id")
    with db() as connection:
        vault = None
        if requested_id:
            vault = connection.execute(
                "SELECT * FROM vaults WHERE id=%s", (requested_id,)
            ).fetchone()
        if vault is None:
            vault = connection.execute(
                """
                SELECT * FROM vaults
                ORDER BY CASE WHEN decommission_state='decommissioning' THEN 0 ELSE 1 END,
                         lower(name)
                LIMIT 1
                """
            ).fetchone()
    if vault is None:
        raise HTTPException(404, "Vault not found")
    return vault


class LoginRequest(BaseModel):
    username: str
    password: str


class LocaleUpdate(BaseModel):
    locale: str


class ReauthRequest(BaseModel):
    password: str


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
    slug: str | None = Field(default=None, min_length=2, max_length=60)
    creation_mode: str = Field(default="empty", pattern="^(empty|adopt)$")
    volume_alias: str | None = Field(default=None, min_length=1, max_length=120)
    relative_path: str | None = Field(default=None, max_length=1024)


class FileAction(BaseModel):
    path: str
    is_directory: bool = False
    archive_version_id: str | None = None
    restore_tier: str | None = None
    restore_days: int | None = Field(default=None, ge=1, le=30)


class CloudOnlyReplacementAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str


class RecoverEstimateRequest(BaseModel):
    path: str
    archive_version_id: str | None = None
    restore_tier: str | None = None
    restore_days: int | None = Field(default=None, ge=1, le=30)


class RecoverApproveAction(BaseModel):
    group_id: str = Field(min_length=1, max_length=64)


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


class CloudPurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    is_directory: bool = False
    paths: list[str] | None = None
    confirmation: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)
    generated_phrase: str = Field(min_length=1, max_length=200)


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


class OperationPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_upload: bool = False
    auto_local_cleanup: bool = False
    local_retention_days: int | None = Field(default=None, ge=1)
    stability_seconds: int = Field(default=300, ge=0)
    include_globs: list[str] = Field(default_factory=list)
    exclude_globs: list[str] = Field(default_factory=list)


class GlobPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[str] = Field(default_factory=list)
    include_globs: list[str] = Field(default_factory=list)
    exclude_globs: list[str] = Field(default_factory=list)


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
                        "cloud-purge": 0,
                    },
                    "storage_classes": set(),
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
                "cloud-purge": has_cloud,
            }
            for action, eligible in action_flags.items():
                if eligible:
                    folder["action_counts"][action] += 1
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
        raise HTTPException(403, "The emergency password is not accepted from this network")
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
            eligible = bool(user and user["password_hash"])
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
    """Emergency Password Reauthentication with an isolated durable backoff.

    Accepted only from loopback and the LAN CIDRs; elsewhere the Owner steps
    up through the provider (see ``/auth/oidc/reauth``). Its account/IP
    counters deliberately use namespaced keys so a Reauthentication success
    cannot reset sign-in or OIDC-related throttling state.
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
):
    redirect_uri = str(request.url_for("oidc_callback"))
    with db() as connection:
        try:
            authorization_url = begin_login(
                connection,
                redirect_uri=redirect_uri,
                return_to=_safe_return_to(return_to),
                http_client=_oidc_client(),
                host_addresses=_oidc_host_addresses,
            )
        except OidcError as error:
            _raise_oidc_start_error(error)
    return RedirectResponse(authorization_url, status_code=303)


@app.get("/auth/oidc/callback")
def oidc_callback(
    request: Request,
    response: Response,
    state: str = Query(...),
    code: str = Query(...),
):
    redirect_uri = str(request.url_for("oidc_callback"))
    rejected = False
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
        try:
            user_id = bind_owner_identity(connection, claims)
        except IdentityRejected:
            rejected = True
        if not rejected:
            existing = resolve_session(connection, _read_session_cookie(request))
            if existing and existing["user"]["id"] == user_id:
                # Step-up reauthentication: the owner proved their identity
                # again, so refresh the reauth window and rotate the token
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
            else:
                raw_token = create_session(
                    connection,
                    user_id=user_id,
                    auth_method="oidc",
                    ip=_client_ip(request),
                    user_agent=request.headers.get("user-agent"),
                )
                csrf_token = csrf_token_for(connection, raw_token)
        else:
            audit_log(
                "oidc_identity_rejected",
                connection=connection,
                ip=_client_ip(request),
                issuer=claims.issuer,
            )
    if rejected:
        raise HTTPException(403, "This identity is not the owner's identity")
    redirect = RedirectResponse(
        _safe_return_to(claims.return_to), status_code=303
    )
    _set_session_cookie(redirect, raw_token)
    if csrf_token:
        _set_csrf_cookie(redirect, csrf_token)
    return redirect


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
        vault_block = {
            "id": vault["id"],
            "slug": vault["slug"],
            "name": vault["name"],
            "delete_enabled": _runtime_settings().allow_local_delete,
            "cloud_deletion_enabled": bool(vault.get("cloud_deletion_enabled")),
        }
    else:
        selected_id = request.state.session.get("vault_id")
        with db() as connection:
            decommission_vault = None
            if selected_id:
                decommission_vault = connection.execute(
                    """
                    SELECT id, slug, name, decommission_state, root_released_at
                    FROM vaults
                    WHERE id=%s AND decommission_state<>'active'
                    """,
                    (selected_id,),
                ).fetchone()
            if decommission_vault is None:
                decommission_vault = connection.execute(
                    """
                    SELECT id, slug, name, decommission_state, root_released_at
                    FROM vaults
                    WHERE decommission_state<>'active'
                    ORDER BY CASE WHEN decommission_state='decommissioning' THEN 0 ELSE 1 END,
                             decommissioned_at DESC, lower(name)
                    LIMIT 1
                    """
                ).fetchone()
        if decommission_vault:
            decommission_vault_block = {
                **decommission_vault,
                "root_released": bool(decommission_vault["root_released_at"]),
            }
    return {
        **user,
        "csrf_token": csrf_token,
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
def list_vaults(_: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        rows = connection.execute(
            f"""
            SELECT v.id, v.slug, v.name
            FROM vaults v
            WHERE {_OPERATIONAL_VAULT}
            ORDER BY v.name
            """
        ).fetchall()
    return {"items": rows}


@app.post("/api/vaults", status_code=201, response_model=response_model("VaultCreateResponse"))
def create_owner_vault(
    action: VaultSelfServiceCreate,
    background_tasks: BackgroundTasks,
    _: dict[str, Any] = Depends(current_user),
):
    """Create a Vault from the UI, the only way Vaults are created (#412).

    The server generates the plaintext storage identity.
    Adoption binds any free directory of a custom Source Volume
    in place and starts an asynchronous local scan.
    """
    try:
        vault = create_vault(
            action.name,
            action.slug,
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
        # scan_vault reloads the authoritative row.
        background_tasks.add_task(scan_vault, {"id": vault["id"]})
    payload: dict[str, Any] = {
        "id": vault["id"],
        "uuid": vault["uuid"],
        "slug": vault["slug"],
        "name": vault["name"],
        "creation_mode": action.creation_mode,
    }
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
    vault: dict[str, Any] = Depends(decommission_vault),
):
    return _preview_vault_decommission(int(vault["id"]), action)


@app.get("/api/vault/decommission/status", response_model=response_model("VaultDecommissionStatus"))
def own_vault_decommission_status(
    vault: dict[str, Any] = Depends(decommission_vault),
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
    vault: dict[str, Any] = Depends(decommission_vault),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    return _start_vault_decommission(
        vault_id=int(vault["id"]),
        action=action,
        actor=user,
    )


@app.post("/api/vault/decommission/cloud-purge/cancel", response_model=JsonObjectResponse)
def cancel_own_vault_decommission_cloud_purge(
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(decommission_vault),
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
    _vault_for_decommission(vault_id)
    return _preview_vault_decommission(vault_id, action)


@app.get("/api/vaults/{vault_id}/decommission/status", response_model=response_model("VaultDecommissionStatus"))
def owned_vault_decommission_status_by_id(
    vault_id: int,
    user: dict[str, Any] = Depends(current_user),
):
    _vault_for_decommission(vault_id)
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
    _vault_for_decommission(vault_id)
    return _start_vault_decommission(
        vault_id=vault_id,
        action=action,
        actor=user,
    )


@app.post("/api/vaults/{vault_id}/decommission/cloud-purge/cancel", response_model=JsonObjectResponse)
def cancel_owned_vault_decommission_cloud_purge_by_id(
    vault_id: int,
    user: dict[str, Any] = Depends(current_user),
):
    _vault_for_decommission(vault_id)
    try:
        with db() as connection:
            return vault_decommission_service.cancel_pending_cloud_purge(
                connection,
                vault_id=vault_id,
                actor_user_id=int(user["id"]),
            )
    except vault_decommission_service.VaultDecommissionError as exc:
        raise _vault_decommission_http_error(exc) from exc


@app.post("/api/vaults/select", response_model=response_model("VaultSelectResponse"))
def select_vault(
    action: VaultSelection,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
):
    with db() as connection:
        allowed = connection.execute(
            """
            SELECT 1 FROM vaults v
            WHERE v.id=%s AND v.enabled=TRUE AND v.decommission_state='active'
            """,
            (action.vault_id,),
        ).fetchone()
        if not allowed:
            raise HTTPException(403, "Vault access denied")
        updated = set_session_vault(
            connection,
            request.state.session["id"],
            action.vault_id,
            expected_version=request.state.session["transition_version"],
        )
    if not updated:
        # The row transition is conditional on the Session snapshot observed by
        # current_user, so another process cannot be silently overwritten.
        raise HTTPException(409, "Session changed; retry the request")
    request.state.session.update(updated)
    return {**_api_message(request, "api.vault_selected")}


@app.get(
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

    return {
        "items": items,
        "total": total,
        "page": page,
        "directory": directory,
        "mode": listing["mode"],
        "aggregate_status": listing.get("aggregate_status", "ready"),
    }


@app.get("/api/stats", response_model=response_model("StatsResponse"))
def stats(vault: dict[str, Any] = Depends(current_vault)):
    source_root = vault.get("source_root") or ""
    # Reconcile Source Volume identity before anything can preflight or walk the
    # configured Vault tree. Catalog summaries remain available when local work
    # is suspended, so they are intentionally collected after this gate too.
    access = vault_local_access(source_root)
    with db() as connection:
        # Cheap SQL aggregates only — never materialize every Vault File row.
        summary = ArchiveCatalog(connection).summary(vault["id"])
        from .services.component_health import (
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
    return {
        **summary,
        "runtime": runtime,
        "source_volume": {
            "alias": access.volume_alias,
            "health": access.volume_health,
            "local_operations_allowed": access.local_operations_allowed,
        },
        "delete_enabled": _runtime_settings().allow_local_delete,
    }


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

    Server-authoritative and idempotent; preserves the same Vault and in-app
    visibility checks as single-item mark-read.
    """
    with db() as connection:
        return notification_service.mark_all_notifications_read(
            connection, user_id=user["id"]
        )


class MetadataBackupRunAction(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@app.get("/api/admin/metadata-backups", response_model=JsonObjectResponse)
def admin_list_metadata_backups(_: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        status = metadata_backup_service.backup_status(connection)
        runs = metadata_backup_service.list_backup_artifacts(connection)
    return {"status": status, "runs": runs}


@app.post("/api/admin/metadata-backups/run", response_model=JsonObjectResponse)
def admin_run_metadata_backup(
    action: MetadataBackupRunAction,
    admin: dict[str, Any] = Depends(current_user),
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
            raise HTTPException(500, str(exc)) from exc
        # An explicit export of the metadata database is sensitive. Routine
        # scheduled backups do not create action-log entries.
        audit_event_store.record_audit_event(
            connection,
            event="metadata_backup_created",
            actor_user_id=admin["id"],
            outcome="success",
            reason=action.reason,
            digest_sha256=result["digest_sha256"],
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
    admin: dict[str, Any] = Depends(current_user),
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
                    action == "recover" and (
                        not selectable or selected["id"] != selectable[0]["id"]
                    )
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
                    pricing=list_prices(settings.aws_region).restore_rates,
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
    if not job_ids:
        raise HTTPException(409, "An operation is already running on the selected files")
    result = {
        "group_id": group_id,
        "job_ids": job_ids,
        "item_count": len(job_ids),
        "total_bytes": total_bytes,
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
    # The response is an internal exact-version pin for recovery, not history.
    recoverable = [row for row in versions if row["recoverable"]][:1]
    default_version_id = recoverable[0]["id"] if recoverable else None
    return {
        "path": logical_path,
        "items": recoverable,
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
    selected = next((row for row in versions if row["recoverable"]), None)
    if selected is None:
        raise HTTPException(404, "No recoverable Archive Version found")
    if action.archive_version_id and action.archive_version_id != selected["id"]:
        raise HTTPException(409, "The current snapshot changed; retry recovery")
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
        priced = estimate_restore_cost(
            list_prices(settings.aws_region),
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
                    "Estimate from AWS list prices; not an AWS Billing quote.",
                )
            ),
            "region": priced.region,
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
    queued = queue_jobs(action.path, "upload", vault["id"], user["id"], action.is_directory)
    return {**queued, **_api_message(request, "api.upload_started")}


def confirm_cloud_only_replacement(
    path: str, vault_id: int, user_id: int
) -> dict[str, Any]:
    """Release held-back content and queue the upload that replaces the copy.

    The previous Archive Version was the only cloud copy; Current Snapshot
    deletes it only after the new upload is verified (#411, #363 D7).
    """
    logical_path = safe_relative_path(path).as_posix()
    with db() as connection:
        try:
            catalog_incident_service.confirm_cloud_only_replacement(
                connection,
                vault_id=vault_id,
                path=logical_path,
                actor_user_id=user_id,
            )
        except catalog_incident_service.NoCloudOnlyConflict as exc:
            raise HTTPException(
                409, "No conflict with a cloud-only file at this path"
            ) from exc
    return queue_jobs(logical_path, "upload", vault_id, user_id)


@app.post(
    "/api/files/cloud-only-conflict/replace",
    status_code=202,
    response_model=JsonObjectResponse,
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def replace_cloud_only_copy(
    action: CloudOnlyReplacementAction,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
):
    """Confirm that new content replaces the only cloud copy at its path."""
    queued = confirm_cloud_only_replacement(action.path, vault["id"], user["id"])
    return {**queued, **_api_message(request, "api.upload_started")}


def cancel_job_group(
    group_id: str,
    job_action: str,
    vault: dict[str, Any],
    actor_user_id: int,
):
    message_keys = {
        "upload": "job.upload_stopped",
        "recover": "job.recovery_stopped",
        "free-space": "job.cleanup_stopped",
        "rename": "job.rename_stopped",
        "cloud-purge": "job.cloud_purge_stopped",
        "storage-class": "job.storage_class_stopped",
    }
    if job_action not in message_keys:
        raise HTTPException(422, "Invalid operation")
    message_key = message_keys[job_action]
    message = translate(message_key, locale=DEFAULT_LOCALE)
    with db() as connection:
        if job_action == "cloud-purge":
            try:
                cancelled = cloud_deletion_service.cancel_cloud_deletion(
                    connection,
                    vault_id=vault["id"],
                    group_id=group_id,
                    actor_user_id=actor_user_id,
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
                actor_user_id=actor_user_id,
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
    return cancel_job_group(action.group_id, action.action, vault, user["id"])


@app.post("/api/upload/cancel", response_model=response_model("JobCancelResponse"))
def cancel_upload(
    action: GroupCancelAction,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
):
    """Backward-compatible endpoint for clients using the old upload route."""
    return cancel_job_group(action.group_id, "upload", vault, user["id"])


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
):
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
):
    if not _runtime_settings().allow_local_delete:
        raise HTTPException(403, "Freeing local space is disabled")
    queued = queue_jobs(
        action.path, "free-space", vault["id"], user["id"], action.is_directory
    )
    return {**queued, **_api_message(request, "api.free_space_started")}


def _cloud_deletion_paths(action: CloudDeletionPreviewRequest | CloudPurgeRequest) -> list[str]:
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
):
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
    with db() as connection:
        preview = cloud_deletion_service.preview_selection(
            connection,
            vault_id=vault["id"],
            paths=_cloud_deletion_paths(action),
            is_directory=action.is_directory,
        )
    return {
        **preview.as_dict(),
    }


@app.post("/api/cloud-purge", status_code=202, response_model=JsonObjectResponse)
def cloud_purge(
    action: CloudPurgeRequest,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
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


@app.get("/api/admin/source-volumes", response_model=response_model("SourceVolumeInventoryResponse"))
def admin_source_volumes(user: dict[str, Any] = Depends(current_user)):
    """Operator inventory of discovered Source Volumes (issue #148)."""
    return {"items": source_volume_inventory()}


_SOURCE_DIRECTORY_ERROR_STATUS: dict[str, tuple[int, str]] = {
    "volume_not_found": (404, "Source Volume not found"),
    "volume_unavailable": (409, "Source Volume is not available"),
    "invalid_volume": (422, "Source Volume is not usable for Vault roots"),
    "invalid_path": (422, "Source directory path is invalid"),
    "path_missing": (404, "Source directory does not exist"),
    "occupied": (409, "Occupied Vault roots cannot be browsed"),
}


def _source_directory_http_error(
    exc: source_directories_service.SourceDirectoryError,
) -> HTTPException:
    status_code, message = _SOURCE_DIRECTORY_ERROR_STATUS.get(
        exc.reason, (400, "Request could not be completed")
    )
    return HTTPException(status_code, message)


@app.get("/api/source-volumes/{volume_alias}/browse", response_model=response_model("SourceDirectoryBrowseResponse"))
def browse_source_volume(
    volume_alias: str,
    path: str = Query(default=""),
    _: dict[str, Any] = Depends(current_user),
):
    """Lazy directory browser for choosing a Vault root on a Source Volume."""
    try:
        with db() as connection:
            return source_directories_service.browse_source_directories(
                connection,
                volume_alias=volume_alias,
                relative_path=path,
            )
    except source_directories_service.SourceDirectoryError as exc:
        raise _source_directory_http_error(exc) from exc


@app.get("/api/admin/vaults", response_model=response_model("AdminVaultsResponse"))
def admin_vaults(_: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        return {"items": list_vault_inventory(connection)}


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
    admin: dict[str, Any] = Depends(current_user),
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


@app.get("/api/vault/operation-policy", response_model=response_model("OperationPolicy"))
def own_vault_operation_policy(vault: dict[str, Any] = Depends(current_vault)):
    with db() as connection:
        return get_policy(connection, vault["id"]).as_dict()


@app.put("/api/vault/operation-policy", response_model=response_model("OperationPolicy"))
def update_vault_operation_policy(
    action: OperationPolicyUpdate,
    vault: dict[str, Any] = Depends(current_vault),
    user: dict[str, Any] = Depends(current_user),
):
    policy = OperationPolicy(
        auto_upload=action.auto_upload,
        auto_local_cleanup=action.auto_local_cleanup,
        local_retention_days=action.local_retention_days,
        stability_seconds=action.stability_seconds,
        include_globs=tuple(action.include_globs),
        exclude_globs=tuple(action.exclude_globs),
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
    vault: dict[str, Any] = Depends(current_vault),
):
    try:
        return preview_glob_rules(
            paths=action.paths,
            include_globs=action.include_globs,
            exclude_globs=action.exclude_globs,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post(
    "/api/admin/cost-estimates/storage",
    response_model=response_model("StorageEstimateResponse"),
)
def admin_storage_cost_estimate(
    action: StorageEstimateRequest,
    _: dict[str, Any] = Depends(current_user),
):
    size_bytes = action.size_bytes
    if action.vault_id is not None:
        with db() as connection:
            if not connection.execute(
                "SELECT id FROM vaults WHERE id=%s", (action.vault_id,)
            ).fetchone():
                raise HTTPException(404, "Vault not found")
            usage = connection.execute(
                """
                SELECT COALESCE(SUM(size), 0) AS total,
                       SUM(CASE WHEN size IS NULL THEN 1 ELSE 0 END) AS unknown
                FROM archive_versions
                WHERE vault_id=%s AND availability='available'
                """,
                (action.vault_id,),
            ).fetchone()
        if usage["unknown"]:
            raise HTTPException(409, "The Vault's cloud storage size is unknown")
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
    return {
        "profile": profile_to_dict(load_vault_plan(connection, vault["id"])),
        "guided_profiles": {
            name: profile_to_dict(profile)
            for name, profile in GUIDED_PROFILES.items()
        },
    }


@app.get("/api/vault/lifecycle", response_model=response_model("LifecycleResponse"))
def own_vault_lifecycle(vault: dict[str, Any] = Depends(current_vault)):
    with db() as connection:
        return _lifecycle_payload(connection, vault)


@app.put("/api/vault/lifecycle", response_model=response_model("LifecycleResponse"))
def update_vault_storage_plan(
    action: LifecycleProfileSelection,
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
                s3_client(),
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
            actor_user_id=user["id"],
            guided_profile=guided_name,
            custom_profile=guided_name is None,
        )
    return {
        "message": "Vault storage plan saved",
        "sync_pending": sync_pending,
        "warnings": list(validation.warnings),
        **payload,
    }


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


_SPA_FALLBACK_EXCLUDED_PREFIXES = frozenset({
    "api", "auth", "static", "assets",
    "manifest.webmanifest", "pwa-192.png", "pwa-512.png",
})


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
    """Serve a built public file from frontend/dist when it exists."""
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
    if target.suffix == ".js":
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


@app.api_route(
    "/{full_path:path}",
    methods=["POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
def unknown_route(full_path: str):
    """Unknown or removed routes are Not Found for every method.

    Without this the GET-only SPA fallback answers 405 to a mutation on a
    removed endpoint, which hides that the endpoint no longer exists.
    """
    del full_path
    raise HTTPException(status_code=404, detail="Not Found")
