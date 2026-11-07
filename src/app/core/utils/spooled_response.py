"""A download owns its spool even when sending headers fails before iteration starts."""

from collections.abc import Iterator
from typing import IO

from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send


class SpooledResponse(StreamingResponse):
    def __init__(self, buffer: IO[bytes], *, media_type: str, headers: dict[str, str]) -> None:
        self.buffer = buffer
        super().__init__(self.chunks(), media_type=media_type, headers=headers)

    def chunks(self) -> Iterator[bytes]:
        while chunk := self.buffer.read(64 * 1024):
            yield chunk

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.buffer.close()
