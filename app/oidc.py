"""OIDC Authorization Code + PKCE login against the provider set in env.

The provider (e.g. Pocket ID) is configured only through ``OIDC_*``
environment variables (#412). The first successful login binds the
provider Identity (issuer and subject) to the single Owner; later logins
must present the same Identity.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import warnings
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator
from urllib.parse import urlencode

import httpx

# authlib.jose is deprecated in favour of joserfc but supported until Authlib
# 2.0; silence the one-time import warning to keep application logs clean.
warnings.filterwarnings(
    "ignore",
    message="authlib.jose module is deprecated",
)
from authlib.jose import JsonWebKey, jwt

from .config import Settings, settings
from .oidc_network import (
    OidcValidationError,
    ensure_safe_oidc_url,
    oidc_http_client,
    oidc_host_addresses,
)


STATE_BYTES = 32
NONCE_BYTES = 32
VERIFIER_BYTES = 64


class OidcError(Exception):
    """A recoverable failure in the OIDC login flow.

    ``reason`` is a short machine-readable code (e.g. ``unknown_state``,
    ``nonce_mismatch``) that callers may surface without leaking details.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OidcClaims:
    def __init__(
        self,
        *,
        issuer: str,
        subject: str,
        return_to: str | None,
        raw: dict[str, Any],
    ) -> None:
        self.issuer = issuer
        self.subject = subject
        self.return_to = return_to
        self.raw = raw


def _configuration(settings_obj: Settings | None = None) -> Settings:
    configuration = settings_obj or settings
    if not configuration.oidc_enabled:
        raise OidcError("disabled")
    return configuration


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _s256_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


@contextmanager
def _http(
    client: httpx.Client | None,
    *,
    host_addresses: Callable[[str], list[str]],
) -> Iterator[httpx.Client]:
    if client is not None:
        yield client
    else:
        with oidc_http_client(
            timeout=10,
            follow_redirects=False,
            host_addresses=host_addresses,
        ) as owned:
            yield owned


def _discover(
    client: httpx.Client,
    configuration: Settings,
    host_addresses: Callable[[str], list[str]],
) -> dict[str, Any]:
    url = (
        configuration.oidc_issuer.rstrip("/")
        + "/.well-known/openid-configuration"
    )
    try:
        ensure_safe_oidc_url(url, host_addresses=host_addresses)
    except OidcValidationError as error:
        raise OidcError(str(error)) from error
    response = client.get(url)
    if response.status_code != 200:
        raise OidcError("discovery_failed")
    return response.json()


