from __future__ import annotations

import asyncio
import json
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from jinja2.utils import htmlsafe_json_dumps
from markupsafe import Markup
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .branding import PRODUCT_NAME
from .config import settings, validate_settings
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
    record_failure,
    record_success,
)
from .breakglass import is_break_glass_allowed
from .catalog import ArchiveCatalog
from .database import INTEGRITY_ERRORS, db, initialize_database
from .oidc import OidcError, begin_login, complete_login
from .proxy import parse_networks, resolve_client_ip
from .invites import (
    InviteError,
    create_invite,
    redeem_invite,
    resolve_invite,
)
from .lookup_rate_limit import check_lookup_rate_limit
from .security import hash_password, verify_password
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
from .services import worker_errors as worker_error_store
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
    apply_guided_profile_to_policy,
    clear_folder_override,
    create_policy,
    ensure_default_policy_with_profile,
    list_vault_policies,
    load_policy_assignments,
    set_folder_override,
    set_vault_default_policy,
    sync_lifecycle_rules_for_bucket,
)
from .services.lifecycle_profiles import GUIDED_PROFILES, guided_profile
from .services import cloud_deletion as cloud_deletion_service
from .services.vaults import (
    InvalidVaultName,
    VaultCreationError,
    VaultProvisioningUnavailable,
    VaultSlugTaken,
    create_vault_for_user,
)
from .sessions import (
    create_session,
    csrf_token_for,
    is_reauth_recent,
    mark_reauthenticated,
    resolve_session,
    revoke_session,
    rotate_session,
    set_session_vault,
)
from .services.fs_preflight import (
    check_vault_filesystem,
    resolve_configured_vault_root,
)
from .storage import (
    background_loop,
    cancel_jobs,
    cleanup_abandoned_restore_files,
    filesystem_watch_loop,
    now_iso,
    reconcile_interrupted_jobs,
    runtime_status,
    safe_relative_path,
    scan_vault,
    s3_client,
    storage_class_requires_restore,
)


TEMPLATE_DIR = __import__("pathlib").Path(__file__).parent / "templates"
STATIC_DIR = __import__("pathlib").Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_settings()
    initialize_database()
    cleanup_abandoned_restore_files()
    reconcile_interrupted_jobs()
    tasks = [asyncio.create_task(background_loop())]
    if settings.filesystem_watch_enabled:
        tasks.append(asyncio.create_task(filesystem_watch_loop()))
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title=PRODUCT_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "xml"]),
)
templates.globals["available_locales"] = available_locales
templates.globals["product_name"] = PRODUCT_NAME


def _tojson_filter(value: Any) -> Markup:
    rendered = htmlsafe_json_dumps(value, dumps=json.dumps, ensure_ascii=False)
    return Markup(
        str(rendered).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    )


templates.filters["tojson"] = _tojson_filter


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


def _render(template_name: str, request: Request, **context: Any) -> str:
    locale = context.pop("locale", None) or _request_locale(request)

    def t(key: str, **params: Any) -> str:
        return translate(key, locale=locale, **params)

    return templates.get_template(template_name).render(
        locale=locale,
        t=t,
        catalog=locale_catalog(locale),
        **context,
    )


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


