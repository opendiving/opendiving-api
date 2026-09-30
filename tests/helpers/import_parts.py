"""An import's request parts built from bytes, for tests that read files without a request.

`services/logbook_import/parts.py` builds these off a streamed body; a test that is about what
the import makes of its files rather than about the body hands them over directly.
"""

import hashlib
import io
import json
import uuid as uuid_pkg
from dataclasses import dataclass
from tempfile import SpooledTemporaryFile
from typing import Any

from starlette.requests import Request

from src.app.services.logbook_import import ImportPart, LoadedBatch, LoadedImport, load_import


def part_of(data: bytes, filename: str = "logbook.divejson", index: int = 0) -> ImportPart:
    spool: Any = SpooledTemporaryFile()
    spool.write(data)
    spool.seek(0)
    return ImportPart(
        index=index, filename=filename, size=len(data), sha256=hashlib.sha256(data).hexdigest(), spool=spool
    )


def parts_of(*files: tuple[str, bytes]) -> list[ImportPart]:
    """One part per `(filename, bytes)`, indexed in the order given."""
    return [part_of(data, name, index) for index, (name, data) in enumerate(files)]


@dataclass
class OneFile:
    """A batch of one file, entered as that file's loaded document."""

    batch: LoadedBatch

    def __enter__(self) -> LoadedImport:
        (loaded,) = self.batch.documents
        return loaded

    def __exit__(self, *exc: object) -> None:
        self.batch.close()


async def load_one(data: bytes, filename: str = "logbook.divejson") -> OneFile:
    """`load_import` over one file, for `with await load_one(...) as loaded:`."""
    return OneFile(await load_import([part_of(data, filename)]))


def multipart(files: list[tuple[str, bytes]], fields: dict[str, Any] | None = None) -> tuple[bytes, str]:
    """A multipart body carrying each file as a `file` part, and `fields` beside them - a
    value that is not a string sent as its JSON."""
    boundary = uuid_pkg.uuid4().hex
    body = io.BytesIO()
    for name, value in (fields or {}).items():
        text = value if isinstance(value, str) else json.dumps(value)
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.write(text.encode() + b"\r\n")
    for filename, data in files:
        body.write(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n".encode()
        )
        body.write(data + b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())
    return body.getvalue(), f"multipart/form-data; boundary={boundary}"


def import_request(files: list[tuple[str, bytes]], fields: dict[str, Any] | None = None) -> Request:
    """A request an import route reads its body from, for a test calling the route directly."""
    body, content_type = multipart(files, fields)
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/import/logbook",
        "headers": [(b"content-type", content_type.encode()), (b"content-length", str(len(body)).encode())],
        "query_string": b"",
    }
    return Request(scope, receive)
