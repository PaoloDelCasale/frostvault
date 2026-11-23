"""Sign-in routes: Emergency Password, OIDC, Reauthentication and the Session's own state."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ..api_models import JsonObjectResponse, response_model
from ..config import settings
from ..services.audit_events import audit_log
from ..i18n import available_locales, catalog as locale_catalog, normalize_locale, translate
from ..backoff import (
    OWNER_ACCOUNT_KEY,
    BackoffError,
    guard as backoff_guard,
    reauth_account_key,
    reauth_ip_key,
    record_failure,
    record_success,
)
from ..breakglass import is_break_glass_allowed
from ..database import db
from ..oidc import IdentityRejected, OidcError, begin_login, bind_owner_identity, complete_login
from ..oidc_network import oidc_host_addresses
from ..security import DUMMY_PASSWORD_HASH, verify_password
from ..sessions import (
    SessionTransitionError,
    create_session,
    csrf_token_for,
    mark_reauthenticated,
    resolve_session,
    revoke_session,
    rotate_session,
)
from .deps import (
    _api_message,
    _clear_session_cookie,
    _client_ip,
    _read_session_cookie,
    _request_locale,
    _set_csrf_cookie,
    _set_locale_cookie,
    _set_session_cookie,
    current_user,
    current_vault,
)
from .ops import _spa_index_response


router = APIRouter()

# Registered after the SPA pages of ``ops.pages_router``: two routers keep the
# historical registration order, and with it the order of the OpenAPI paths.
session_router = APIRouter()


class LoginRequest(BaseModel):
    # The Owner has no username: the Emergency Password alone signs in (#586).
    password: str


class LocaleUpdate(BaseModel):
    locale: str


class ReauthRequest(BaseModel):
    password: str


@router.get("/login")
def login_page():
    return _spa_index_response()


@router.post("/api/login", response_model=JsonObjectResponse)
def login(action: LoginRequest, request: Request, response: Response):
    client_ip = _client_ip(request)
    if not is_break_glass_allowed(client_ip):
        with db() as connection:
            audit_log(
                "break_glass_denied",
                connection=connection,
                actor="emergency",
                ip=client_ip,
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
            backoff_guard(connection, scope="account", key=OWNER_ACCOUNT_KEY)
        except BackoffError as blocked:
            retry_after = blocked.retry_after
        else:
            owner = connection.execute(
                "SELECT password_hash FROM owner WHERE id=1"
            ).fetchone()
            eligible = bool(owner and owner["password_hash"])
            password_hash = (
                owner["password_hash"] if eligible else DUMMY_PASSWORD_HASH
            )
            password_valid = verify_password(password_hash, action.password)
            # Always reject the dummy path, even if its hash ever matches the
            # submitted password (or verification is replaced in a test).
            if not eligible or not password_valid:
                record_failure(connection, scope="ip", key=client_ip)
                record_failure(connection, scope="account", key=OWNER_ACCOUNT_KEY)
                audit_log(
                    "break_glass_failed",
                    connection=connection,
                    actor="emergency",
                    ip=client_ip,
                )
            else:
                record_success(connection, scope="ip", key=client_ip)
                record_success(connection, scope="account", key=OWNER_ACCOUNT_KEY)
                raw_token = create_session(
                    connection,
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
                actor="emergency",
                flow="break_glass",
                ip=client_ip,
                retry_after=retry_after,
            )
        raise HTTPException(
            429,
            "Too many attempts; try again later",
            headers={"Retry-After": str(retry_after)},
        )
    if raw_token is None:
        raise HTTPException(401, "Incorrect emergency password")
    _set_session_cookie(response, raw_token)
    if csrf_token:
        _set_csrf_cookie(response, csrf_token)
    return {**_api_message(request, "api.signed_in")}


@router.post("/api/logout", response_model=JsonObjectResponse)
def logout(request: Request, response: Response):
    token = _read_session_cookie(request)
    if token:
        with db() as connection:
            session = resolve_session(connection, token)
            if session:
                revoke_session(connection, session["id"])
    _clear_session_cookie(response)
    return {**_api_message(request, "api.signed_out")}


@router.post("/api/reauth", response_model=JsonObjectResponse)
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
        ("account", reauth_account_key()),
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
                "SELECT password_hash FROM owner WHERE id=1"
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
                    actor="emergency",
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
                    actor="emergency",
                    outcome="success",
                    flow="reauth",
                    ip=client_ip,
                )

        if retry_after is not None:
            audit_log(
                "auth_backoff_blocked",
                connection=connection,
                actor="emergency",
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


@router.get("/auth/oidc/reauth")
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


@router.get("/auth/oidc/login")
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


@router.get("/auth/oidc/callback")
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
            bind_owner_identity(connection, claims)
        except IdentityRejected:
            rejected = True
        if not rejected:
            existing = resolve_session(connection, _read_session_cookie(request))
            if existing:
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
                    auth_method="oidc",
                    ip=_client_ip(request),
                    user_agent=request.headers.get("user-agent"),
                )
                csrf_token = csrf_token_for(connection, raw_token)
        else:
            audit_log(
                "oidc_identity_rejected",
                connection=connection,
                actor="system",
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


@session_router.get("/api/me", response_model=response_model("MeResponse"))
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
    stopped_vault_block = None
    if vault is not None:
        vault_block = {
            "id": vault["id"],
            "uuid": str(vault["uuid"]),
            "name": vault["name"],
        }
    else:
        selected_id = request.state.session.get("vault_id")
        with db() as connection:
            stopped_vault = connection.execute(
                """
                SELECT id, uuid, name, lifecycle, root_released_at
                FROM vaults
                WHERE lifecycle IN ('stopping', 'tombstone')
                ORDER BY id IS DISTINCT FROM %s,
                         lifecycle <> 'stopping', root_released_at DESC, lower(name)
                LIMIT 1
                """,
                (selected_id,),
            ).fetchone()
        if stopped_vault:
            stopped_vault_block = {
                **stopped_vault,
                "uuid": str(stopped_vault["uuid"]),
                "root_released": stopped_vault["root_released_at"] is not None,
            }
    return {
        **user,
        "csrf_token": csrf_token,
        "auth_method": request.state.session.get("auth_method"),
        "locale": _request_locale(request),
        "locales": list(available_locales()),
        "vault": vault_block,
        "stopped_vault": stopped_vault_block,
    }


@session_router.get("/api/login/options", response_model=response_model("LoginOptionsResponse"))
def login_options():
    """Public sign-in choices for the login page: no secrets, no Session."""
    return {
        "oidc_enabled": settings.oidc_enabled,
        "oidc_provider_name": settings.oidc_provider_name or None,
    }


@session_router.get("/api/i18n/catalog", response_model=response_model("I18nCatalogResponse"))
def i18n_catalog(request: Request, locale: str | None = None):
    resolved = normalize_locale(locale) if locale else _request_locale(request)
    return {
        "locale": resolved,
        "locales": list(available_locales()),
        "messages": locale_catalog(resolved),
    }


@session_router.put("/api/locale", response_model=response_model("LocaleUpdateResponse"))
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