def admin_user(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if not user["is_admin"]:
        raise HTTPException(403, "Administrator access required")
    return user


class ReauthRequired(HTTPException):
    """Signals the frontend that a fresh Reauthentication is needed."""

    def __init__(self) -> None:
        super().__init__(status_code=403, detail="reauth_required")


@app.exception_handler(ReauthRequired)
async def _reauth_required_handler(_: Request, __: ReauthRequired) -> JSONResponse:
    # Stable marker the frontend keys on to trigger a step-up.
    return JSONResponse({"error": "reauth_required"}, status_code=403)


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
    if not is_reauth_recent(
        session.get("reauth_at"),
        now=datetime.now(timezone.utc),
        window_seconds=settings.reauth_window_seconds,
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
}


def _governance_http_error(exc: GovernanceError) -> HTTPException:
    status_code, message = _GOVERNANCE_ERROR_STATUS.get(
        exc.reason, (400, "Request could not be completed")
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
                ORDER BY v.name
                LIMIT 1
                """,
                (user["id"],),
            ).fetchone()
        if not vault:
            raise HTTPException(403, "No vault is assigned to this user")
        if session.get("vault_id") != vault["id"]:
            set_session_vault(connection, session["id"], vault["id"])
            session["vault_id"] = vault["id"]
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
    """Self-service vault creation payload (issues #7 and #6).

    Only a label (``name``), an optional ``slug``, and ``encryption_mode``
    are accepted. The server alone derives the storage namespace and crypt
    secrets, so any caller-supplied storage field (e.g. a source root, S3
    bucket/prefix, rclone remote, or password) is rejected outright rather
    than silently ignored -- a client must never believe it controls where
    its vault lives or how it is encrypted.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    slug: str | None = Field(default=None, min_length=2, max_length=60)
    encryption_mode: str = Field(default="plain", pattern="^(plain|crypt)$")


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
    new_path: str = Field(min_length=1, max_length=1024)


class ConfirmFolderRenameAction(BaseModel):
    old_prefix: str = Field(min_length=1, max_length=1024)
    new_prefix: str = Field(min_length=1, max_length=1024)


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


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    display_name: str = Field(min_length=1, max_length=120)
    password: str | None = Field(default=None, min_length=12, max_length=200)
    is_admin: bool = False


class InviteCreate(BaseModel):
    target_user_id: int


class UserUpdate(BaseModel):
    active: bool | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    password: str | None = Field(default=None, min_length=12, max_length=200)


class VaultCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=2, max_length=60)
    owner_user_id: int
    reason: str = Field(min_length=3, max_length=500)
    encryption_mode: str = Field(default="plain", pattern="^(plain|crypt)$")


class MembershipCreate(BaseModel):
    user_id: int
    role: str = "viewer"


class UserLookup(BaseModel):
    username: str = Field(min_length=2, max_length=80)


class AdminMembershipCreate(MembershipCreate):
    # Required for every global-admin override of a vault's sharing, per
    # ADR-0005's reauth-then-audit precedent for sensitive actions.
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


class LifecycleDefaultUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guided_profile: str = Field(min_length=1, max_length=80)
    name: str = Field(default="Vault default", min_length=1, max_length=120)


class LifecycleFolderOverrideUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder_path: str = Field(min_length=1, max_length=500)
    guided_profile: str = Field(min_length=1, max_length=80)
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


def normalize_directory(value: str) -> str:
    """Return a safe, normalized catalog directory (the root is an empty string)."""
    if not value:
        return ""
    try:
        return safe_relative_path(value).as_posix().rstrip("/")
    except ValueError as exc:
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
                "upload": row.get("upload_eligible", row["state"] == "local_only"),
                "recover": row.get("recover_eligible", row["state"] == "cloud_only"),
                "free-space": row.get("cleanup_eligible", row["state"] == "both"),
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


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    token = _read_session_cookie(request)
    if token:
        with db() as connection:
            if resolve_session(connection, token):
                return RedirectResponse("/", status_code=303)
    local_login = is_break_glass_allowed(_client_ip(request))
    return _render("login.html", request, local_login=local_login)


@app.post("/api/login")
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
                "SELECT * FROM users WHERE lower(username)=lower(%s) AND active=TRUE",
                (username,),
            ).fetchone()
            # Break-glass Login is admin-only and never accepts a null password hash.
            if (
                not user
                or not user["password_hash"]
                or not user["is_admin"]
                or not verify_password(user["password_hash"], action.password)
            ):
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


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = _read_session_cookie(request)
    if token:
        with db() as connection:
            session = resolve_session(connection, token)
            if session:
                revoke_session(connection, session["id"])
    _clear_session_cookie(response)
    return {**_api_message(request, "api.signed_out")}


@app.post("/api/reauth")
def reauth(
    action: ReauthRequest,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
):
    """Break-glass Reauthentication: re-enter the local password.

    OIDC users have no password hash and must step up through the provider
    (see ``/auth/oidc/reauth``).
    """
    client_ip = _client_ip(request)
    if not is_break_glass_allowed(client_ip):
        raise HTTPException(403, "Password reauthentication is not allowed here")
    with db() as connection:
        row = connection.execute(
            "SELECT password_hash FROM users WHERE id=%s", (user["id"],)
        ).fetchone()
        if (
            not row
            or not row["password_hash"]
            or not verify_password(row["password_hash"], action.password)
        ):
            raise HTTPException(401, "Incorrect password")
        mark_reauthenticated(connection, request.state.session["id"])
    return {"message": "Reauthenticated"}


@app.get("/auth/oidc/reauth")
def oidc_reauth(
    request: Request,
    return_to: str | None = Query(default=None),
    user: dict[str, Any] = Depends(current_user),
):
    """OIDC step-up: force a fresh provider login with ``prompt=login``."""
    if not settings.oidc_enabled:
        raise HTTPException(404, "OIDC login is not enabled")
    redirect_uri = str(request.url_for("oidc_callback"))
    with db() as connection:
        authorization_url = begin_login(
            connection,
            redirect_uri=redirect_uri,
            return_to=_safe_return_to(return_to),
            prompt="login",
            http_client=_oidc_client(),
        )
    return RedirectResponse(authorization_url, status_code=303)


def _oidc_client():
    # Seam for tests to inject a fake provider transport; production uses the
    # OIDC module's own HTTP client.
    return None


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
    if not settings.oidc_enabled:
        raise HTTPException(404, "OIDC login is not enabled")
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
                authorization_url = begin_login(
                    connection,
                    redirect_uri=redirect_uri,
                    return_to=_safe_return_to(return_to),
                    invite_id=invite_id,
                    http_client=_oidc_client(),
                )
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
    if not settings.oidc_enabled:
        raise HTTPException(404, "OIDC login is not enabled")
    redirect_uri = str(request.url_for("oidc_callback"))
    with db() as connection:
        token = create_invite(
            connection, target_user_id=user["id"], created_by=user["id"]
        )
        invite_id = _resolve_invite_id(connection, token)
        authorization_url = begin_login(
            connection,
            redirect_uri=redirect_uri,
            return_to="/",
            invite_id=invite_id,
            prompt="login",
            http_client=_oidc_client(),
        )
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
    if not settings.oidc_enabled:
        raise HTTPException(404, "OIDC login is not enabled")
    redirect_uri = str(request.url_for("oidc_callback"))
    with db() as connection:
        try:
            claims = complete_login(
                connection,
                state=state,
                code=code,
                redirect_uri=redirect_uri,
                http_client=_oidc_client(),
            )
        except OidcError as error:
            raise HTTPException(400, f"OIDC login failed: {error.reason}")
        user_id = _resolve_or_bind_identity(connection, claims)
        existing = resolve_session(connection, _read_session_cookie(request))
        if existing and existing["user"]["id"] == user_id:
            # Step-up reauthentication: the user proved their identity again for
            # the same account, so refresh the reauth window and rotate the token
            # instead of minting a brand-new Session.
            mark_reauthenticated(connection, existing["id"])
            raw_token = rotate_session(connection, existing["id"])
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


@app.get("/vaults/new", response_class=HTMLResponse)
def vault_create_page(request: Request, response: Response):
    try:
        user = current_user(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=303)
        raise
    _set_csrf_cookie(response, request.state.session["csrf_token"])
    return templates.get_template("vault_create.html").render(user=user)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    try:
        user = current_user(request)
        vault = current_vault(request, user)
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=303)
        return _render("no_vault.html", request)
    return _render(
        "index.html",
        request,
        user=user,
        vault=vault,
        can_operate=can_operate(vault["role"]),
        delete_enabled=settings.allow_local_delete and is_owner(vault["role"]),
        cloud_deletion_enabled=bool(vault.get("cloud_deletion_enabled"))
        and is_owner(vault["role"]),
        is_vault_owner=is_owner(vault["role"]),
    )