def begin_login(
    connection: Any,
    *,
    redirect_uri: str,
    return_to: str | None = None,
    prompt: str | None = None,
    http_client: httpx.Client | None = None,
    host_addresses: Callable[[str], list[str]] = oidc_host_addresses,
) -> str:
    configuration = _configuration()
    with _http(http_client, host_addresses=host_addresses) as client:
        metadata = _discover(client, configuration, host_addresses)
    state = secrets.token_urlsafe(STATE_BYTES)
    nonce = secrets.token_urlsafe(NONCE_BYTES)
    code_verifier = secrets.token_urlsafe(VERIFIER_BYTES)
    now = _now()
    connection.execute(
        """
        INSERT INTO oidc_login(
            state, nonce, code_verifier, return_to, created_at, expires_at
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            state,
            nonce,
            code_verifier,
            return_to,
            now,
            now + timedelta(seconds=configuration.oidc_login_ttl_seconds),
        ),
    )
    query = urlencode(
        {
            "response_type": "code",
            "client_id": configuration.oidc_client_id,
            "redirect_uri": redirect_uri,
            "scope": configuration.oidc_scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": _s256_challenge(code_verifier),
            "code_challenge_method": "S256",
            **({"prompt": prompt} if prompt else {}),
        }
    )
    return f"{metadata['authorization_endpoint']}?{query}"


def _exchange_code(
    metadata: dict[str, Any],
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client: httpx.Client,
    configuration: Settings,
    host_addresses: Callable[[str], list[str]],
) -> dict[str, Any]:
    try:
        ensure_safe_oidc_url(
            metadata["token_endpoint"],
            host_addresses=host_addresses,
        )
    except (KeyError, OidcValidationError) as error:
        raise OidcError("unsafe_token_endpoint") from error
    response = client.post(
        metadata["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": configuration.oidc_client_id,
            "client_secret": configuration.oidc_client_secret,
            "code_verifier": code_verifier,
        },
    )
    if response.status_code != 200:
        raise OidcError("token_exchange_failed")
    return response.json()


def _validate_id_token(
    metadata: dict[str, Any],
    id_token: str,
    *,
    nonce: str,
    access_token: str | None,
    client: httpx.Client,
    configuration: Settings,
    host_addresses: Callable[[str], list[str]],
) -> dict[str, Any]:
    try:
        ensure_safe_oidc_url(
            metadata["jwks_uri"],
            host_addresses=host_addresses,
        )
    except (KeyError, OidcValidationError) as error:
        raise OidcError("unsafe_jwks_uri") from error
    jwks_response = client.get(metadata["jwks_uri"])
    if jwks_response.status_code != 200:
        raise OidcError("jwks_failed")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        key_set = JsonWebKey.import_key_set(jwks_response.json())
        try:
            claims = jwt.decode(id_token, key_set)
        except Exception as error:  # noqa: BLE001 - normalize to a login error
            raise OidcError("bad_signature") from error
    if claims.get("iss") != configuration.oidc_issuer:
        raise OidcError("bad_issuer")
    audience = claims.get("aud")
    allowed = audience if isinstance(audience, list) else [audience]
    if configuration.oidc_client_id not in allowed:
        raise OidcError("bad_audience")
    expires = claims.get("exp")
    if expires is None or _now().timestamp() >= float(expires):
        raise OidcError("token_expired")
    if claims.get("nonce") != nonce:
        raise OidcError("nonce_mismatch")
    if "at_hash" in claims:
        if access_token is None or claims["at_hash"] != _at_hash(access_token):
            raise OidcError("at_hash_mismatch")
    return dict(claims)


def _at_hash(access_token: str) -> str:
    digest = hashlib.sha256(access_token.encode("ascii")).digest()
    return _b64url(digest[: len(digest) // 2])


def complete_login(
    connection: Any,
    *,
    state: str,
    code: str,
    redirect_uri: str,
    http_client: httpx.Client | None = None,
    host_addresses: Callable[[str], list[str]] = oidc_host_addresses,
) -> OidcClaims:
    configuration = _configuration()
    row = connection.execute(
        "SELECT * FROM oidc_login WHERE state=%s",
        (state,),
    ).fetchone()
    if not row:
        raise OidcError("unknown_state")
    # Single-use: the transient state is spent whether or not validation passes.
    connection.execute("DELETE FROM oidc_login WHERE id=%s", (row["id"],))
    if _now() >= row["expires_at"]:
        raise OidcError("expired")
    with _http(http_client, host_addresses=host_addresses) as client:
        metadata = _discover(client, configuration, host_addresses)
        tokens = _exchange_code(
            metadata,
            code=code,
            code_verifier=row["code_verifier"],
            redirect_uri=redirect_uri,
            client=client,
            configuration=configuration,
            host_addresses=host_addresses,
        )
        id_token = tokens.get("id_token")
        if not id_token:
            raise OidcError("token_exchange_failed")
        claims = _validate_id_token(
            metadata,
            id_token,
            nonce=row["nonce"],
            access_token=tokens.get("access_token"),
            client=client,
            configuration=configuration,
            host_addresses=host_addresses,
        )
    return OidcClaims(
        issuer=claims["iss"],
        subject=claims["sub"],
        return_to=row["return_to"],
        raw=claims,
    )


def sweep_expired_logins(connection: Any) -> int:
    removed = connection.execute(
        "DELETE FROM oidc_login WHERE expires_at <= %s", (_now(),)
    )
    return int(removed.rowcount or 0)


class IdentityRejected(Exception):
    """The provider Identity is not the one bound to the Owner."""


def bind_owner_identity(connection: Any, claims: OidcClaims) -> None:
    """Accept ``claims`` as the Owner's Identity, binding it on first login.

    The Owner has at most one Identity. The first successful login binds its
    issuer and subject; afterwards any other subject, or another issuer, is
    rejected (#412, ADR-0003, ADR-0010). Locking the single Owner row
    serializes concurrent first logins: exactly one subject is bound and the
    others see the binding once the lock is released.
    """
    owner = connection.execute(
        "SELECT oidc_issuer, oidc_subject FROM owner WHERE id=1 FOR UPDATE"
    ).fetchone()
    if owner is None:
        raise IdentityRejected("no_owner")
    if owner["oidc_subject"] is not None:
        if (owner["oidc_issuer"], owner["oidc_subject"]) != (claims.issuer, claims.subject):
            raise IdentityRejected("identity_mismatch")
        return
    connection.execute(
        """
        UPDATE owner SET oidc_issuer=%s, oidc_subject=%s, oidc_bound_at=%s
        WHERE id=1
        """,
        (claims.issuer, claims.subject, _now()),
    )
