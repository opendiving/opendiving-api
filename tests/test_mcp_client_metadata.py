"""Client metadata URLs never turn into a request to a private network or a redirect."""

import socket
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from src.app.mcp.client_metadata import fetch_client_metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://example.com:invalid/client.json",
        "https://[invalid/client.json",
        "http://example.com/client.json",
        "https://example.com/",
        "https://user:password@example.com/client.json",
        "https://example.com:8443/client.json",
    ],
)
async def test_refuses_unsafe_metadata_urls(url: str) -> None:
    assert await fetch_client_metadata(url) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.1", "::1", "224.0.0.1"])
async def test_refuses_private_or_multicast_dns(address: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    resolver = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    assert await fetch_client_metadata("https://client.example/client.json") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status,size,expected", [(200, 0, True), (302, 0, False), (200, 40000, False)])
async def test_pins_dns_and_bounds_metadata(
    status: int, size: int, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    client_id = "https://client.example/client.json"
    resolver = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "client.example"
        assert request.extensions["sni_hostname"] == "client.example"
        return httpx.Response(
            status,
            headers={"Content-Type": "application/json", "Location": "http://127.0.0.1/"},
            json={
                "client_id": client_id,
                "redirect_uris": ["https://client.example/callback"],
                "token_endpoint_auth_method": "none",
                "client_name": "x" * size,
            },
        )

    original = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    metadata = await fetch_client_metadata(client_id)
    assert (metadata is not None) == expected
    assert len(requests) == 1


@pytest.mark.parametrize("host", ["127.0.0.1", "[::1]"])
def test_native_loopback_ports_are_dynamic_but_callback_identity_is_exact(host: str) -> None:
    from mcp.shared.auth import InvalidRedirectUriError
    from pydantic import AnyUrl

    from src.app.mcp.oauth import RegisteredClient

    client = RegisteredClient(
        client_id="native", application_type="native", redirect_uris=[AnyUrl(f"http://{host}:1234/callback")]
    )
    assert client.validate_redirect_uri(AnyUrl(f"http://{host}:9876/callback")).port == 9876
    for changed in (f"http://{host}:9876/other", "http://localhost:9876/callback", f"http://{host}:9876/callback?x=1"):
        with pytest.raises(InvalidRedirectUriError):
            client.validate_redirect_uri(AnyUrl(changed))
    client.application_type = "web"
    with pytest.raises(InvalidRedirectUriError):
        client.validate_redirect_uri(AnyUrl(f"http://{host}:9876/callback"))
