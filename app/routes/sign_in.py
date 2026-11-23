"""Sign-in routes: Emergency Password, OIDC, Reauthentication and the Session's own state."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict

from ..api_models import passthrough
from ..config import settings
from ..services.audit_events import audit_log
from ..i18n import available_locales, catalog as locale_catalog, normalize_locale, translate
from ..database import db
from ..errors import BadRequest, Conflict, Forbidden, NotFound, RateLimited, Unauthenticated
from ..emergency_password import (
    REAUTHENTICATION,
    SIGN_IN,
    Accepted,
    BreakGlassDenied,
    Outcome,
    Rejected,
    Throttled,
    verify_emergency_password,
)
from ..oidc import IdentityRejected, OidcError, begin_login, bind_owner_identity, complete_login
from ..oidc_network import oidc_host_addresses
from ..sessions import (
    SessionTransitionError,
    create_session,
    csrf_token_for,
    mark_reauthenticated,
    resolve_session,
    revoke_session,
    rotate_session,
    set_session_vault,
)
from ..services.vaults import VAULT_ROW_SQL
from .deps import (
    _OPERATIONAL_VAULT,
    _api_message,
    _clear_session_cookie,
    _client_ip,
    _read_session_cookie,
    _request_locale,
    _set_csrf_cookie,
    _set_locale_cookie,
    _set_session_cookie,
    current_user,
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


# The answers of this router (ADR-0056). Every model is closed: the routes
# below build these objects from fixed keys, and no service adds any.

class SessionMessageResponse(BaseModel):
    """What signing in and signing out answer."""
    model_config = ConfigDict(extra="forbid")
    message: str
    message_key: str


class ReauthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str


type AuthMethod = str | None


class MeVault(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    uuid: UUID
    name: str


class MeStoppedVault(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    uuid: UUID
    name: str
    lifecycle: Literal["stopping", "tombstone"]
    root_released_at: datetime | None
    root_released: bool


class MeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    display_name: str
    session_version: int
    csrf_token: str
    auth_method: AuthMethod
    locale: str
    locales: list[str]
    vault: MeVault | None
    stopped_vault: MeStoppedVault | None


class LoginOptionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    oidc_enabled: bool
    oidc_provider_name: str | None


class I18nCatalogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    locale: str
    locales: list[str]
    messages: dict[str, str]


class LocaleUpdateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    locale: str
    message: str
    message_key: str
    messages: dict[str, str]


@router.get("/login")
def login_page():
    return _spa_index_response()


@router.post("/api/login", response_model=passthrough(SessionMessageResponse))
def login(action: LoginRequest, request: Request, response: Response):
    client_ip = _client_ip(request)
    raw_token: str | None = None
    csrf_token: str | None = None

    def start_session(connection: Any, outcome: Outcome) -> None:
        nonlocal raw_token, csrf_token
        if isinstance(outcome, Rejected):
            audit_log(
                "break_glass_failed",
                connection=connection,
                actor="emergency",
                ip=client_ip,
            )
        elif isinstance(outcome, Accepted):
            raw_token = create_session(
                connection,
                auth_method="local",
                ip=client_ip,
                user_agent=request.headers.get("user-agent"),
            )
            csrf_token = csrf_token_for(connection, raw_token)

    # The throttle counters are committed before the outcome comes back, so
    # the errors raised below never roll a recorded failure back.
    outcome = verify_emergency_password(
        action.password,
        client_ip=client_ip,
        throttle=SIGN_IN,
        in_transaction=start_session,
    )
    if isinstance(outcome, BreakGlassDenied):
        with db() as connection:
            audit_log(
                "break_glass_denied",
                connection=connection,
                actor="emergency",
                ip=client_ip,
            )
        raise Forbidden("api.sign_in.emergency_network")
    if isinstance(outcome, Throttled):
        retry_after = outcome.retry_after
        with db() as connection:
            audit_log(
                "auth_backoff_blocked",
                connection=connection,
                actor="emergency",
                flow="break_glass",
                ip=client_ip,
                retry_after=retry_after,
            )
        raise RateLimited("api.sign_in.too_many_attempts", retry_after=retry_after)
    if raw_token is None:
        raise Unauthenticated("api.sign_in.incorrect_emergency_password")
    _set_session_cookie(response, raw_token)
    if csrf_token:
        _set_csrf_cookie(response, csrf_token)
    return {**_api_message(request, "api.signed_in")}


@router.post("/api/logout", response_model=passthrough(SessionMessageResponse))
def logout(request: Request, response: Response):
    token = _read_session_cookie(request)
    if token:
        with db() as connection:
            session = resolve_session(connection, token)
            if session:
                revoke_session(connection, session["id"])
    _clear_session_cookie(response)
    return {**_api_message(request, "api.signed_out")}


@router.post("/api/reauth", response_model=passthrough(ReauthResponse))
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

    def record(connection: Any, outcome: Outcome) -> None:
        if isinstance(outcome, Accepted):
            mark_reauthenticated(connection, request.state.session["id"])
            audit_log(
                "reauth_succeeded",
                connection=connection,
                actor="emergency",
                outcome="success",
                flow="reauth",
                ip=client_ip,
            )
        elif isinstance(outcome, Rejected) or (
            isinstance(outcome, Throttled) and outcome.password_rejected
        ):
            audit_log(
                "reauth_failed",
                connection=connection,
                actor="emergency",
                outcome="failure",
                flow="reauth",
                ip=client_ip,
            )
        if isinstance(outcome, Throttled):
            audit_log(
                "auth_backoff_blocked",
                connection=connection,
                actor="emergency",
                flow="reauth",
                ip=client_ip,
                retry_after=outcome.retry_after,
            )

    # Like signing in, every counter and audit event is committed before an
    # HTTP error is raised. Otherwise a rejected request rolls back the very
    # state intended to throttle it.
    outcome = verify_emergency_password(
        action.password,
        client_ip=client_ip,
        throttle=REAUTHENTICATION,
        in_transaction=record,
    )
    if isinstance(outcome, BreakGlassDenied):
        raise Forbidden("api.sign_in.reauth_not_allowed")
    if isinstance(outcome, Throttled):
        retry_after = outcome.retry_after
        raise RateLimited("api.sign_in.too_many_attempts", retry_after=retry_after)
    if not isinstance(outcome, Accepted):
        raise Unauthenticated("api.sign_in.incorrect_password")
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
        raise NotFound("api.sign_in.oidc_disabled") from error
    raise BadRequest("api.sign_in.oidc_failed", reason=error.reason) from error


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
            raise BadRequest("api.sign_in.oidc_failed", reason=error.reason)
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
                    raise Unauthenticated("api.invalid_session") from error
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
        raise Forbidden("api.sign_in.not_owner_identity")
    redirect = RedirectResponse(
        _safe_return_to(claims.return_to), status_code=303
    )
    _set_session_cookie(redirect, raw_token)
    if csrf_token:
        _set_csrf_cookie(redirect, csrf_token)
    return redirect


def _session_vault(request: Request) -> dict[str, Any] | None:
    """The Vault the interface opens on: the Session's, else the first operational one.

    It addresses no operation (ADR-0056). A Vault chosen here because the
    Session named none, or one no longer operational, is written into the
    Session.
    """
    session = request.state.session
    requested_id = session.get("vault_id")
    with db() as connection:
        vault = None
        if requested_id:
            vault = connection.execute(
                f"{VAULT_ROW_SQL} WHERE v.id=%s AND {_OPERATIONAL_VAULT}",
                (requested_id,),
            ).fetchone()
        if not vault:
            vault = connection.execute(
                f"""
                {VAULT_ROW_SQL} WHERE {_OPERATIONAL_VAULT}
                ORDER BY v.name
                LIMIT 1
                """
            ).fetchone()
        if not vault:
            return None
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
                raise Conflict("api.session_changed")
            session.update(updated)
    return vault


@session_router.get("/api/me", response_model=passthrough(MeResponse))
def me(request: Request, response: Response, user: dict[str, Any] = Depends(current_user)):
    csrf_token = request.state.session["csrf_token"]
    _set_csrf_cookie(response, csrf_token)
    vault = _session_vault(request)
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


@session_router.get("/api/login/options", response_model=passthrough(LoginOptionsResponse))
def login_options():
    """Public sign-in choices for the login page: no secrets, no Session."""
    return {
        "oidc_enabled": settings.oidc_enabled,
        "oidc_provider_name": settings.oidc_provider_name or None,
    }


@session_router.get("/api/i18n/catalog", response_model=passthrough(I18nCatalogResponse))
def i18n_catalog(request: Request, locale: str | None = None):
    resolved = normalize_locale(locale) if locale else _request_locale(request)
    return {
        "locale": resolved,
        "locales": list(available_locales()),
        "messages": locale_catalog(resolved),
    }


@session_router.put("/api/locale", response_model=passthrough(LocaleUpdateResponse))
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
        raise BadRequest("api.unsupported_locale")
    _set_locale_cookie(response, resolved)
    return {
        "locale": resolved,
        "message": translate("api.locale_updated", locale=resolved),
        "message_key": "api.locale_updated",
        "messages": locale_catalog(resolved),
    }
