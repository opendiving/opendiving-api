from fastapi import HTTPException, UploadFile

_UPLOAD_READ_CHUNK_SIZE = 1024 * 1024  # 1 MB


def safe_filename(filename: str | None, *, default: str = "file") -> str:
    """Reduce an uploaded filename to something safe to store and to echo back in a
    `Content-Disposition` header.

    Nothing here ever touches the filesystem, so this is not path-traversal defence; it
    strips directory components and control characters (notably CR/LF and `"`) so the
    value can't break out of the header it later lands in.

    `default` is what an absent, empty or entirely-stripped name falls back to; callers
    pass something recognizable for their own kind of upload ("card", "dive-file") so a
    downloaded file with no usable original name still says what it is.
    """
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '"\\')
    name = name.strip() or default
    return name[:255]


async def read_upload_within_limit(file: UploadFile, max_size: int) -> bytes:
    """Read an upload's full content, rejecting it once it exceeds `max_size`.

    Reads in bounded chunks instead of trusting the `Content-Length` header (which may
    be absent or spoofed) or calling `file.read()` unbounded, so at most `max_size`
    (+ one chunk) bytes are ever buffered in memory.

    Raises `HTTPException(413)` naming the limit in MB, rounded up so that a limit which
    isn't a whole number of megabytes is never reported as smaller than it really is.
    """
    limit_mb = -(-max_size // (1024 * 1024))
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_READ_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_size:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum allowed size is {limit_mb} MB.",
            )
        chunks.append(chunk)
    return b"".join(chunks)
