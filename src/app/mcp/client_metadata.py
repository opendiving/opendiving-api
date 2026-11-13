"""HTTPS client metadata with pinned public DNS answers and bounded responses."""

import asyncio
import ipaddress
import json
import socket
from urllib.parse import urlsplit, urlunsplit

import httpx
from mcp.shared.auth import OAuthClientInformationFull

MAX_METADATA_BYTES = 32 * 1024


async def fetch_client_metadata(client_id: str) -> OAuthClientInformationFull | None:
    if len(client_id) > 512:
        return None
    try:
        url = urlsplit(client_id)
        port = url.port
    except ValueError:
        return None
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.fragment
        or url.query
        or port not in (None, 443)
        or not url.path
        or url.path == "/"
    ):
        return None
    try:
        addresses = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(url.hostname, 443, type=socket.SOCK_STREAM), timeout=5
        )
        ips = {ipaddress.ip_address(answer[4][0]) for answer in addresses}
        if not ips or any(not address.is_global or address.is_multicast or address.is_reserved for address in ips):
            return None
        # The connection uses the checked address itself; DNS cannot change between
        # validation and connect. TLS still verifies the original metadata hostname.
        address = sorted(ips, key=str)[0]
        host = f"[{address}]" if address.version == 6 else str(address)
        target = urlunsplit(("https", host, url.path, "", ""))
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
            async with client.stream(
                "GET",
                target,
                headers={"Host": url.netloc, "Accept": "application/json"},
                extensions={"sni_hostname": url.hostname},
            ) as response:
                if response.status_code != 200 or "application/json" not in response.headers.get("content-type", ""):
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_METADATA_BYTES:
                        return None
        metadata = OAuthClientInformationFull.model_validate(json.loads(body))
        return metadata if metadata.client_id == client_id else None
    except OSError, ValueError, httpx.HTTPError, TimeoutError:
        return None