@app.get("/vault/access", response_class=HTMLResponse)
def vault_access_page(
    request: Request,
    response: Response,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(owner_vault),
):
    _set_csrf_cookie(response, request.state.session["csrf_token"])
    return templates.get_template("vault_access.html").render(user=user, vault=vault)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    try:
        user = current_user(request)
    except HTTPException:
        return RedirectResponse("/login", status_code=303)
    if not user["is_admin"]:
        return RedirectResponse("/", status_code=303)
    return templates.get_template("admin.html").render(user=user)


@app.get("/api/me")
def me(request: Request, response: Response, user: dict[str, Any] = Depends(current_user)):
    csrf_token = request.state.session["csrf_token"]
    _set_csrf_cookie(response, csrf_token)
    return {
        **user,
        "csrf_token": csrf_token,
        "auth_method": request.state.session.get("auth_method"),
        "locale": _request_locale(request),
        "locales": list(available_locales()),
    }


@app.get("/api/i18n/catalog")
def i18n_catalog(request: Request, locale: str | None = None):
    resolved = normalize_locale(locale) if locale else _request_locale(request)
    return {
        "locale": resolved,
        "locales": list(available_locales()),
        "messages": locale_catalog(resolved),
    }


@app.put("/api/locale")
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


@app.get("/api/vaults")
def user_vaults(user: dict[str, Any] = Depends(current_user)):
    with db() as connection:
        rows = connection.execute(
            """
            SELECT v.id, v.slug, v.name, vm.role
            FROM vaults v
            JOIN vault_members vm ON vm.vault_id=v.id
            WHERE vm.user_id=%s AND v.enabled=TRUE
            ORDER BY v.name
            """,
            (user["id"],),
        ).fetchall()
    return {"items": rows}


@app.post("/api/vaults", status_code=201)
def create_own_vault(
    action: VaultSelfServiceCreate,
    user: dict[str, Any] = Depends(current_user),
):
    """Let an authenticated, already-existing user create their own vault.

    The server generates the storage identity; it never provisions a user
    from identity claims (the caller must already be `current_user`).
    Crypt vaults also receive a one-time recovery export so the owner can
    confirm custody before uploads are admitted.
    """
    try:
        vault = create_vault_for_user(
            user["id"],
            action.name,
            action.slug,
            encryption_mode=action.encryption_mode,
        )
    except VaultSlugTaken as exc:
        raise HTTPException(409, str(exc)) from exc
    except VaultProvisioningUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except InvalidVaultName as exc:
        raise HTTPException(422, str(exc)) from exc
    except VaultCreationError as exc:
        raise HTTPException(409, str(exc)) from exc
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
    }
    if vault["encryption_mode"] == "crypt":
        payload["recovery_export"] = build_recovery_export(vault)
    return payload


