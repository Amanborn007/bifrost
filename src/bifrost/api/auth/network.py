"""Peer-address helpers shared by the identity providers and middleware."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

LOOPBACK_NETWORKS: tuple[Network, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)


def parse_networks(cidrs: Iterable[str]) -> tuple[Network, ...]:
    return tuple(ipaddress.ip_network(c, strict=False) for c in cidrs)


def peer_in(host: str | None, networks: Iterable[Network]) -> bool:
    """True when ``host`` is an IP address inside any of ``networks``.

    A missing or non-IP peer (a unix socket, a test client name) is never
    trusted.
    """
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr.version == net.version and addr in net for net in networks)


def is_loopback(host: str | None) -> bool:
    return peer_in(host, LOOPBACK_NETWORKS)
