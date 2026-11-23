"""What every router shares: the Session and Vault dependencies, cookies, locale and API errors."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, Query, Request, Response

from ..config import settings
from ..errors import Conflict, Forbidden, Invalid, NotFound, ReauthRequired, Unauthenticated
from ..i18n import DEFAULT_LOCALE, LOCALE_COOKIE_NAME, normalize_locale, resolve_locale, translate
from ..database import db
from ..proxy import parse_networks, resolve_client_ip
from ..services.vaults import VAULT_ROW_SQL
from ..sessions import is_reauth_recent, resolve_session, session_actor, set_session_vault
from ..services.vault_paths import InvalidLogicalPath, safe_relative_path


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
                    "$ref": "#/components/schemas/ApiErrorResponse"
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


def _trusted_proxies() -> list[Any]:
    # ``validate_settings`` refuses an invalid list before the first request.
    return parse_networks(settings.trusted_proxies)


def _client_ip(request: Request) -> str | None:
    peer = request.client.host if request.client else None
    return resolve_client_ip(
        peer=peer,
        forwarded_for=request.headers.get("x-forwarded-for"),
        trusted_proxies=_trusted_proxies(),
    )


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
        raise Unauthenticated("api.authentication_required")
    with db() as connection:
        session = resolve_session(connection, token)
    if not session:
        raise Unauthenticated("api.invalid_session")
    request.state.session = session
    return session["owner"]


def _actor(request: Request) -> str:
    """The audit actor of this signed-in request: ``owner`` or ``emergency``."""
    return session_actor(getattr(request.state, "session", None))


class NoVault(Forbidden):
    """No operational Vault exists yet: the Session has nothing to look at."""

    def __init__(self) -> None:
        super().__init__("api.no_vault")


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


_OPERATIONAL_VAULT = "v.lifecycle IN ('active', 'reconnect_required')"


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
                f"{VAULT_ROW_SQL} WHERE v.id=%s AND {_OPERATIONAL_VAULT}",
                (requested_id,),
            ).fetchone()
        if not vault and explicit:
            raise NotFound("api.protected_vault_not_found")
        if not vault:
            vault = connection.execute(
                f"""
                {VAULT_ROW_SQL} WHERE {_OPERATIONAL_VAULT}
                ORDER BY v.name
                LIMIT 1
                """
            ).fetchone()
        if not vault:
            raise NoVault()
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
                raise Conflict("api.session_changed")
            session.update(updated)
    return vault


def normalize_directory(value: str, *, api: bool = False) -> str:
    """Return a safe catalog directory (the root is an empty string).

    The default names the folder in its refusal; routed API calls opt into
    the shared logical-path contract.
    """
    if not value:
        return ""
    try:
        return safe_relative_path(value).as_posix().rstrip("/")
    except InvalidLogicalPath as exc:
        if api:
            raise
        raise Invalid("api.invalid_folder") from exc