@app.post("/api/vault/recovery/confirm")
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


@app.post("/api/vault/recovery/export")
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


@app.post("/api/vaults/select")
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
            """,
            (action.vault_id, user["id"]),
        ).fetchone()
    if not allowed:
        raise HTTPException(403, "Vault access denied")
    with db() as connection:
        set_session_vault(connection, request.state.session["id"], action.vault_id)
    return {**_api_message(request, "api.vault_selected")}


@app.get("/api/files")
def list_files(
    q: str = "",
    state: str = "",
    directory: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=10, le=500),
    vault: dict[str, Any] = Depends(current_vault),
):
    directory = normalize_directory(directory)
    with db() as connection:
        rows = ArchiveCatalog(connection).list_file_rows(
            vault["id"],
            search=q,
            path_prefix=directory if not q else "",
        )
        if q:
            if state:
                rows = [row for row in rows if row["state"] == state]
            total = len(rows)
            offset = (page - 1) * page_size
            rows = rows[offset:offset + page_size]
            items = [{**row, "type": "file", "name": row["path"]} for row in rows]
        else:
            entries = build_directory_items(rows, directory, state)
            total = len(entries)
            offset = (page - 1) * page_size
            items = entries[offset:offset + page_size]
    return {
        "items": items,
        "total": total,
        "page": page,
        "directory": directory,
        "mode": "search" if q else "browse",
    }


@app.get("/api/file-history")
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
        path_history = catalog.list_path_history(observed["id"])
    return {
        "vault_file_id": observed["id"],
        "path": logical_path,
        "path_history": path_history,
        "versions": versions,
    }


@app.get("/api/rename-candidates")
def rename_candidates(vault: dict[str, Any] = Depends(current_vault)):
    with db() as connection:
        candidates = ArchiveCatalog(connection).list_rename_candidates(vault["id"])
    return {"items": candidates}


@app.post("/api/confirm-rename", status_code=202)
def confirm_rename(
    action: ConfirmRenameAction,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
):
    if not can_operate(vault["role"]):
        raise HTTPException(403, "Vault is read-only")
    new_path = safe_relative_path(action.new_path).as_posix()
    with db() as connection:
        catalog = ArchiveCatalog(connection)
        catalog.confirm_file_rename(
            vault_file_id=action.vault_file_id,
            new_path=new_path,
            changed_at=now_iso(),
        )
        audit_log(
            "vault_file_renamed",
            connection=connection,
            vault_id=vault["id"],
            vault_file_id=action.vault_file_id,
            new_path=new_path,
            decision="confirmed",
            actor_id=user["id"],
        )
    try:
        queued = queue_jobs(new_path, "rename", vault["id"], user["id"])
    except HTTPException as exc:
        if exc.status_code == 409:
            return {
                "vault_file_id": action.vault_file_id,
                "path": new_path,
                "message": "Rename confirmed; no cloud migration required",
            }
        raise
    return {
        **queued,
        "vault_file_id": action.vault_file_id,
        "path": new_path,
        "message": "Rename confirmed",
    }


@app.post("/api/confirm-folder-rename", status_code=202)
def confirm_folder_rename(
    action: ConfirmFolderRenameAction,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
):
    if not can_operate(vault["role"]):
        raise HTTPException(403, "Vault is read-only")
    old_prefix = safe_relative_path(action.old_prefix).as_posix()
    new_prefix = safe_relative_path(action.new_prefix).as_posix()
    with db() as connection:
        catalog = ArchiveCatalog(connection)
        renamed_ids = catalog.confirm_folder_rename(
            vault_id=vault["id"],
            old_prefix=old_prefix,
            new_prefix=new_prefix,
            changed_at=now_iso(),
        )
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


@app.get("/api/stats")
def stats(vault: dict[str, Any] = Depends(current_vault)):
    with db() as connection:
        summary = ArchiveCatalog(connection).summary(vault["id"])
    source_root = vault.get("source_root") or ""
    allowed_bases = [settings.vault_sources_root]
    bootstrap_root = (settings.bootstrap_vault_source_root or "").strip()
    if bootstrap_root:
        allowed_bases.append(bootstrap_root)
    safe_root = resolve_configured_vault_root(
        source_root, allowed_bases=allowed_bases
    )
    if safe_root is None:
        # Report missing under the configured sources root; never walk raw input.
        filesystem = check_vault_filesystem(
            f"{settings.vault_sources_root.rstrip('/')}/.missing-vault-root",
            allowed_bases=allowed_bases,
        )
    else:
        filesystem = check_vault_filesystem(safe_root, allowed_bases=allowed_bases)
    filesystem_payload = {
        "ok": filesystem.ok,
        "uid": filesystem.uid,
        "gid": filesystem.gid,
        "root": filesystem.root,
        "checks": [
            {
                "code": check.code,
                "status": check.status,
                "message": check.message,
                "remediation": check.remediation,
            }
            for check in filesystem.checks
        ],
        "findings": [
            {
                "path": finding.path,
                "code": finding.code,
                "message": finding.message,
            }
            for finding in filesystem.findings
        ],
    }
    runtime = dict(runtime_status.get(vault["id"], {}))
    # Merge any scan-time findings that are not already in the live preflight.
    scan_findings = (runtime.get("filesystem") or {}).get("findings") or []
    if scan_findings:
        known = {(item["path"], item["code"]) for item in filesystem_payload["findings"]}
        for item in scan_findings:
            key = (item.get("path"), item.get("code"))
            if key not in known:
                filesystem_payload["findings"].append(item)
                filesystem_payload["ok"] = False
    return {
        **summary,
        "runtime": runtime,
        "filesystem": filesystem_payload,
        "delete_enabled": settings.allow_local_delete and is_owner(vault["role"]),
    }


@app.get("/api/audit-events")
def vault_audit_events(vault: dict[str, Any] = Depends(current_vault)):
    """List audit events visible to members of the current vault."""
    with db() as connection:
        events = audit_event_store.list_vault_audit_events(connection, vault["id"])
    return {"events": events}


@app.get("/api/admin/audit-events")
def admin_audit_events(_: dict[str, Any] = Depends(admin_user)):
    """List all audit events for global administrators."""
    with db() as connection:
        events = audit_event_store.list_admin_audit_events(connection)
    return {"events": events}


@app.get("/api/notifications")
def list_notifications(user: dict[str, Any] = Depends(current_user)):
    """List in-app notifications for the authenticated user."""
    with db() as connection:
        items = notification_service.list_in_app_notifications(
            connection, user_id=user["id"]
        )
    return {"items": items}


class NotificationReadAction(BaseModel):
    notification_id: int


@app.post("/api/notifications/read")
def mark_notification_read(
    action: NotificationReadAction,
    user: dict[str, Any] = Depends(current_user),
):
    with db() as connection:
        item = notification_service.mark_notification_read(
            connection,
            notification_id=action.notification_id,
            user_id=user["id"],
        )
    if item is None:
        raise HTTPException(404, "Notification not found")
    return item


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


@app.post("/api/admin/notification-endpoints/webhook")
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


@app.post("/api/admin/notification-endpoints/smtp")
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


@app.post("/api/vault/notification-preferences")
def set_vault_notification_preference(
    action: VaultNotificationPreferenceAction,
    vault: dict[str, Any] = Depends(owner_vault),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    with db() as connection:
        pref = notification_service.set_vault_notification_preference(
            connection,
            vault_id=vault["id"],
            event=action.event,
            channel=action.channel,
            enabled=action.enabled,
            recipient_user_ids=action.recipient_user_ids,
        )
    return pref


@app.get("/api/admin/worker-errors")
def admin_worker_errors(_: dict[str, Any] = Depends(admin_user)):
    with db() as connection:
        items = worker_error_store.list_worker_errors(connection)
    return {"items": items}


class MetadataBackupRunAction(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@app.get("/api/admin/metadata-backups")
def admin_list_metadata_backups(_: dict[str, Any] = Depends(admin_user)):
    with db() as connection:
        status = metadata_backup_service.backup_status(connection)
        runs = metadata_backup_service.list_backup_artifacts(connection)
    return {"status": status, "runs": runs}


@app.post("/api/admin/metadata-backups/run")
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
                retention=settings.metadata_backup_retention,
                s3_prefix=settings.metadata_backup_s3_prefix,
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


@app.get("/api/admin/metadata-backups/download/{run_id}")
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


@app.get("/api/jobs")
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


@app.post("/api/scan", status_code=202)
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
) -> dict[str, Any]:
    logical_path = safe_relative_path(path).as_posix()
    if action not in {"upload", "recover", "free-space", "rename"}:
        raise HTTPException(422, "Invalid operation")
    if archive_version_id and (action != "recover" or is_directory):
        raise HTTPException(
            422, "archive_version_id is only valid for single-file recovery"
        )
    group_id = uuid.uuid4().hex
    estimated_cost_eur = None
    estimated_hours = None
    resolved_tier = None
    resolved_days = None
    with db() as connection:
        catalog = ArchiveCatalog(connection)
        if action == "recover" and not is_directory:
            versions = catalog.list_versions(vault_id, logical_path)
            recoverable = [row for row in versions if row["recoverable"]]
            if archive_version_id:
                selected = next(
                    (
                        row
                        for row in versions
                        if row["id"] == archive_version_id
                    ),
                    None,
                )
                if selected is None or not selected["recoverable"]:
                    raise HTTPException(
                        409, "The selected Archive Version is not recoverable"
                    )
            elif len(recoverable) == 1:
                archive_version_id = recoverable[0]["id"]
                selected = recoverable[0]
            elif recoverable:
                selected = recoverable[0]
                archive_version_id = selected["id"]
            else:
                selected = None
            if selected is not None and storage_class_requires_restore(
                selected.get("storage_class")
            ):
                resolved_days = int(
                    restore_days
                    if restore_days is not None
                    else settings.restore_days
                )
                resolved_tier = normalize_restore_tier(
                    restore_tier or settings.restore_tier,
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
        )
        if not eligible_count:
            raise HTTPException(409, "No files are eligible for this operation")
        quota = catalog.last_quota_evaluation.as_dict()
    if not job_ids:
        raise HTTPException(409, "An operation is already running on the selected files")
    return {
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


@app.get("/api/files/versions")
def file_versions(
    path: str = Query(min_length=1),
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
        "default_restore_tier": settings.restore_tier,
        "default_restore_days": settings.restore_days,
    }


@app.post("/api/recover/estimate")
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
        else settings.restore_days
    )
    tier = normalize_restore_tier(
        action.restore_tier or settings.restore_tier,
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
            size_threshold_gib=settings.restore_high_impact_gib,
            cost_threshold_eur=settings.restore_high_impact_eur,
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


@app.post("/api/upload", status_code=202)
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


@app.post("/api/jobs/cancel")
def cancel_job(
    action: JobCancelAction,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
):
    return cancel_job_group(action.group_id, action.action, vault)


@app.post("/api/upload/cancel")
def cancel_upload(
    action: GroupCancelAction,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
):
    """Backward-compatible endpoint for clients using the old upload route."""
    return cancel_job_group(action.group_id, "upload", vault)


@app.post("/api/recover", status_code=202)
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


@app.post("/api/recover/approve", status_code=202)
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


@app.post("/api/free-space", status_code=202)
def free_space(
    action: FileAction,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    if not is_owner(vault["role"]):
        raise HTTPException(403, "Vault is read-only")
    if not settings.allow_local_delete:
        raise HTTPException(403, "Freeing local space is disabled")
    queued = queue_jobs(
        action.path, "free-space", vault["id"], user["id"], action.is_directory
    )
    return {**queued, **_api_message(request, "api.free_space_started")}


def _cloud_deletion_paths(action: CloudDeletionPreviewRequest | CloudArchiveRequest | CloudPurgeRequest) -> list[str]:
    if action.paths:
        return list(action.paths)
    return [action.path]


@app.get("/api/vault/cloud-deletion")
def get_cloud_deletion_setting(vault: dict[str, Any] = Depends(current_vault)):
    with db() as connection:
        enabled = cloud_deletion_service.is_cloud_deletion_enabled(
            connection, vault["id"]
        )
    return {
        "enabled": enabled,
        "purge_delay_seconds": settings.cloud_purge_delay_seconds,
        "delete_marker_explanation": cloud_deletion_service.delete_marker_explanation(),
        "generated_phrase": cloud_deletion_service.generate_confirmation_phrase(),
        "accepted_single_identity_risk": (
            "This installation may use one IAM identity for ordinary archive "
            "operations and permanent DeleteObjectVersion calls. Restrict that "
            "identity, audit every purge, and prefer a dedicated deletion role "
            "when your threat model requires stricter separation."
        ),
    }


@app.put("/api/vault/cloud-deletion")
def update_cloud_deletion_setting(
    action: CloudDeletionSettingUpdate,
    user: dict[str, Any] = Depends(current_user),
    vault: dict[str, Any] = Depends(current_vault),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    if not is_owner(vault["role"]):
        raise HTTPException(403, "Only the primary owner can change cloud deletion")
    with db() as connection:
        enabled = cloud_deletion_service.set_cloud_deletion_enabled(
            connection,
            vault_id=vault["id"],
            enabled=action.enabled,
            actor_user_id=user["id"],
        )
    return {"enabled": enabled}


@app.post("/api/cloud-deletion/preview")
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


@app.post("/api/cloud-archive", status_code=202)
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


@app.post("/api/cloud-purge", status_code=202)
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
                delay_seconds=settings.cloud_purge_delay_seconds,
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


@app.get("/api/admin/users")
def admin_users(_: dict[str, Any] = Depends(admin_user)):
    with db() as connection:
        rows = connection.execute(
            """
            SELECT u.id, u.username, u.display_name, u.is_admin, u.active, u.created_at,
                   COUNT(vm.vault_id) AS vault_count
            FROM users u LEFT JOIN vault_members vm ON vm.user_id=u.id
            GROUP BY u.id ORDER BY lower(u.username)
            """
        ).fetchall()
    return {"items": rows}


@app.post("/api/admin/users", status_code=201)
def create_user(
    action: UserCreate,
    _: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    username = action.username.strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]+", username):
        raise HTTPException(422, "The username can contain letters, numbers, periods, hyphens, and underscores")
    password_hash = hash_password(action.password) if action.password else None
    try:
        with db() as connection:
            row = connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, is_admin)
                VALUES (%s, %s, %s, %s)
                RETURNING id, username, display_name, is_admin, active
                """,
                (username, action.display_name.strip(), password_hash, action.is_admin),
            ).fetchone()
    except INTEGRITY_ERRORS:
        raise HTTPException(409, "Username is already in use")
    return row


