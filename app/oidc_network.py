"""SSRF-safe HTTP access to the OIDC provider.

Discovery, token, and JWKS requests only reach HTTPS endpoints whose
addresses are global; the connection is pinned to the validated addresses so
DNS cannot be rebound between validation and connect.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
import httpcore


class OidcValidationError(ValueError):
    pass


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
