"""The ASGI download boundary stops oversized bodies before HTTPX buffers them."""

import io
import tempfile
from collections.abc import AsyncIterator

import httpx
import pytest
from starlette.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

from src.app.mcp.downloads import DownloadCapture, DownloadTooLarge


@pytest.mark.asyncio
async def test_unknown_length_is_bounded_while_streaming() -> None:
    consumed = 0

    async def chunks() -> AsyncIterator[bytes]:
        nonlocal consumed
        for _ in range(100):
            consumed += 1
            yield b"abc"

    app = StreamingResponse(chunks(), media_type="application/zip")
    with io.BytesIO() as output:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=DownloadCapture(app, output, limit=5)), base_url="http://testserver"
        ) as client:
            with pytest.raises(DownloadTooLarge):
                await client.get("/")
        assert output.getvalue() == b"abc"
    assert consumed == 2


@pytest.mark.asyncio
async def test_file_bytes_spill_to_disk_and_do_not_enter_httpx_response() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b"abc"
        yield b"def"

    with tempfile.SpooledTemporaryFile(max_size=3, mode="w+b") as output:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=DownloadCapture(StreamingResponse(chunks(), media_type="application/zip"), output, limit=6)
            ),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/")
        assert response.status_code == 200
        assert response.content == b""
        output.seek(0)
        assert output.read() == b"abcdef"


@pytest.mark.asyncio
async def test_http_errors_keep_their_json_status_and_body() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await JSONResponse({"detail": "No avatar set"}, status_code=404)(scope, receive, send)

    with io.BytesIO() as output:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=DownloadCapture(app, output, limit=3)), base_url="http://testserver"
        ) as client:
            response = await client.get("/")
        assert response.status_code == 404
        assert response.json() == {"detail": "No avatar set"}
        assert output.getvalue() == b""