@app.post("/api/admin/invites", status_code=201)
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


@app.patch("/api/admin/users/{user_id}")
def update_user(
    user_id: int,
    action: UserUpdate,
    admin: dict[str, Any] = Depends(admin_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    if action.active is False and user_id == admin["id"]:
        raise HTTPException(400, "You cannot deactivate your own account")
    updates: list[str] = []
    params: list[Any] = []
    if action.active is not None:
        updates.append("active=%s")
        params.append(action.active)
        updates.append("session_version=session_version+1")
    if action.display_name is not None:
        updates.append("display_name=%s")
        params.append(action.display_name.strip())
    if action.password is not None:
        updates.append("password_hash=%s")
        params.append(hash_password(action.password))
        updates.append("session_version=session_version+1")
    if not updates:
        raise HTTPException(400, "No changes requested")
    params.append(user_id)
    with db() as connection:
        if action.active is False:
            # Serialize the last-admin check with the UPDATE (REQ-032).
            # SQLite takes an immediate write lock; PostgreSQL locks every
            # active-admin row (FOR UPDATE cannot wrap COUNT(*) aggregates).
            backend = getattr(connection, "backend", settings.db_backend)
            if backend == "sqlite":
                connection.begin_immediate()
                target = connection.execute(
                    "SELECT is_admin FROM users WHERE id=%s",
                    (user_id,),
                ).fetchone()
                active_admins = connection.execute(
                    "SELECT COUNT(*) AS total FROM users "
                    "WHERE is_admin=TRUE AND active=TRUE"
                ).fetchone()["total"]
            else:
                target = connection.execute(
                    "SELECT is_admin FROM users WHERE id=%s FOR UPDATE",
                    (user_id,),
                ).fetchone()
                active_admin_rows = connection.execute(
                    "SELECT id FROM users "
                    "WHERE is_admin=TRUE AND active=TRUE FOR UPDATE"
                ).fetchall()
                active_admins = len(active_admin_rows)
            if target and target["is_admin"] and active_admins <= 1:
                raise HTTPException(
                    400, "At least one administrator must remain active"
                )
        where_extra = ""
        if action.active is False:
            # Conditional UPDATE refuses concurrent last-admin races (REQ-032).
            where_extra = (
                " AND (is_admin=FALSE OR "
                "(SELECT COUNT(*) FROM users WHERE is_admin=TRUE AND active=TRUE) > 1)"
            )
        row = connection.execute(
            f"UPDATE users SET {', '.join(updates)} WHERE id=%s{where_extra} "
            "RETURNING id, username, display_name, is_admin, active",
            params,
        ).fetchone()
        if action.active is False and not row:
            exists = connection.execute(
                "SELECT id, is_admin FROM users WHERE id=%s", (user_id,)
            ).fetchone()
            if exists and exists["is_admin"]:
                raise HTTPException(
                    400, "At least one administrator must remain active"
                )
    if not row:
        raise HTTPException(404, "User not found")
    return row


@app.get("/api/admin/vaults")
def admin_vaults(_: dict[str, Any] = Depends(admin_user)):
    with db() as connection:
        rows = connection.execute(
            """
            SELECT v.*, COUNT(vm.user_id) AS member_count
            FROM vaults v LEFT JOIN vault_members vm ON vm.vault_id=v.id
            GROUP BY v.id ORDER BY lower(v.name)
            """
        ).fetchall()
    return {"items": rows}


@app.post("/api/admin/vaults", status_code=201)
def create_vault(
    action: VaultCreate,
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
        vault = create_vault_for_user(
            action.owner_user_id,
            action.name,
            action.slug,
            encryption_mode=action.encryption_mode,
        )
    except VaultSlugTaken as exc:
        raise HTTPException(409, str(exc)) from exc
    except VaultProvisioningUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except InvalidVaultName as exc:
        raise HTTPException(422, str(exc)) from exc
    except VaultCreationError as exc:
        raise HTTPException(409, str(exc)) from exc

    notify_owner_of_admin_action(
        "vault_created",
        vault_id=vault["id"],
        owner_user_id=action.owner_user_id,
        actor_id=admin["id"],
        reason=action.reason,
    )
    return vault


@app.post("/api/admin/vaults/{vault_id}/recovery/export")
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


@app.get("/api/admin/vaults/{vault_id}/quotas")
def admin_vault_quotas(
    vault_id: int, _: dict[str, Any] = Depends(admin_user)
):
    with db() as connection:
        return _quota_payload(connection, vault_id)


@app.put("/api/admin/vaults/{vault_id}/quotas")
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


@app.get("/api/vault/quotas")
def own_vault_quotas(vault: dict[str, Any] = Depends(owner_vault)):
    """Primary-owner read seam; ordinary operators and viewers are denied."""
    with db() as connection:
        return _quota_payload(connection, vault["id"])


@app.get("/api/vault/operation-policy")
def own_vault_operation_policy(vault: dict[str, Any] = Depends(owner_vault)):
    with db() as connection:
        return get_policy(connection, vault["id"]).as_dict()


@app.put("/api/vault/operation-policy")
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


@app.post("/api/vault/operation-policy/preview-globs")
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


@app.get("/api/admin/cost-price-books/active")
def admin_active_cost_price_book(_: dict[str, Any] = Depends(admin_user)):
    with db() as connection:
        return get_active_price_book(connection).as_dict()


@app.get("/api/admin/cost-price-books")
def admin_list_cost_price_books(_: dict[str, Any] = Depends(admin_user)):
    with db() as connection:
        return {"items": [book.as_dict() for book in list_price_books(connection)]}


@app.post("/api/admin/cost-price-books", status_code=201)
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


@app.post("/api/admin/cost-price-books/{price_book_id}/activate")
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


@app.post("/api/admin/cost-estimates/storage")
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
            }
            for name, profile in GUIDED_PROFILES.items()
        },
    }


