from __future__ import annotations

import asyncio
import mimetypes
import secrets
import uuid
from collections.abc import Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal
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
from .diagnostics import require_metrics_token
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
from .database import close_pools, db, initialize_database
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
from .services import protection_stop as protection_stop_service
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
from .services import recovery_kit as recovery_kit_service
from .services import metrics as metrics_service
from .services import notifications as notification_service
from .services.directory_aggregates import request_vault_rebuild
from .services.shutdown import shutdown_state
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
    local_release_is_confirmed,
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
from .services import vault_moves
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
    auto_upload_loop,
    cloud_preflight_loop,
    upload_remediation_loop,
    bulk_upload_admission_loop,
    drain_background_executors,
    stop_background_executors,
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
    shutdown_state.reset()
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
    protection_stop_service.reconcile_all()
    tasks = [asyncio.create_task(background_loop()), asyncio.create_task(auto_upload_loop()),
             asyncio.create_task(cloud_preflight_loop()), asyncio.create_task(upload_remediation_loop()),
             asyncio.create_task(bulk_upload_admission_loop())]
    if settings.filesystem_watch_enabled:
        tasks.append(asyncio.create_task(filesystem_watch_loop()))
    shutdown_state.register_producers(tasks)
    shutdown_state.register_stop_callback(stop_background_executors)
    try:
        yield
    finally:
        # Also works for non-uvicorn ASGI/test lifespans. In production the
        # server has already signalled SSE and stopped these producers BEFORE
        # waiting for HTTP connections, rather than only reaching this point.
        shutdown_state.request()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not await drain_background_executors():
            raise RuntimeError("Background workers exceeded the shutdown drain budget")
        # Workers are drained: close the idle pooled connections (#535).
        close_pools()


