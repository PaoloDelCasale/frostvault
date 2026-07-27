from __future__ import annotations

import ipaddress
import json
import socket
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

import httpx
import httpcore
from cryptography.fernet import Fernet, InvalidToken

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from authlib.jose import JsonWebKey

from .config import Settings, is_placeholder


class OidcConfigurationError(ValueError):
    pass


class OidcConfigurationConflict(OidcConfigurationError):
    pass


class OidcValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ActiveOidcConfiguration:
    enabled: bool
    issuer: str
    client_id: str
    client_secret: str
    scopes: str
    login_transaction_ttl_seconds: int


def _decode_json(value: Any, *, sqlite: bool) -> Any:
    return json.loads(value) if sqlite and isinstance(value, str) else value


def _fernet(settings_obj: Settings) -> Fernet:
    key = settings_obj.oidc_settings_encryption_key.strip()
    if not key:
        raise OidcConfigurationError(
            "oidc_settings_encryption_key_not_configured"
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise OidcConfigurationError(
            "oidc_settings_encryption_key_invalid"
        ) from error


def active_oidc_configuration(
    connection: Any,
    *,
    settings_obj: Settings,
) -> ActiveOidcConfiguration:
    row = _configuration_row(connection)
    if not row or row["active_version"] is None:
        return ActiveOidcConfiguration(
            enabled=settings_obj.oidc_enabled,
            issuer=settings_obj.oidc_issuer,
            client_id=settings_obj.oidc_client_id,
            client_secret=settings_obj.oidc_client_secret,
            scopes=settings_obj.oidc_scopes,
            login_transaction_ttl_seconds=(
                settings_obj.oidc_login_ttl_seconds
            ),
        )
    ciphertext = row["active_secret_ciphertext"]
    try:
        client_secret = (
            _fernet(settings_obj).decrypt(
                ciphertext.encode("ascii")
            ).decode("utf-8")
            if ciphertext
            else ""
        )
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as error:
        raise OidcConfigurationError(
            "oidc_client_secret_decryption_failed"
        ) from error
    scopes = _decode_json(
        row["active_scopes"],
        sqlite=getattr(connection, "backend", None) == "sqlite",
    )
    return ActiveOidcConfiguration(
        enabled=bool(row["active_enabled"]),
        issuer=row["active_issuer"],
        client_id=row["active_client_id"],
        client_secret=client_secret,
        scopes=" ".join(scopes),
        login_transaction_ttl_seconds=row["active_login_ttl_seconds"],
    )


def _configuration_row(connection: Any) -> dict[str, Any] | None:
    return connection.execute(
        "SELECT * FROM oidc_configuration WHERE id=1"
    ).fetchone()


def _locked_configuration_row(connection: Any) -> dict[str, Any] | None:
    sqlite = getattr(connection, "backend", None) == "sqlite"
    if sqlite:
        connection.begin_immediate()
    query = "SELECT * FROM oidc_configuration WHERE id=1"
    if not sqlite:
        query += " FOR UPDATE"
    return connection.execute(query).fetchone()


def managed_oidc_setting_values(connection: Any) -> dict[str, Any]:
    row = _configuration_row(connection)
    if not row or row["active_version"] is None:
        return {}
    scopes = _decode_json(
        row["active_scopes"],
        sqlite=getattr(connection, "backend", None) == "sqlite",
    )
    return {
        "oidc_enabled": bool(row["active_enabled"]),
        "oidc_issuer": row["active_issuer"],
        "oidc_client_id": row["active_client_id"],
        "oidc_client_secret": bool(row["active_secret_ciphertext"]),
        "oidc_scopes": " ".join(scopes),
        "oidc_login_ttl_seconds": row["active_login_ttl_seconds"],
    }


def oidc_host_addresses(hostname: str) -> list[str]:
    return list(
        {
            item[4][0]
            for item in socket.getaddrinfo(
                hostname,
                443,
                type=socket.SOCK_STREAM,
            )
        }
    )


def _safe_host_addresses(
    hostname: str,
    *,
    host_addresses: Callable[[str], list[str]],
) -> list[str]:
    try:
        addresses = host_addresses(hostname)
    except OSError as error:
        raise OidcValidationError("host_resolution_failed") from error
    if not addresses:
        raise OidcValidationError("host_resolution_failed")
    try:
        unsafe = any(
            not ipaddress.ip_address(address).is_global
            for address in addresses
        )
    except ValueError as error:
        raise OidcValidationError("host_resolution_failed") from error
    if unsafe:
        raise OidcValidationError("ssrf_blocked")
    return addresses


def ensure_safe_oidc_url(
    value: str,
    *,
    host_addresses: Callable[[str], list[str]],
) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise OidcValidationError("unsafe_url")
    _safe_host_addresses(parsed.hostname, host_addresses=host_addresses)


class _PinnedNetworkBackend(httpcore.NetworkBackend):
    def __init__(
        self,
        *,
        host_addresses: Callable[[str], list[str]],
        backend: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._host_addresses = host_addresses
        self._backend = backend or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        addresses = _safe_host_addresses(
            host,
            host_addresses=self._host_addresses,
        )
        last_error: Exception | None = None
        for address in addresses:
            try:
                return self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                last_error = error
        assert last_error is not None
        raise last_error

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> Any:
        return self._backend.connect_unix_socket(
            path,
            timeout=timeout,
            socket_options=socket_options,
        )

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class _PinnedHTTPTransport(httpx.HTTPTransport):
    def __init__(
        self,
        *,
        host_addresses: Callable[[str], list[str]],
    ) -> None:
        super().__init__()
        self._pool.close()
        self._pool = httpcore.ConnectionPool(
            network_backend=_PinnedNetworkBackend(
                host_addresses=host_addresses,
            )
        )


def oidc_http_client(
    *,
    timeout: float,
    follow_redirects: bool,
    host_addresses: Callable[[str], list[str]],
) -> httpx.Client:
    return httpx.Client(
        transport=_PinnedHTTPTransport(host_addresses=host_addresses),
        timeout=timeout,
        follow_redirects=follow_redirects,
        trust_env=False,
    )


@contextmanager
def _validation_http(
    client: httpx.Client | None,
    *,
    host_addresses: Callable[[str], list[str]],
) -> Iterator[httpx.Client]:
    if client is not None:
        yield client
    else:
        with oidc_http_client(
            timeout=5,
            follow_redirects=False,
            host_addresses=host_addresses,
        ) as owned:
            yield owned


def oidc_configuration_response(
    connection: Any,
    *,
    settings_obj: Settings,
    callback_url: str,
) -> dict[str, Any]:
    row = _configuration_row(connection)
    if row and row["active_version"] is not None:
        active = {
            "enabled": bool(row["active_enabled"]),
            "issuer": row["active_issuer"],
            "client_id": row["active_client_id"],
            "client_secret_configured": bool(
                row["active_secret_ciphertext"]
            ),
            "scopes": _decode_json(
                row["active_scopes"],
                sqlite=getattr(connection, "backend", None) == "sqlite",
            ),
            "login_transaction_ttl_seconds": (
                row["active_login_ttl_seconds"]
            ),
            "callback_url": callback_url,
            "source": "database",
        }
    else:
        secret = settings_obj.oidc_client_secret.strip()
        active = {
            "enabled": settings_obj.oidc_enabled,
            "issuer": settings_obj.oidc_issuer,
            "client_id": settings_obj.oidc_client_id,
            "client_secret_configured": (
                bool(secret) and not is_placeholder(secret)
            ),
            "scopes": settings_obj.oidc_scopes.split(),
            "login_transaction_ttl_seconds": (
                settings_obj.oidc_login_ttl_seconds
            ),
            "callback_url": callback_url,
            "source": "environment",
        }
    draft = None
    if row and row["draft_version"] is not None:
        validation_matches_draft = (
            row["validated_draft_version"] == row["draft_version"]
        )
        draft = {
            "issuer": row["draft_issuer"],
            "client_id": row["draft_client_id"],
            "client_secret_configured": bool(
                row["draft_secret_ciphertext"]
            ),
            "scopes": _decode_json(
                row["draft_scopes"],
                sqlite=getattr(connection, "backend", None) == "sqlite",
            ),
            "login_transaction_ttl_seconds": (
                row["draft_login_ttl_seconds"]
            ),
            "version": row["draft_version"],
            "validation_status": (
                row["validation_status"]
                if validation_matches_draft
                else "not_validated"
            ),
        }
    last_validation = None
    if row and row["validation_status"] in {"valid", "invalid"}:
        last_validation = {
            "status": row["validation_status"],
            "validated_at": row["validated_at"],
            "error": row["validation_error"],
        }
    return {
        "active": active,
        "draft": draft,
        "configuration_status": (
            "validated"
            if (
                draft is not None
                and row["validation_status"] == "valid"
                and row["validated_draft_version"] == row["draft_version"]
            )
            else "draft"
            if draft is not None
            else ("active" if active["enabled"] else "disabled")
        ),
        "last_validation": last_validation,
    }


def save_oidc_draft(
    connection: Any,
    *,
    issuer: str,
    client_id: str,
    client_secret: str,
    scopes: list[str],
    login_transaction_ttl_seconds: int,
    updated_by: int,
    settings_obj: Settings,
) -> None:
    try:
        encrypted_secret = _fernet(settings_obj).encrypt(
            client_secret.encode("utf-8")
        ).decode("ascii")
    except InvalidToken as error:
        raise OidcConfigurationError("client_secret_encryption_failed") from error
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    connection.execute(
        """
        INSERT INTO oidc_configuration(
            id, active_enabled, draft_version, draft_issuer,
            draft_client_id, draft_secret_ciphertext, draft_scopes,
            draft_login_ttl_seconds, validation_status, updated_by, updated_at
        ) VALUES (1, FALSE, 1, %s, %s, %s, %s, %s, 'not_validated', %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            draft_version=CASE
                WHEN COALESCE(oidc_configuration.active_version, 0)
                   > COALESCE(oidc_configuration.draft_version, 0)
                THEN oidc_configuration.active_version + 1
                ELSE COALESCE(oidc_configuration.draft_version, 0) + 1
            END,
            draft_issuer=excluded.draft_issuer,
            draft_client_id=excluded.draft_client_id,
            draft_secret_ciphertext=excluded.draft_secret_ciphertext,
            draft_scopes=excluded.draft_scopes,
            draft_login_ttl_seconds=excluded.draft_login_ttl_seconds,
            validated_draft_version=NULL,
            validation_status='not_validated',
            validation_error=NULL,
            validated_at=NULL,
            updated_by=excluded.updated_by,
            updated_at=excluded.updated_at
        """,
        (
            issuer,
            client_id,
            encrypted_secret,
            json.dumps(scopes),
            login_transaction_ttl_seconds,
            updated_by,
            now,
        ),
    )


def validate_oidc_draft(
    connection: Any,
    *,
    http_client: httpx.Client | None = None,
    host_addresses: Callable[[str], list[str]] = oidc_host_addresses,
) -> str:
    row = _configuration_row(connection)
    if not row or row["draft_version"] is None:
        raise OidcConfigurationError("oidc_draft_not_found")
    status = "valid"
    error_code = None
    try:
        issuer = row["draft_issuer"]
        ensure_safe_oidc_url(issuer, host_addresses=host_addresses)
        discovery_url = (
            issuer.rstrip("/") + "/.well-known/openid-configuration"
        )
        with _validation_http(
            http_client,
            host_addresses=host_addresses,
        ) as client:
            discovery_response = client.get(discovery_url, timeout=5)
            if discovery_response.status_code != 200:
                raise OidcValidationError("discovery_failed")
            try:
                metadata = discovery_response.json()
            except ValueError as error:
                raise OidcValidationError("discovery_invalid_json") from error
            if not isinstance(metadata, dict):
                raise OidcValidationError("discovery_invalid_json")
            if metadata.get("issuer") != issuer:
                raise OidcValidationError("issuer_mismatch")
            endpoints: dict[str, str] = {}
            for name in (
                "authorization_endpoint",
                "token_endpoint",
                "jwks_uri",
            ):
                endpoint = metadata.get(name)
                if not isinstance(endpoint, str) or not endpoint:
                    raise OidcValidationError(f"{name}_missing")
                ensure_safe_oidc_url(
                    endpoint,
                    host_addresses=host_addresses,
                )
                endpoints[name] = endpoint
            jwks_uri = endpoints["jwks_uri"]
            jwks_response = client.get(jwks_uri, timeout=5)
            if jwks_response.status_code != 200:
                raise OidcValidationError("jwks_failed")
            try:
                jwks = jwks_response.json()
            except ValueError as error:
                raise OidcValidationError("jwks_invalid_json") from error
            if not isinstance(jwks, dict):
                raise OidcValidationError("jwks_invalid_json")
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    JsonWebKey.import_key_set(jwks)
            except (TypeError, ValueError) as error:
                raise OidcValidationError("jwks_invalid") from error
            if not jwks.get("keys"):
                raise OidcValidationError("jwks_empty")
    except (httpx.HTTPError, OidcValidationError) as error:
        status = "invalid"
        error_code = str(error)
    validated_at = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    )
    updated = connection.execute(
        """
        UPDATE oidc_configuration
        SET validated_draft_version=%s, validation_status=%s,
            validation_error=%s, validated_at=%s
        WHERE id=1 AND draft_version=%s
        RETURNING draft_version
        """,
        (
            row["draft_version"],
            status,
            error_code,
            validated_at,
            row["draft_version"],
        ),
    ).fetchone()
    if not updated:
        raise OidcConfigurationError(
            "oidc_draft_changed_during_validation"
        )
    return status


def require_validated_oidc_draft(
    connection: Any,
    *,
    row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = row if row is not None else _configuration_row(connection)
    if not row or row["draft_version"] is None:
        raise OidcConfigurationError("oidc_draft_not_found")
    if (
        row["validation_status"] != "valid"
        or row["validated_draft_version"] != row["draft_version"]
    ):
        raise OidcConfigurationError("oidc_draft_not_validated")
    return row


def activate_oidc_draft(
    connection: Any,
    *,
    updated_by: int,
    settings_obj: Settings,
) -> int:
    if not settings_obj.break_glass_allowed_cidrs.strip():
        raise OidcConfigurationError("break_glass_recovery_unavailable")
    sqlite = getattr(connection, "backend", None) == "sqlite"
    if sqlite:
        connection.begin_immediate()
    admin_query = """
        SELECT id
        FROM users
        WHERE active=TRUE AND is_admin=TRUE
          AND password_hash IS NOT NULL AND password_hash <> ''
    """
    if not sqlite:
        admin_query += " FOR UPDATE"
    recoverable_admins = connection.execute(admin_query).fetchall()
    if not recoverable_admins:
        raise OidcConfigurationError("break_glass_recovery_unavailable")
    configuration = (
        _configuration_row(connection)
        if sqlite
        else _locked_configuration_row(connection)
    )
    require_validated_oidc_draft(connection, row=configuration)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    activated = connection.execute(
        """
        UPDATE oidc_configuration
        SET active_enabled=TRUE,
            active_version=draft_version,
            active_issuer=draft_issuer,
            active_client_id=draft_client_id,
            active_secret_ciphertext=draft_secret_ciphertext,
            active_scopes=draft_scopes,
            active_login_ttl_seconds=draft_login_ttl_seconds,
            draft_version=NULL,
            draft_issuer=NULL,
            draft_client_id=NULL,
            draft_secret_ciphertext=NULL,
            draft_scopes=NULL,
            draft_login_ttl_seconds=NULL,
            updated_by=%s,
            updated_at=%s
        WHERE id=1 AND validation_status='valid'
          AND draft_version=validated_draft_version
        RETURNING active_version
        """,
        (updated_by, now),
    ).fetchone()
    if not activated:
        raise OidcConfigurationError("oidc_draft_changed_during_activation")
    connection.execute("DELETE FROM oidc_login")
    return int(activated["active_version"])


def disable_oidc(
    connection: Any,
    *,
    updated_by: int,
    settings_obj: Settings,
) -> int:
    row = _locked_configuration_row(connection)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if row and row["active_version"] is not None:
        version = int(row["active_version"]) + 1
        connection.execute(
            """
            UPDATE oidc_configuration
            SET active_enabled=FALSE, active_version=%s,
                updated_by=%s, updated_at=%s
            WHERE id=1
            """,
            (version, updated_by, now),
        )
    else:
        version = 1
        connection.execute(
            """
            INSERT INTO oidc_configuration(
                id, active_enabled, active_version, active_issuer,
                active_client_id, active_secret_ciphertext, active_scopes,
                active_login_ttl_seconds, updated_by, updated_at
            ) VALUES (1, FALSE, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                active_enabled=FALSE,
                active_version=excluded.active_version,
                active_issuer=excluded.active_issuer,
                active_client_id=excluded.active_client_id,
                active_secret_ciphertext=excluded.active_secret_ciphertext,
                active_scopes=excluded.active_scopes,
                active_login_ttl_seconds=excluded.active_login_ttl_seconds,
                updated_by=excluded.updated_by,
                updated_at=excluded.updated_at
            """,
            (
                version,
                settings_obj.oidc_issuer,
                settings_obj.oidc_client_id,
                None,
                json.dumps(settings_obj.oidc_scopes.split()),
                settings_obj.oidc_login_ttl_seconds,
                updated_by,
                now,
            ),
        )
    connection.execute("DELETE FROM oidc_login")
    return version


def rotate_oidc_secret(
    connection: Any,
    *,
    client_secret: str,
    updated_by: int,
    settings_obj: Settings,
) -> int:
    row = _locked_configuration_row(connection)
    if not row or row["active_version"] is None:
        raise OidcConfigurationConflict(
            "oidc_managed_configuration_not_active"
        )
    encrypted_secret = _fernet(settings_obj).encrypt(
        client_secret.encode("utf-8")
    ).decode("ascii")
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    version = int(row["active_version"]) + 1
    connection.execute(
        """
        UPDATE oidc_configuration
        SET active_version=%s, active_secret_ciphertext=%s,
            updated_by=%s, updated_at=%s
        WHERE id=1
        """,
        (version, encrypted_secret, updated_by, now),
    )
    connection.execute("DELETE FROM oidc_login")
    return version