@app.get("/api/vault/lifecycle")
def own_vault_lifecycle(vault: dict[str, Any] = Depends(owner_vault)):
    with db() as connection:
        return _lifecycle_payload(connection, vault)


@app.put("/api/vault/lifecycle/default")
def update_vault_lifecycle_default(
    action: LifecycleDefaultUpdate,
    vault: dict[str, Any] = Depends(owner_vault),
    user: dict[str, Any] = Depends(current_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    try:
        guided_profile(action.guided_profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with db() as connection:
        policy_id, validation = ensure_default_policy_with_profile(
            connection,
            vault_id=vault["id"],
            name=action.name,
            guided_profile=action.guided_profile,
        )
        if not validation.ok:
            raise HTTPException(status_code=400, detail="; ".join(validation.errors))
        try:
            sync_lifecycle_rules_for_bucket(
                connection,
                s3_client(),
                bucket=vault["s3_bucket"],
            )
        except Exception:
            # Tag/rule sync continues on the next vault scan if AWS is unavailable.
            pass
        payload = _lifecycle_payload(connection, vault)
        audit_log(
            "vault_lifecycle_default_updated",
            connection=connection,
            vault_id=vault["id"],
            actor_user_id=user["id"],
            policy_id=policy_id,
            guided_profile=action.guided_profile,
        )
    return {
        "message": "Vault default lifecycle profile updated",
        "warnings": list(validation.warnings),
        **payload,
    }


@app.put("/api/vault/lifecycle/folder-overrides")
def upsert_vault_lifecycle_folder_override(
    action: LifecycleFolderOverrideUpdate,
    vault: dict[str, Any] = Depends(owner_vault),
    user: dict[str, Any] = Depends(current_user),
    _reauth: dict[str, Any] = Depends(require_recent_reauth),
):
    try:
        guided_profile(action.guided_profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        validation = apply_guided_profile_to_policy(
            connection,
            policy_id=policy_id,
            guided_profile=action.guided_profile,
        )
        if not validation.ok:
            raise HTTPException(status_code=400, detail="; ".join(validation.errors))
        set_folder_override(
            connection,
            vault_id=vault["id"],
            folder_path=action.folder_path,
            policy_id=policy_id,
        )
        payload = _lifecycle_payload(connection, vault)
        audit_log(
            "vault_lifecycle_folder_override_updated",
            connection=connection,
            vault_id=vault["id"],
            actor_user_id=user["id"],
            folder_path=action.folder_path,
            policy_id=policy_id,
            guided_profile=action.guided_profile,
        )
    return {
        "message": "Folder lifecycle override updated",
        "warnings": list(validation.warnings),
        **payload,
    }


@app.delete("/api/vault/lifecycle/folder-overrides")
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


@app.get("/api/admin/vaults/{vault_id}/members")
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


@app.post("/api/admin/vaults/{vault_id}/members", status_code=201)
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


@app.delete("/api/admin/vaults/{vault_id}/members/{user_id}")
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


@app.post("/api/admin/vaults/{vault_id}/transfer-owner")
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


@app.get("/api/vault/members")
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


@app.post("/api/vault/user-lookup")
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


@app.post("/api/vault/members", status_code=201)
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


@app.delete("/api/vault/members/{user_id}")
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


@app.post("/api/vault/transfer-owner")
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


@app.get("/health")
def health():
    """Process liveness probe — does not inspect dependencies."""
    return {"status": "ok"}


@app.get("/ready")
def ready():
    """Dependency-aware readiness: database, worker heartbeat, and config."""
    report = health_service.readiness_report()
    status_code = 200 if report["status"] == "ready" else 503
    return JSONResponse(report, status_code=status_code)


@app.get("/metrics")
def metrics():
    """Prometheus text exposition (low-cardinality labels only)."""
    return Response(
        content=metrics_service.render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
