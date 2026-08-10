from fastapi import HTTPException, UploadFile

_UPLOAD_READ_CHUNK_SIZE = 1024 * 1024  # 1 MB


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