app = FastAPI(
    title=PRODUCT_NAME,
    lifespan=lifespan,
    docs_url="/docs" if settings.api_docs_enabled else None,
    redoc_url="/redoc" if settings.api_docs_enabled else None,
    openapi_url="/openapi.json" if settings.api_docs_enabled else None,
)


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
    request: Request, user: dict[str, Any] = Depends(current_user),
    vault_id: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    """Resolve an explicitly pinned Vault, or the Session's home Vault.

    Every Vault belongs to the single owner (#412); the selection only picks
    which one the UI is looking at.
    """
    del user  # authentication gate only
    session = request.state.session
    explicit = isinstance(vault_id, int)
    requested_id = vault_id if explicit else session.get("vault_id")
    with db() as connection:
        vault = None
        if requested_id:
            vault = connection.execute(
                f"SELECT v.* FROM vaults v WHERE v.id=%s AND {_OPERATIONAL_VAULT}",
                (requested_id,),
            ).fetchone()
        if not vault and explicit:
            raise HTTPException(404, "Protected Vault not found")
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
        if not explicit and session.get("vault_id") != vault["id"]:
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
    """Update supplied fields only; omitted settings retain their current values."""

    model_config = ConfigDict(extra="forbid")

    auto_upload: bool = True
    upload_verification: Literal["s3_checksum", "read_back"] | None = None
    local_release_after_days: Literal[90, 180, 365, 730] | None = None
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
            action_flags = {
                "upload": row.get("upload_eligible", row["state"] == "pending"),
                "recover": row.get("recover_eligible", row["state"] == "cloud_only"),
                "free-space": row.get("cleanup_eligible", row["state"] == "protected"),
                "cloud-purge": row["state"] == "cloud_only",
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
        from .services.protection_states import aggregate_state
        state = aggregate_state(states)
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
            "delete_enabled": vault_local_access(vault["source_root"]).local_operations_allowed,
            "cloud_deletion_enabled": True,
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
        "stopped_vault": decommission_vault_block,
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


@app.post("/api/vaults/{vault_id}/pause", response_model=response_model("VaultPauseResponse"))
def pause_vault(vault_id: int, request: Request, user: dict[str, Any] = Depends(current_user)):
    return _set_vault_pause(vault_id, True, request, user)


@app.post("/api/vaults/{vault_id}/resume", response_model=response_model("VaultPauseResponse"))
def resume_vault(vault_id: int, request: Request, user: dict[str, Any] = Depends(current_user)):
    return _set_vault_pause(vault_id, False, request, user)


def _set_vault_pause(vault_id: int, paused: bool, request: Request, user: dict[str, Any]):
    from .services import vault_pause
    try:
        with db() as connection:
            return vault_pause.set_paused(connection, vault_id=vault_id, paused=paused,
                                           user_id=int(user["id"]), locale=_request_locale(request))
    except vault_pause.VaultNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/vaults", response_model=response_model("VaultsResponse"))
def list_vaults(_: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        rows = connection.execute(
            f"""
            SELECT v.id, v.slug, v.name, v.paused
            FROM vaults v
            WHERE {_OPERATIONAL_VAULT}
            ORDER BY v.name
            """
        ).fetchall()
    return {"items": rows}


VAULT_ADOPTION_ERROR_KEYS = frozenset(
    f"api.vault_adoption.{reason}"
    for reason in (
        "invalid_path", "invalid_volume", "path_missing", "volume_not_found", "nested_mount",
        "occupied", "overlap", "unreadable", "unwritable", "volume_unavailable",
    )
)


def _vault_create_error(request: Request, status: int, key: str) -> JSONResponse:
    """Localized creation failure: the SPA shows ``message`` and keys on ``message_key``."""
    message = translate(key, locale=_request_locale(request))
    return JSONResponse({"detail": message, "message": message, "message_key": key}, status_code=status)


@app.post("/api/vaults", status_code=201, response_model=response_model("VaultCreateResponse"))
def create_owner_vault(
    action: VaultSelfServiceCreate,
    background_tasks: BackgroundTasks,
    request: Request,
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
    except VaultSlugTaken:
        return _vault_create_error(request, 409, "api.vault_name_taken")
    except VaultProvisioningUnavailable:
        return _vault_create_error(request, 503, "api.vault_provisioning_unavailable")
    except InvalidVaultName:
        return _vault_create_error(request, 422, "api.vault_name_invalid")
    except VaultAdoptionError as exc:
        key = f"api.vault_adoption.{exc.reason}"
        return _vault_create_error(request, 422, key if key in VAULT_ADOPTION_ERROR_KEYS else "api.vault_adoption.failed")
    except VaultCreationError:
        return _vault_create_error(request, 409, "ui.vault_create.failed")
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


@app.get("/api/vaults/{vault_id}/stop-protection", response_model=response_model("StopProtectionStatus"))
def stop_protection_status(vault_id: int, user: dict[str, Any] = Depends(current_user)):
    try:
        with db() as connection:
            return protection_stop_service.status(connection, vault_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/vaults/{vault_id}/stop-protection", status_code=202, response_model=response_model("StopProtectionStatus"))
def stop_folder_protection(vault_id: int, action: StopProtectionRequest, request: Request,
                           user: dict[str, Any] = Depends(current_user)):
    if action.cloud_disposition == "delete":
        require_recent_reauth(request, user)
    operation_lock = scan_lock_for_vault(vault_id)
    if not operation_lock.acquire(blocking=False):
        raise HTTPException(409, "Wait for the Vault scan to finish")
    try:
        with db() as connection:
            return protection_stop_service.stop_protection(
                connection, vault_id=vault_id, actor_user_id=int(user["id"]),
                cloud_disposition=action.cloud_disposition, confirmation=action.confirmation,
                reason=action.reason)
    except cloud_deletion_service.ConfirmationRequired as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    finally:
        operation_lock.release()


@app.post("/api/vaults/{vault_id}/stop-protection/cancel-deletion", response_model=response_model("StopProtectionStatus"))
def cancel_stopped_folder_deletion(vault_id: int, user: dict[str, Any] = Depends(current_user)):
    try:
        with db() as connection:
            return protection_stop_service.cancel_deletion(connection, vault_id=vault_id,
                                                            actor_user_id=int(user["id"]))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


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


def _protection_observation(connection: Any, vault: dict[str, Any], access: Any) -> tuple[bool, bool]:
    from .services.component_health import load_vault_health
    from .services.protection_states import observation_is_unconfirmed
    from .services.operation_policies import get_policy

    health = load_vault_health(connection, int(vault["id"]))
    unconfirmed = (
        not access.local_operations_allowed
        or vault.get("relocation_state", "ready") != "ready"
        or observation_is_unconfirmed(health)
    )
    suspended = not get_policy(connection, int(vault["id"])).auto_upload
    return unconfirmed, suspended


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
    access = vault_local_access(vault.get("source_root") or "")
    with db() as connection:
        unconfirmed, suspended = _protection_observation(connection, vault, access)
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
        "protection_suspended": suspended,
    }


@app.get("/api/files/detail", response_model=response_model("ArchiveListItem"), responses=LOGICAL_PATH_ERROR_RESPONSES)
def file_detail(path: str, vault: dict[str, Any] = Depends(current_vault)):
    path = safe_relative_path(path).as_posix()
    with db() as connection:
        access = vault_local_access(vault.get("source_root") or "")
        unconfirmed, _ = _protection_observation(connection, vault, access)
        catalog = ArchiveCatalog(connection, protection_unconfirmed=unconfirmed)
        rows = connection.execute(catalog._file_row_select_sql([
            "vf.vault_id=%s", "fp.vault_id=vf.vault_id", "vf.status='active'", "fp.path=%s",
        ], vault["id"]), (vault["id"], path)).fetchall()
        items = catalog._materialize_file_items(vault["id"], rows, name_from_path=True)
        if not items:
            raise HTTPException(404, "Vault File not found")
        return items[0]


@app.get("/api/files/search", response_model=response_model("FilesResponse"))
def search_all_files(
    q: str = "", state: str = "", page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=10, le=500),
    _: dict[str, Any] = Depends(current_user),
):
    """Search name/path across all protected Vaults, independent of Session choice."""
    with db() as connection:
        vaults = connection.execute(f"SELECT v.* FROM vaults v WHERE {_OPERATIONAL_VAULT}").fetchall()
        scoped = []
        for vault in vaults:
            access = vault_local_access(vault.get("source_root") or "")
            unconfirmed, _suspended = _protection_observation(connection, vault, access)
            scoped.append({**vault, "unconfirmed": unconfirmed})
        return ArchiveCatalog(connection).search_protected_vaults(
            scoped, search=q, state=state, page=page, page_size=page_size,
        )


@app.get("/api/upload-activity", response_model=response_model("UploadActivityResponse"))
def upload_activity_overview(_: dict[str, Any] = Depends(current_user)):
    from .services.upload_activity import upload_activity
    with db() as connection:
        return upload_activity(connection)


@app.get("/api/general", response_model=response_model("GeneralOverview"))
def general_overview(request: Request, user: dict[str, Any] = Depends(current_user)):
    from .services.essential_interface import overview
    with db() as connection:
        return overview(connection, user_id=user["id"], locale=_request_locale(request))


@app.get("/api/problems", response_model=response_model("NotificationsResponse"))
def open_problems(request: Request, page: int = Query(1, ge=1),
                  user: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        notification_service.reconcile_problem_states(connection)
        rows = connection.execute(
            "SELECT * FROM notifications WHERE user_id=%s AND in_app_enabled=TRUE "
            "AND problem_status='open' ORDER BY id DESC LIMIT 100 OFFSET %s",
            (user["id"], (page - 1) * 100),
        ).fetchall()
        return {"items": [notification_service._row_notification(row, locale=_request_locale(request)) for row in rows],
                "open_problem_count": notification_service.count_open_problems(connection, user_id=user["id"])}


@app.get("/api/stats", response_model=response_model("StatsResponse"))
def stats(vault: dict[str, Any] = Depends(current_vault)):
    source_root = vault.get("source_root") or ""
    # Reconcile Source Volume identity before anything can preflight or walk the
    # configured Vault tree. Catalog summaries remain available when local work
    # is suspended, so they are intentionally collected after this gate too.
    access = vault_local_access(source_root)
    with db() as connection:
        # Cheap SQL aggregates only — never materialize every Vault File row.
        unconfirmed, suspended = _protection_observation(connection, vault, access)
        summary = ArchiveCatalog(connection, protection_unconfirmed=unconfirmed).summary(vault["id"])
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
        "protection_suspended": suspended,
        "runtime": runtime,
        "source_volume": {
            "alias": access.volume_alias,
            "health": access.volume_health,
            "local_operations_allowed": access.local_operations_allowed,
        },
        "delete_enabled": access.local_operations_allowed,
    }


@app.get("/api/notifications", response_model=response_model("NotificationsResponse"))
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
        open_problem_count = notification_service.count_open_problems(connection, user_id=user["id"])
    return {"items": items, "unread_count": unread_count, "has_more": has_more,
            "open_problem_count": open_problem_count,
            "email_configured": notification_service.email_configured()}


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


class RecoveryKitCustody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fingerprint: str = Field(min_length=24, max_length=24, pattern="^[0-9a-f]+$")


@app.get("/api/recovery-kit", response_model=response_model("RecoveryKitStatus"))
def recovery_kit_status(_: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        return recovery_kit_service.kit_status(connection)


@app.get("/api/recovery-kit/download", response_class=Response, responses={200: {
    "description": "Secret-bearing installation Recovery Kit (never cache)",
    "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
}})
def download_recovery_kit(
    user: dict[str, Any] = Depends(current_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    try:
        payload = recovery_kit_service.build_recovery_kit()
    except recovery_kit_service.RecoveryKitUnavailable as exc:
        raise HTTPException(409, str(exc)) from None
    with db() as connection:
        audit_event_store.record_audit_event(connection, event="recovery_kit_downloaded",
                           actor_user_id=int(user["id"]), outcome="success", visibility="owner")
    return Response(payload, media_type="application/octet-stream", headers={
        "Content-Disposition": 'attachment; filename="frostvault-recovery-kit.json"',
        "Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff",
    })


@app.post("/api/recovery-kit/custody", response_model=response_model("RecoveryKitStatus"))
def acknowledge_recovery_kit(
    action: RecoveryKitCustody,
    user: dict[str, Any] = Depends(current_user),
):
    with db() as connection:
        try:
            recovery_kit_service.acknowledge_custody(connection, action.fingerprint)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        audit_event_store.record_audit_event(connection, event="recovery_kit_custody_acknowledged",
                           actor_user_id=int(user["id"]), outcome="success", visibility="owner",
                           fingerprint=action.fingerprint)
        return recovery_kit_service.kit_status(connection)


@app.get("/api/general/metadata-backups", response_model=JsonObjectResponse)
def list_metadata_backups(_: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        status = metadata_backup_service.backup_status(connection)
        runs = metadata_backup_service.list_backup_artifacts(connection)
    return {"status": status, "runs": runs}


@app.post("/api/general/metadata-backups/run", response_model=JsonObjectResponse)
def run_metadata_backup(
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
                "resolved_failed_count": 0,
                "unresolved_failed_count": 0,
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
            resolved = (row.get("remediation") or {}).get("state") == "resolved"
            group["resolved_failed_count" if resolved else "unresolved_failed_count"] += 1
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


@app.get("/api/jobs", response_model=response_model("JobsResponse"), responses=LOGICAL_PATH_ERROR_RESPONSES)
def jobs(
    request: Request,
    vault: dict[str, Any] = Depends(current_vault),
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
    from .services.job_queries import list_jobs
    logical_path = safe_relative_path(path).as_posix() if path is not None else None
    with db() as connection:
        return list_jobs(connection, vault_id=vault["id"], locale=_request_locale(request),
                         actions=action, active_only=active_only, path=logical_path,
                         groups_before_id=groups_before_id)


@app.get("/api/job-groups/{group_id}/jobs", response_model=response_model("FileJobHistoryResponse"))
def job_group_detail(
    request: Request, group_id: str,
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=50),
    vault: dict[str, Any] = Depends(current_vault),
):
    """Newest-first keyset page of one group's Jobs, scoped to this Vault."""
    from .services.job_queries import group_jobs
    with db() as connection:
        return group_jobs(connection, vault_id=vault["id"], group_id=group_id,
                          locale=_request_locale(request), before_id=before_id, limit=limit)


_JOB_HISTORY_COLUMNS = "id,path,action,status,message,message_key,message_params,requested_at,updated_at,group_id,group_path,total_bytes,transferred_bytes,archive_version_id,upload_reconcile_only"


@app.get("/api/jobs/{job_id}", response_model=response_model("JobRecord"))
def job_detail(request: Request, job_id: int, vault: dict[str, Any] = Depends(current_vault)):
    from .services.upload_remediation import project_jobs
    with db() as connection:
        row = connection.execute(f"SELECT {_JOB_HISTORY_COLUMNS} FROM jobs WHERE id=%s AND vault_id=%s", (job_id,vault["id"])).fetchone()
        if not row:
            raise HTTPException(404,"Job not found in this Vault")
        projected = project_jobs(connection,[row])[0]
    return {**projected,"message":present_job_message(row,_request_locale(request))}


@app.get("/api/files/jobs", response_model=response_model("FileJobHistoryResponse"), responses=LOGICAL_PATH_ERROR_RESPONSES)
def file_job_history(request: Request, path: str = Query(), before_id: int | None = Query(default=None,ge=1),
                     limit: int = Query(default=50,ge=1,le=50), vault: dict[str, Any] = Depends(current_vault)):
    from .services.upload_remediation import project_jobs
    logical_path = safe_relative_path(path).as_posix()
    with db() as connection:
        file = connection.execute("SELECT vault_file_id FROM file_paths WHERE vault_id=%s AND path=%s AND valid_to IS NULL",(vault["id"],logical_path)).fetchone()
        if not file:
            raise HTTPException(404,"Vault File not found")
        rows = connection.execute(f"SELECT {_JOB_HISTORY_COLUMNS} FROM jobs WHERE vault_id=%s AND vault_file_id=%s AND id<%s ORDER BY id DESC LIMIT %s",
            (vault["id"],file["vault_file_id"],before_id or 2**63-1,limit+1)).fetchall()
        items = project_jobs(connection,rows[:limit])
    return {"items":[{**row,"message":present_job_message(row,_request_locale(request))} for row in items],
            "next_before_id":items[-1]["id"] if len(rows)>limit else None}


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
            explicit_vault="vault_id" in request.query_params,
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


# A Vault directory may mix GLACIER and DEEP_ARCHIVE versions, so one request
# tier must be valid for every class that needs a restore (no Expedited).
DIRECTORY_RESTORE_TIERS = ("Standard", "Bulk")


def resolve_directory_restore(
    tier: str | None, days: int | None
) -> tuple[str, int]:
    """Resolve the tier and days one directory recovery applies to every Job.

    The directory Cost Estimate and the queued Jobs share this so the dialog,
    the estimate and the actual restore agree (#532).
    """
    runtime = _runtime_settings()
    requested = (tier or "").strip()
    if requested:
        if requested not in DIRECTORY_RESTORE_TIERS:
            raise HTTPException(422, "Choose Standard or Bulk")
        resolved_tier = requested
    else:
        resolved_tier = normalize_restore_tier(
            runtime.restore_tier, storage_class="DEEP_ARCHIVE"
        )
    resolved_days = int(days if days is not None else runtime.restore_days)
    return resolved_tier, resolved_days


# A version whose cloud restore is already requested or done costs nothing more
# to recover, so it never counts toward a Restore Approval (#534).
_RESTORE_ALREADY_REQUESTED = frozenset({"restoring", "available"})


def recovery_request_impact(
    rows: Iterable[Mapping[str, Any]],
    *,
    tier: str | None = None,
    days: int | None = None,
) -> dict[str, Any]:
    """Price one whole recovery request and decide if it needs Restore Approval.

    ``rows`` carry ``bytes``, ``storage_class`` and ``restore_state`` (one per
    file or aggregated per class), optionally with their own ``restore_tier``
    and ``restore_days``. The thresholds apply to the request's total, so a
    folder of many small archived files is held like one large file (#533).
    The directory Cost Estimate and queueing share this so the dialog and the
    queued Jobs agree on ``high_impact``.
    """
    runtime = _runtime_settings()
    prices = list_prices(runtime.aws_region)
    size_bytes = 0
    cost = 0.0
    hours = 0.0
    priced = False
    for row in rows:
        storage_class = row.get("storage_class")
        if not storage_class_requires_restore(storage_class):
            continue
        if (row.get("restore_state") or "") in _RESTORE_ALREADY_REQUESTED:
            continue
        estimate = estimate_restore_cost(
            prices,
            size_bytes=int(row.get("bytes") or 0),
            storage_class=str(storage_class),
            tier=str(row.get("restore_tier") or tier or runtime.restore_tier or "Bulk"),
            days=int(row.get("restore_days") or days or runtime.restore_days or 3),
        )
        priced = True
        size_bytes += estimate.size_bytes
        cost += estimate.estimated_cost_eur
        hours = max(hours, float(estimate.estimated_hours or 0))
    return {
        "requires_restore": priced,
        "size_bytes": size_bytes,
        "estimated_cost_eur": cost,
        "estimated_hours": hours,
        "high_impact": priced and is_high_impact_restore(
            size_bytes=size_bytes,
            estimated_cost_eur=cost,
            size_threshold_gib=runtime.restore_high_impact_gib,
            cost_threshold_eur=runtime.restore_high_impact_eur,
        ),
    }


HIGH_IMPACT_RESTORE_HELD_MESSAGE = (
    "High-impact Glacier restore held for primary-owner approval; "
    "RestoreObject cannot be cancelled after AWS accepts it"
)


def _hold_high_impact_recovery(connection: Any, job_ids: list[int]) -> dict[str, Any]:
    """Evaluate Restore Approval once, at queue time, for a whole group.

    Runs in the queueing transaction, so no worker can claim a Job before the
    group is either released as ``queued`` or held as ``pending_approval``.
    The worker never re-evaluates the thresholds (#534): one approval per
    ``group_id`` releases every Job.
    """
    rows = connection.execute(
        """
        SELECT av.storage_class, av.restore_state, j.restore_tier, j.restore_days,
               sum(j.total_bytes) AS bytes
        FROM jobs j JOIN archive_versions av ON av.id=j.archive_version_id
        WHERE j.id=ANY(%s)
        GROUP BY av.storage_class, av.restore_state, j.restore_tier, j.restore_days
        """,
        (job_ids,),
    ).fetchall()
    impact = recovery_request_impact(rows)
    if not impact["requires_restore"]:
        return impact
    timestamp = now_iso()
    # Every Job carries the request's Cost Estimate, which the group shows.
    connection.execute(
        """
        UPDATE jobs SET estimated_cost_eur=%s, estimated_hours=%s
        WHERE id=ANY(%s)
        """,
        (impact["estimated_cost_eur"], impact["estimated_hours"], job_ids),
    )
    if impact["high_impact"]:
        hold_seconds = int(_runtime_settings().restore_approval_hold_seconds)
        pending_until = (
            datetime.now(timezone.utc) + timedelta(seconds=hold_seconds)
        ).isoformat()
        connection.execute(
            """
            UPDATE jobs
            SET status='pending_approval', message=%s, pending_until=%s, updated_at=%s
            WHERE id=ANY(%s) AND status='queued'
            """,
            (HIGH_IMPACT_RESTORE_HELD_MESSAGE, pending_until, timestamp, job_ids),
        )
        notification_service.enqueue_recovery_approval_best_effort(connection, job_ids=job_ids)
    return impact


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
    recovery_selection: list[tuple[str, bool]] | None = None,
) -> dict[str, Any]:
    if recovery_selection:
        recovery_selection = [(safe_relative_path(p).as_posix(), directory) for p, directory in recovery_selection]
        if action != "recover" or archive_version_id or whole_vault:
            raise HTTPException(422, "Invalid recovery selection")
        logical_path = ""
    elif whole_vault:
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
        if protection_stop_service.local_work_suspended(vault_row):
            raise HTTPException(409, "Vault is no longer protected")
        if vault_relocation_service.local_work_suspended(vault_row):
            raise HTTPException(409, "Local work is suspended pending reconnect scan")
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
    directory_recover = action == "recover" and (is_directory or whole_vault or bool(recovery_selection))
    if directory_recover:
        resolved_tier, resolved_days = resolve_directory_restore(
            restore_tier, restore_days
        )
    with db() as connection:
        if action == "free-space" and not local_release_is_confirmed(connection, vault_id):
            raise HTTPException(409, "Local release blocked: protection is not confirmed")
        catalog = ArchiveCatalog(connection)
        if action in {"recover", "storage-class"} and not is_directory and not whole_vault and not recovery_selection:
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
            recovery_selection=recovery_selection,
        )
        skipped_same_class = int(getattr(catalog, "last_skipped_same_class", 0) or 0)
        if directory_recover and not (
            job_ids
            and connection.execute(
                "SELECT 1 FROM jobs WHERE id=ANY(%s) AND restore_tier IS NOT NULL LIMIT 1",
                (job_ids,),
            ).fetchone()
        ):
            # Nothing queued needs a cloud restore; echo what was persisted.
            resolved_tier = resolved_days = None
        approval_required = False
        if action == "recover" and job_ids:
            impact = _hold_high_impact_recovery(connection, job_ids)
            approval_required = bool(impact["high_impact"])
            if impact["requires_restore"]:
                estimated_cost_eur = impact["estimated_cost_eur"]
                estimated_hours = impact["estimated_hours"]
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
    if action == "recover":
        result["approval_required"] = approval_required
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
        from .services.upload_provenance import describe
        reconciliation = describe(connection, vault_id=vault["id"], path=logical_path)
        from .services.upload_remediation import file_projection
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
    selection = [(safe_relative_path(item.path).as_posix(), item.is_directory) for item in action.selection] if action.selection else None
    logical_path = "" if selection else safe_relative_path(action.path).as_posix()
    if action.is_directory or selection:
        recoverable_join = """LEFT JOIN archive_versions av ON av.id=(
            SELECT recoverable.id FROM archive_versions recoverable
            WHERE recoverable.vault_file_id=vf.id AND recoverable.integrity='verified'
              AND recoverable.availability='available' ORDER BY recoverable.version_number DESC LIMIT 1
        )"""
        scope_sql, scope_params = ArchiveCatalog.recovery_selection_scope(selection or [(logical_path, True)])
        with db() as connection:
            classes = connection.execute(
                f"SELECT av.storage_class, av.restore_state, sum(av.size) AS bytes FROM vault_files vf "
                f"JOIN file_paths fp ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL "
                f"LEFT JOIN local_copies lc ON lc.vault_file_id=vf.id {recoverable_join} "
                f"WHERE vf.vault_id=%s AND fp.vault_id=vf.vault_id AND vf.status='active' AND {scope_sql} "
                "AND av.integrity='verified' AND av.availability='available' "
                "AND NOT EXISTS (SELECT 1 FROM jobs pending WHERE pending.vault_file_id=vf.id "
                "AND pending.status NOT IN ('completed','failed','cancelled')) "
                "AND (lc.presence IS NULL OR lc.presence<>'present') GROUP BY av.storage_class, av.restore_state",
                [vault["id"], *scope_params],
            ).fetchall()
        tier, days = resolve_directory_restore(action.restore_tier, action.restore_days)
        estimates = [estimate_restore_cost(list_prices(settings.aws_region), size_bytes=int(row["bytes"]),
                     storage_class=row["storage_class"], tier=tier, days=days)
                     for row in classes if storage_class_requires_restore(row["storage_class"])]
        # Only new cloud restores are charged; already-requested versions must
        # not inflate the selection's price or approval threshold (#534).
        impact = recovery_request_impact(classes, tier=tier, days=days)
        total_cost = impact["estimated_cost_eur"]
        high_impact = impact["high_impact"]
        return {"path": logical_path, "archive_version_id": None,
                "requires_restore": bool(estimates), "restore_object_irreversible": bool(estimates), "high_impact": high_impact,
                "estimate": {"tier": tier, "days": days,
                             "estimated_cost_eur": total_cost,
                             "estimated_hours": max((item.estimated_hours or 0 for item in estimates), default=0)} if estimates else None}
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
        high_impact = recovery_request_impact(
            [{
                "bytes": int(selected.get("size") or 0),
                "storage_class": selected.get("storage_class"),
                "restore_state": selected.get("restore_state"),
            }],
            tier=tier,
            days=days,
        )["high_impact"]
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
    action: UploadAction,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
):
    if action.is_directory:
        from .services import bulk_upload_admission
        path = normalize_directory(action.path, api=True)
        try:
            with db() as connection:
                admission = bulk_upload_admission.create(
                    connection, vault_id=vault["id"], path=path, user_id=user["id"],
                    request_id=str(action.request_id) if action.request_id else None)
        except (ValueError, LookupError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"group_id": admission["group_id"], "job_ids": [], "item_count": admission["admitted_count"],
                "total_bytes": admission["total_bytes"], "admission": admission,
                **_api_message(request, "api.upload_admission_started")}
    queued = queue_jobs(action.path, "upload", vault["id"], user["id"], False)
    return {**queued, **_api_message(request, "api.upload_started")}


@app.get("/api/upload/admissions/{group_id}", response_model=response_model("BulkUploadAdmissionResponse"))
def upload_admission_status(group_id: uuid.UUID, _: dict[str, Any] = Depends(current_user),
                            vault: dict[str, Any] = Depends(current_vault)):
    from .services import bulk_upload_admission
    with db() as connection:
        result = bulk_upload_admission.get(connection, vault_id=vault["id"], group_id=str(group_id))
    if result is None:
        raise HTTPException(404, "Upload admission not found")
    return result


@app.post("/api/upload/reconcile", status_code=202, response_model=response_model("UploadReconciliationResponse"), responses=LOGICAL_PATH_ERROR_RESPONSES)
def reconcile_upload(action: UploadReconciliationAction, user: dict[str, Any] = Depends(current_user), vault: dict[str, Any] = Depends(current_vault)):
    """Exact resume only, including protected files for which upload returns 409.

    Common target arbiter; no arbitrary deletion or cancellation resurrection. It
    never PUTs by itself: the Job may send only when S3 positively proves the
    earlier send never landed (ADR-0025), and never on a path with paused protection.
    """
    from .services.upload_provenance import schedule_resume, UploadBlocked
    logical_path = safe_relative_path(action.path).as_posix()
    if action.is_directory:
        raise HTTPException(422, "Reconciliation requires one stable Vault File")
    if not vault_local_access(vault["source_root"]).local_operations_allowed:
        raise HTTPException(503, "Expected Source Volume is unavailable")
    try:
        with db() as connection:
            return schedule_resume(connection, vault=vault, path=logical_path, user_id=user["id"], timestamp=datetime.now(timezone.utc).isoformat(), remediation_id=action.remediation_id)
    except UploadBlocked as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/upload/retry-failed", status_code=202, response_model=response_model("RetryFailedUploadsResponse"))
def retry_failed_uploads(vault_id: int | None = Query(default=None, ge=1),
                         _: dict[str, Any] = Depends(current_user)):
    """Explicit scope: omitted Vault means installation, never Session choice."""
    from .storage import queue_vault_auto_uploads, _ScanRootMismatch
    with db() as connection:
        vaults = connection.execute(
            "SELECT * FROM vaults" + (" WHERE id=%s" if vault_id is not None else "") + " ORDER BY id",
            (vault_id,) if vault_id is not None else (),
        ).fetchall()
    if vault_id is not None and not vaults:
        raise HTTPException(404, "Vault not found")
    count = 0
    for vault in vaults:
        try:
            count += queue_vault_auto_uploads(dict(vault), retry_failed_only=True)
        except (_ScanRootMismatch, OSError):
            # Missing/replaced roots remain ineligible, not implicitly reconnected.
            continue
    return {"item_count": count}


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
        connection.execute("SELECT id FROM vaults WHERE id=%s FOR UPDATE", (vault["id"],))
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
        stopped_admissions = 0
        if job_action == "upload":
            from .services import bulk_upload_admission
            stopped_admissions = bulk_upload_admission.cancel(connection, vault_id=vault["id"], group_id=group_id)
        if not job_ids and not stopped_admissions:
            raise HTTPException(409, "The operation is no longer running")
        if job_ids:
            _cancel_active_jobs(connection, vault, job_action, job_ids, actor_user_id)
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
    actor_user_id: int,
) -> None:
    """Cancel already-selected non-terminal Jobs; the caller holds the Vault lock."""
    message_keys = {
        "upload": "job.upload_stopped",
        "recover": "job.recovery_stopped",
        "free-space": "job.cleanup_stopped",
        "rename": "job.rename_stopped",
        "cloud-purge": "job.cloud_purge_stopped",
        "storage-class": "job.storage_class_stopped",
    }
    message_key = message_keys[job_action]
    message = translate(message_key, locale=DEFAULT_LOCALE)
    automatic_cleanup_rows = []
    if job_action == "free-space":
        automatic_cleanup_rows = connection.execute(
            """
            SELECT id, path, requested_by, archive_version_id
            FROM jobs
            WHERE vault_id=%s AND id = ANY(%s) AND action='free-space'
              AND origin='automatic'
              AND status NOT IN ('completed', 'failed', 'cancelled')
            """,
            (vault["id"], job_ids),
        ).fetchall()
    cancel_jobs(job_ids)
    connection.execute(
        """
        UPDATE jobs
        SET status='cancelled',
            message=%s,
            message_key=%s,
            message_params=%s,
            updated_at=%s
        WHERE id = ANY(%s)
          AND status NOT IN ('completed', 'failed', 'cancelled')
        """,
        [message, message_key, "{}", now_iso(), job_ids],
    )
    if job_action == "upload":
        from .services import upload_remediation
        for job_id in job_ids:
            upload_remediation.transition(connection, job_id, "cancelled", now_iso())
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


class PendingUploadCancelAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(default="", max_length=1024)


# An upload that has not begun: waiting in the queue, never admitted to the
# upload provenance machinery, nothing linked in the cloud, not part of a
# remediation episode. Cancelling anything else could close an ambiguous send
# for good (ADR-0024/0025), so it stays with the single-Job cancel and Resume.
_UNSTARTED_UPLOAD_SQL = """
    j.vault_id=%s AND j.action='upload'
    AND j.status IN ('queued', 'retrying')
    AND NOT COALESCE(j.upload_reconcile_only, FALSE)
    AND j.archive_version_id IS NULL
    AND NOT EXISTS (SELECT 1 FROM upload_intent_jobs b WHERE b.job_id=j.id)
    AND NOT EXISTS (SELECT 1 FROM upload_intents i
                    WHERE i.vault_file_id=j.vault_file_id AND i.send_started AND NOT i.settled)
    AND NOT EXISTS (SELECT 1 FROM upload_remediation_jobs rj WHERE rj.job_id=j.id)
    AND NOT EXISTS (SELECT 1 FROM upload_remediation_originals ro WHERE ro.original_job_id=j.id)
    AND NOT EXISTS (SELECT 1 FROM upload_remediations r
                    WHERE r.vault_file_id=j.vault_file_id AND r.state NOT IN ('resolved', 'cancelled'))
"""


@app.post(
    "/api/upload/cancel-pending",
    response_model=response_model("JobCancelResponse"),
    responses=LOGICAL_PATH_ERROR_RESPONSES,
)
def cancel_pending_uploads(
    action: PendingUploadCancelAction,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
):
    """Cancel the uploads of the selected Vault (or one folder) that have not begun.

    One call replaces thousands of per-file cancels after a burst or a moved
    folder (issue #493). An empty ``path`` means the whole Vault; a folder
    matches itself and its descendants only (``foo`` never matches ``foobar``).
    Only uploads that never started are cancelled: running or verifying ones,
    reconcile-only attempts, jobs with a started or recorded send and jobs of a
    remediation episode are left to their own cancel and Resume, so an ambiguous
    send is never closed for good by a bulk action. A cancelled automatic upload
    is not re-queued until the file's content changes, like a single cancel.
    """
    folder = normalize_directory(action.path, api=True)
    from .services import bulk_upload_admission
    with db() as connection:
        connection.execute("SELECT id FROM vaults WHERE id=%s FOR UPDATE", (vault["id"],))
        stopped_admissions = bulk_upload_admission.cancel(connection, vault_id=vault["id"], path=folder)
        scope_sql, scope_params = "", []
        if folder:
            scope_sql = "AND (j.path=%s OR starts_with(j.path, %s))"
            scope_params = [folder, folder + "/"]
        rows = connection.execute(
            f"SELECT j.id FROM jobs j WHERE {_UNSTARTED_UPLOAD_SQL} {scope_sql} ORDER BY j.id",
            [vault["id"], *scope_params],
        ).fetchall()
        job_ids = [int(row["id"]) for row in rows]
        if job_ids:
            _cancel_active_jobs(connection, vault, "upload", job_ids, user["id"])
    return {
        "message": translate("job.upload_stopped", locale=DEFAULT_LOCALE),
        "cancelled_count": len(job_ids),
        "stopped_admissions": stopped_admissions,
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
    action: RecoverRequest,
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
        recovery_selection=[(item.path, item.is_directory) for item in action.selection] if action.selection else None,
    )
    return {**queued, **_api_message(request, "api.recovery_started")}


@app.post("/api/recover/approve", status_code=202, response_model=JsonObjectResponse)
def approve_recover(
    action: RecoverApproveAction,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
):
    from .services.recovery_approval import expire_pending

    with db() as connection:
        connection.execute("SELECT id FROM vaults WHERE id=%s FOR UPDATE", (vault["id"],))
        # Read the clock AFTER the lock: waiting for another decision must never
        # allow a late approval. Keep subsecond precision at the deadline.
        timestamp = datetime.now(timezone.utc).isoformat()
        expire_pending(connection, vault_id=vault["id"], group_id=action.group_id, now=timestamp)
        rows = connection.execute(
            """
            UPDATE jobs SET status='queued', approved_by=%s, approved_at=%s,
                pending_until=NULL, message=%s, updated_at=%s
            WHERE vault_id=%s AND group_id=%s AND action='recover'
              AND status='pending_approval'
            RETURNING id
            """,
            (user["id"], timestamp,
             "High-impact restore approved; RestoreObject cannot be cancelled after AWS accepts it",
             timestamp, vault["id"], action.group_id),
        ).fetchall()
        job_ids = [int(row["id"]) for row in rows]
    # Commit expiration even when the approval is refused.
    if not job_ids:
        raise HTTPException(409, "No high-impact restore is waiting for approval (it may have expired)")
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
    queued = queue_jobs(
        action.path, "free-space", vault["id"], user["id"], action.is_directory
    )
    return {**queued, **_api_message(request, "api.free_space_started")}


def _cloud_deletion_paths(action: CloudDeletionPreviewRequest | CloudPurgeRequest) -> list[str]:
    if action.paths:
        return list(action.paths)
    return [action.path]


@app.get("/api/cloud-deletion", response_model=response_model("CloudDeletionSettings"))
def cloud_deletion_settings(user: dict[str, Any] = Depends(current_user)):
    return {"purge_delay_seconds": cloud_deletion_service.DELETION_DELAY_SECONDS}


@app.post("/api/cloud-deletion/preview", response_model=response_model("CloudDeletionPreview"))
def preview_cloud_deletion(
    action: CloudDeletionPreviewRequest,
    vault: dict[str, Any] = Depends(current_vault),
):
    try:
        with db() as connection:
            preview = cloud_deletion_service.preview_selection(
                connection, vault_id=vault["id"], paths=_cloud_deletion_paths(action),
                is_directory=action.is_directory)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
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
            )
    except cloud_deletion_service.ConfirmationRequired as exc:
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


@app.get("/api/general/source-volumes", response_model=response_model("SourceVolumeInventoryResponse"))
def general_source_volumes(user: dict[str, Any] = Depends(current_user)):
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


@app.get("/api/general/vaults", response_model=response_model("GeneralVaultsResponse"))
def general_vaults(_: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        return {"items": list_vault_inventory(connection)}


_VAULT_RECONNECT_ERROR_STATUS: dict[str, tuple[int, str]] = {
    "not_found": (404, "Vault not found"),
    "reason_required": (422, "A reason between 3 and 500 characters is required"),
    "invalid_path": (422, "Reconnect destination is invalid"),
    "not_directory": (422, "Reconnect destination is not a directory"),
    "volume_unavailable": (409, "Destination Source Volume is unavailable"),
    "different_volume": (409, "Destination crosses a nested mount"),
    "symlink": (409, "Reconnect paths cannot contain symbolic links"),
    "inaccessible": (409, "Reconnect destination is inaccessible"),
    "overlap": (409, "Reconnect destination overlaps another Vault"),
    "active_jobs": (409, "Vault has active Jobs or a scan"),
    "identity_ambiguous": (409, "Source Volume identity is unavailable"),
    "identity_mismatch": (409, "Destination changed during reconnect"),
    "protection_stopped": (409, "Folder protection has stopped; reconnect is unavailable"),
}


@app.post("/api/vaults/{vault_id}/reconnect", response_model=response_model("GeneralVaultReconnectResponse"))
def reconnect_vault_root(
    vault_id: int,
    action: VaultReconnect,
    background_tasks: BackgroundTasks,
    owner: dict[str, Any] = Depends(current_user),
):
    """Accept this folder/mount for this Vault only; mandatory cloud/local scan."""
    # Serialize the runtime snapshot, relocation transaction, and scan start.
    # Without this lock a scan can begin after the ``busy`` snapshot but before
    # relocation publishes its persistent suspension state.
    relocation_lock = scan_lock_for_vault(vault_id)
    if not relocation_lock.acquire(blocking=False):
        status_code, message = _VAULT_RECONNECT_ERROR_STATUS["active_jobs"]
        raise HTTPException(status_code, message)
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
                    actor_user_id=int(owner["id"]),
                    reason=action.reason,
                    runtime_busy=busy,
                )
        except vault_relocation_service.VaultRelocationError as exc:
            status_code, message = _VAULT_RECONNECT_ERROR_STATUS.get(
                exc.reason, (400, "Vault reconnect could not be completed")
            )
            raise HTTPException(status_code, message) from exc
        except Exception:
            # A failed outer transaction/commit must not leave the in-process
            # gate stuck when persistent acceptance rolled back.
            vault_relocation_service.release_relocation_scan_runtime(vault_id)
            raise
    finally:
        relocation_lock.release()
    # BackgroundTasks registration is non-I/O and cannot partially dispatch.
    # A crash before execution is recovered by scan_all_vaults on next start;
    # relocation_state remains scan_required until local scan success.
    background_tasks.add_task(scan_vault, dict(vault))
    return {
        "vault_id": vault_id,
        "source_root": vault["source_root"],
        "reconnect_state": vault["relocation_state"],
        "full_scan_required": True,
    }


@app.get("/api/vault/operation-policy", response_model=response_model("OperationPolicy"))
def own_vault_operation_policy(vault: dict[str, Any] = Depends(current_vault)):
    with db() as connection:
        policy = get_policy(connection, vault["id"])
        return {**policy.as_dict(), "global_upload_verification": settings.upload_verification,
                "effective_upload_verification": policy.effective_upload_verification()}


@app.put("/api/vault/operation-policy", response_model=response_model("OperationPolicy"))
def update_vault_operation_policy(
    action: OperationPolicyUpdate,
    vault: dict[str, Any] = Depends(current_vault),
    user: dict[str, Any] = Depends(current_user),
):
    with db() as connection:
        try:
            # Independent panels must never replay a stale local-release value
            # when saving upload rules. Serialize read/merge/write per Vault.
            connection.execute("UPDATE vaults SET name=name WHERE id=%s", (vault["id"],))
            before = get_policy(connection, vault["id"])
            values = before.as_dict()
            values.update(action.model_dump(exclude_unset=True))
            stored = set_policy(connection, vault["id"], OperationPolicy(**values))
            if (before.include_globs, before.exclude_globs) != (stored.include_globs, stored.exclude_globs):
                # Excluded is derived from these globs (#498); refresh the
                # durable directory rollups, never the catalog evidence.
                request_vault_rebuild(connection, vault["id"])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except LookupError as exc:
            reason = str(exc)
            if reason == "vault_quiesced":
                raise HTTPException(409, "Vault is no longer protected") from exc
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
    return {**stored.as_dict(), "global_upload_verification": settings.upload_verification,
            "effective_upload_verification": stored.effective_upload_verification()}


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


class VaultMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vault_id: int = Field(gt=0)
    target_class: str = Field(min_length=1, max_length=40)
    acknowledge_costs: bool = False


@app.get("/api/vault/move", response_model=response_model("VaultMovePreview"))
def preview_vault_move(vault: dict[str, Any] = Depends(current_vault)):
    try:
        with db() as connection:
            return vault_moves.preview(connection, vault["id"])
    except vault_moves.MoveBlocked as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/vault/move", response_model=response_model("VaultMoveResponse"))
def move_vault(action: VaultMoveRequest, vault: dict[str, Any] = Depends(current_vault),
               user: dict[str, Any] = Depends(current_user)):
    if action.vault_id != vault["id"]:
        raise HTTPException(409, "Selected Vault changed; refresh the move preview")
    try:
        with db() as connection:
            return vault_moves.schedule(connection, vault_id=vault["id"], target=action.target_class,
                user_id=user["id"], acknowledged=action.acknowledge_costs, timestamp=now_iso())
    except vault_moves.MoveBlocked as exc:
        raise HTTPException(409, str(exc)) from exc


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
    # Public probes expose dependency booleans, never workload/queue internals.
    return JSONResponse(
        {"status": report["status"], "checks": report["checks"]},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@app.get(
    "/metrics",
    response_class=Response,
    dependencies=[Depends(require_metrics_token)],
    responses={
        200: {
            "description": "Prometheus text exposition",
            "content": {"text/plain": {"schema": {"type": "string"}}},
        },
        401: {"description": "Missing or invalid independent Bearer credential"},
        404: {"description": "Metrics disabled (METRICS_TOKEN is empty)"},
    },
)
def metrics():
    """Prometheus exposition; disabled without METRICS_TOKEN, Bearer-only when set."""
    return Response(
        content=metrics_service.render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


_SPA_FALLBACK_EXCLUDED_PREFIXES = frozenset({
    "api", "auth", "static", "assets",
    # Disabled diagnostics must remain 404, never the SPA or a built public file.
    "docs", "redoc", "openapi.json", "metrics",
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
    if head in _SPA_FALLBACK_EXCLUDED_PREFIXES or head == "admin" or full_path.startswith("vault/access"):
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
