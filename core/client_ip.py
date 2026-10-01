"""Who is really on the other end of the socket - and when may we believe a header that says so?

The old rule (main._rl_client_ip) was "the first X-Forwarded-For hop wins". That is only true if
the proxy in front REPLACES the header; if it ever appends, the caller picks their own identity and
therefore their own rate-limit bucket. And it hashed the whole chain into usage telemetry.

The rule here is the standard one:

  * If the TCP peer is NOT a trusted proxy, nothing a header says about the caller is believed. The
    peer is the caller.
  * If the peer IS a trusted proxy, walk X-Forwarded-For from the RIGHT, skipping every hop that is
    itself a trusted proxy; the first untrusted address is the client. (Walking from the left is how
    a caller who sends `X-Forwarded-For: 1.2.3.4` themselves picks the address we attribute to them.)

Trusted proxies are the loopback and private ranges by default - the container is published on
127.0.0.1 only, so its only possible peers are Caddy on the host (seen from inside as the Docker
bridge gateway) and the box itself. Override with TRUSTED_PROXY_CIDRS (comma separated). The default
is a non-empty literal on purpose: scripts/check_deploy_env.py treats a getenv with a non-empty
default as optional, so adding this knob does not make the deploy demand a new variable.
"""
from __future__ import annotations

import ipaddress
import os
from functools import lru_cache
from typing import Optional

_DEFAULT_TRUSTED = (
    "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7,fe80::/10"
)


@lru_cache(maxsize=8)
def _parse_networks(raw: str):
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return tuple(nets)


def trusted_networks():
    return _parse_networks(os.getenv("TRUSTED_PROXY_CIDRS", _DEFAULT_TRUSTED))


def _parse_ip(value: Optional[str]):
    if not value:
        return None
    text = value.strip().strip("[]")
    # Drop a port: "1.2.3.4:5678" (IPv4 only - a bare IPv6 literal has colons of its own).
    if text.count(":") == 1 and "." in text:
        text = text.split(":", 1)[0]
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def is_trusted_proxy(host: Optional[str]) -> bool:
    ip = _parse_ip(host)
    if ip is None:
        return False
    return any(ip in net for net in trusted_networks())


def _hops(xff: Optional[str]) -> list:
    out = []
    for piece in (xff or "").split(","):
        ip = _parse_ip(piece)
        if ip is not None:
            out.append(ip)
    return out


def resolve_client_ip(
    peer_host: Optional[str],
    forwarded_for: Optional[str] = None,
    real_ip: Optional[str] = None,
) -> str:
    """The best-supported client address; "unknown" only when nothing at all is available."""
    peer = _parse_ip(peer_host)
    if peer is None:
        # No socket peer (a test client, an in-process call): the headers are all there is, so
        # take the leftmost parseable hop rather than refuse to attribute anything.
        hops = _hops(forwarded_for)
        if hops:
            return str(hops[0])
        r = _parse_ip(real_ip)
        if r is not None:
            return str(r)
        return (peer_host or "").strip() or "unknown"

    if not is_trusted_proxy(peer_host):
        return str(peer)

    for ip in reversed(_hops(forwarded_for)):
        if not any(ip in net for net in trusted_networks()):
            return str(ip)
    r = _parse_ip(real_ip)
    if r is not None and not any(r in net for net in trusted_networks()):
        return str(r)
    # Every hop is a trusted proxy (an internal call, a local health check): the nearest one.
    hops = _hops(forwarded_for)
    return str(hops[0]) if hops else str(peer)


def first_hop(value: Optional[str]) -> str:
    """The first parseable address of an X-Forwarded-For-style value, or ''. For telemetry that
    only has the header (no socket) and must hash ONE address, never the chain."""
    hops = _hops(value)
    return str(hops[0]) if hops else ""
