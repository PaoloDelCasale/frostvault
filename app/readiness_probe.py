"""Local container readiness check; runnable with ``python -m app.readiness_probe``.

Use only the standard library: importing application settings would also parse
unrelated configuration, and this process must not open the database itself.
The Host is an exact allowlist entry, never the connection destination.
"""
from __future__ import annotations

import http.client
import os
import re
import sys

# Concrete ASCII DNS names (including localhost and IPv4), not URLs, ports,
# wildcards, credentials, shell fragments or header control characters. Keep
# spelling/case intact because the application's Host comparison is exact.
_HOSTNAME = re.compile(
    r"(?=.{1,253}\Z)"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?\Z"
)


def _probe_host(allowed_hosts: str) -> str:
    # Match the middleware's comma splitting and whitespace handling, without
    # changing that middleware or adding loopback to its allowlist.
    hosts = [host.strip() for host in allowed_hosts.split(",") if host.strip()]
    if not hosts:
        return "127.0.0.1"
    for host in hosts:
        if _HOSTNAME.fullmatch(host):
            return host
    raise ValueError("No safe concrete Host in ALLOWED_HOSTS")


def main() -> int:
    try:
        host = _probe_host(os.environ.get("ALLOWED_HOSTS", ""))
    except ValueError:
        print("Readiness probe failed: no safe Host in ALLOWED_HOSTS", file=sys.stderr)
        return 1

    # Fixed local socket: no DNS lookup of the public Host, environment proxies,
    # TLS, forwarded headers, shell evaluation, redirects or liveness fallback.
    connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=3)
    try:
        connection.request("GET", "/ready", headers={"Host": host})
        status = connection.getresponse().status
        if status != 200:
            # Do not log the Host, environment, response body or exception text:
            # Docker retains probe output in its inspect/health history.
            print(f"Readiness probe failed: HTTP {status}", file=sys.stderr)
            return 1
        return 0
    except (OSError, http.client.HTTPException):
        print("Readiness probe failed: local request failed", file=sys.stderr)
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
