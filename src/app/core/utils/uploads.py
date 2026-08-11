import unicodedata
from urllib.parse import quote

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


def _ascii_fallback(name: str, *, default: str) -> str:
    """Fold a filename onto ASCII for the plain `filename` parameter.

    NFKD splits an accented letter into a base letter plus a combining mark, so dropping
    what stays non-ASCII afterwards leaves "café.jpg" readable as "cafe.jpg" rather than
    losing the vowel outright. It also maps fullwidth punctuation onto its ASCII twin
    (`＂` -> `"`, `／` -> `/`), which is why the folded name goes back through
    `safe_filename`: it arrives needing the same scrub the original already had.

    A wholly non-ASCII name folds away to nothing or to a bare extension, so `default`
    supplies the stem - "潜水.jpg" downloads as "card.jpg", not as an extensionless
    placeholder or a dotfile.
    """
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    folded = safe_filename(folded, default=default)
    if folded.startswith("."):
        folded = f"{default}{folded}"
    return folded[:255]


def content_disposition_attachment(filename: str | None, *, default: str = "file") -> str:
    """Build the `Content-Disposition` value that offers a stored file as a download.

    Starlette encodes header values as latin-1, so interpolating a filename straight into
    the header raises `UnicodeEncodeError` the moment it holds anything outside that range
    - a CJK or emoji name, which `safe_filename` deliberately keeps because
    `original_filename` is also what the clients display. That exception fires while the
    response is being built, so it isn't a garbled download: it's a 500 on every fetch of
    that file, forever.

    RFC 6266's two-parameter form is the fix. `filename*` carries the real name
    percent-encoded as UTF-8 for anything that understands it (every current browser), and
    the plain `filename` carries an ASCII folding for anything that doesn't - `curl -OJ`
    reads only the latter. `quote` escapes everything outside the unreserved set, so what
    lands in the header is always ASCII and always within RFC 5987's `attr-char`.

    Building this here rather than folding at upload time also repairs the rows already
    stored, which hold their filename in full Unicode: only the header has to be narrow,
    so only the header is narrowed.
    """
    name = safe_filename(filename, default=default)
    fallback = _ascii_fallback(name, default=default)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


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
