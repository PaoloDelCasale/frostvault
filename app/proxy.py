from __future__ import annotations

import ipaddress
from typing import List

IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(raw: str) -> List[IpNetwork]:
    """Parse a comma-separated CIDR list, raising ValueError on any bad entry."""
    networks: List[IpNetwork] = []
    for part in raw.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        networks.append(ipaddress.ip_network(candidate, strict=False))
    return networks


def _is_trusted(address: str, trusted_proxies: List[IpNetwork]) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in trusted_proxies)


def resolve_client_ip(
    *,
    peer: str | None,
    forwarded_for: str | None,
    trusted_proxies: List[IpNetwork],
) -> str | None:
    """Resolve the real client address behind zero or more Trusted Proxies.

    ``X-Forwarded-For`` is believed only when the direct ``peer`` is itself a
    Trusted Proxy; otherwise the socket peer wins so a client cannot forge its
    own address.
    """
    if not peer:
        return None
    if not forwarded_for or not _is_trusted(peer, trusted_proxies):
        return peer
    hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
    for hop in reversed(hops):
        if not _is_trusted(hop, trusted_proxies):
            return hop
    return hops[0] if hops else peer
