"""Resolving the caller's IP address, which is what every per-IP rate limit is keyed on.

`request.client.host` is the *socket peer*. Directly exposed that is the caller; behind
any reverse proxy - the bundled Caddy, an ALB, Cloudflare - it is the proxy, identically
for everyone. Every per-IP bucket then collapses into one global bucket, and a single
bot exhausting the magic-link limit locks sign-in out for the whole instance.

The fix is not to trust `X-Forwarded-For` unconditionally: that header is caller-supplied,
so trusting it on a directly-reachable deployment lets anyone forge a fresh identity per
request and skip the limits entirely - strictly worse than the bug it fixes. So the
header is only consulted when the socket peer is a proxy the operator has explicitly
declared via `TRUSTED_PROXY_IPS`, and the value taken is the right-most address that
isn't itself one of those proxies. Right-most matters: a client can prepend whatever it
likes to `X-Forwarded-For`, and only the entries appended by trusted infrastructure -
which sit at the end - are worth anything.

With `TRUSTED_PROXY_IPS` unset (the default) this is exactly the old behaviour, so a
direct-to-uvicorn deployment stays correct without configuring anything.
"""

from functools import lru_cache
from ipaddress import IPv6Address, ip_address, ip_network

from fastapi import Request

from ..config import settings, split_csv

UNKNOWN_CLIENT = "unknown"


@lru_cache(maxsize=1)
def _trusted_networks() -> tuple:
    """Parse `TRUSTED_PROXY_IPS` once. Accepts bare addresses and CIDR blocks, since a
    proxy in Docker or Kubernetes usually has an address from a range rather than a
    fixed one (e.g. `10.0.0.0/8`).
    """
    # `strict=False` so a host address with a prefix (10.1.2.3/24) is accepted rather
    # than raising - operators write those and mean the block.
    return tuple(ip_network(entry, strict=False) for entry in split_csv(settings.TRUSTED_PROXY_IPS))


def _is_trusted(raw: str) -> bool:
    try:
        parsed = ip_address(raw)
    except ValueError:
        return False
    return any(parsed in network for network in _trusted_networks())


def client_ip(request: Request) -> str:
    """The caller's IP, honouring `X-Forwarded-For` only from a trusted proxy.

    Returns `"unknown"` when there is no peer at all (ASGI transports aren't required to
    provide one). That is a single shared bucket, which is the conservative direction:
    unattributable traffic throttles together rather than not at all.
    """
    peer = request.client.host if request.client else UNKNOWN_CLIENT

    if not _trusted_networks() or not _is_trusted(peer):
        return peer

    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return peer

    for candidate in reversed([part.strip() for part in forwarded.split(",") if part.strip()]):
        if not _is_trusted(candidate):
            return candidate

    # Every hop claims to be trusted infrastructure - nothing attributable to a caller.
    return peer


def client_ip_bucket(request: Request) -> str:
    """What a per-IP rate limit is keyed on: `client_ip`, with an IPv6 address widened to
    its /64.

    A /64 is the smallest block an IPv6 host is normally handed, so a single machine holds
    2^64 addresses and can present a fresh one per request. Keyed on the full address, a
    per-IP limit bounds nothing for that caller - and on the anonymous forms it is the limit
    that keeps one caller from spending every address's own bucket. IPv4, an IPv4-mapped
    IPv6 address, and `"unknown"` are keyed as they are.
    """
    ip = client_ip(request)
    try:
        parsed = ip_address(ip)
    except ValueError:
        return ip
    if not isinstance(parsed, IPv6Address):
        return ip
    if parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    return str(ip_network(f"{parsed}/64", strict=False))
