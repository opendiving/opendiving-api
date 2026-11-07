"""Capture file responses before HTTPX's ASGI transport can buffer their bodies."""

from typing import IO

from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class DownloadTooLarge(Exception):
    pass


class DownloadCapture:
    def __init__(self, app: ASGIApp, output: IO[bytes], limit: int) -> None:
        self.app, self.output, self.limit = app, output, limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        capturing = False
        size = 0

        async def capture(message: Message) -> None:
            nonlocal capturing, size
            if message["type"] == "http.response.start":
                capturing = 200 <= message["status"] < 300
                if capturing:
                    headers = dict(message.get("headers", []))
                    declared = headers.get(b"content-length")
                    if declared is not None and int(declared) > self.limit:
                        raise DownloadTooLarge
            if message["type"] == "http.response.body" and capturing:
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > self.limit:
                    raise DownloadTooLarge
                await run_in_threadpool(self.output.write, chunk)
                # HTTPX receives the status and headers, while the bytes stay in the spool.
                message = {**message, "body": b""}
            await send(message)

        await self.app(scope, receive, capture)
