from __future__ import annotations

import ipaddress

from .config import settings
from .proxy import IpNetwork, parse_networks


# The application always trusts the machine it runs on, regardless of how the
# operator configures BREAK_GLASS_ALLOWED_CIDRS.
_LOOPBACK_NETWORKS: tuple[IpNetwork, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)


def is_break_glass_allowed(client_ip: str | None) -> bool:
    """Whether the Emergency Password is accepted from ``client_ip``.

    Allowed = loopback ∪ BREAK_GLASS_ALLOWED_CIDRS. An empty list therefore means
    loopback only, and an unknown or unparseable address is denied (fail-closed).
    """
    if not client_ip:
        return False
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    if any(address in network for network in _LOOPBACK_NETWORKS):
        return True
    try:
        allowed = parse_networks(settings.break_glass_allowed_cidrs)
    except ValueError:
        return False
    return any(address in network for network in allowed)
