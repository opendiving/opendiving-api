"""The lossless metadata strip every kept picture original goes through.

A kept original is the file the diver picked, minus everything but the image: it decodes to
exactly the upload's pixels and keeps its orientation, and it carries no location. Rather than
naming what to drop - EXIF, GPS, XMP, IPTC, C2PA Content Credentials (which copy EXIF fields
and embed a thumbnail), comments, vendor segments, an MPF secondary image, a motion photo's
appended video - it keeps what the image needs and drops everything else, including every
byte after the image ends. A metadata kind nobody has heard of yet goes with the rest.

**Stripping a stripped file changes no byte**: the kept segments are copied in order, and the
one rewrite, EXIF cut down to its Orientation tag, is written the same way every time. So an
original this app wrote reads back with the digest it was stored under.

Walking the container is the whole technique. JPEG and PNG are the two formats a picture
original may be; WebP is here too because a certification card may be one, and a card scan goes
through the same strip (`services/certification_files.py`).
"""

import struct
import zlib

from PIL import Image

JPEG_CONTENT_TYPE = "image/jpeg"
PNG_CONTENT_TYPE = "image/png"
WEBP_CONTENT_TYPE = "image/webp"

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_EXIF_HEADER = b"Exif\x00\x00"
_ORIENTATION_TAG = 0x0112

# APPn segments kept by the identifier their payload starts with. Anything else in an APPn
# is metadata.
_KEPT_APP_SEGMENTS = {0xE0: b"JFIF\x00", 0xE2: b"ICC_PROFILE\x00", 0xEE: b"Adobe"}
# DQT, DHT, DAC, DRI, and every SOF but the three codes the standard reserves (C4 is DHT,
# C8 is JPG, CC is DAC).
_KEPT_JPEG_MARKERS = {0xDB, 0xC4, 0xCC, 0xDD} | {code for code in range(0xC0, 0xD0) if code not in (0xC4, 0xC8, 0xCC)}

# Critical chunks, transparency, and the colour chunks that change how the pixels read.
_KEPT_PNG_CHUNKS = {
    b"IHDR",
    b"PLTE",
    b"tRNS",
    b"gAMA",
    b"cHRM",
    b"sRGB",
    b"iCCP",
    b"sBIT",
    b"cICP",
    b"IDAT",
    b"IEND",
}


# The chunks a WebP image is drawn from: the extended header, the bitstreams, alpha, animation
# and the colour profile. EXIF is handled on its own, and XMP and anything unknown are dropped.
_KEPT_WEBP_CHUNKS = {b"VP8X", b"VP8 ", b"VP8L", b"ALPH", b"ANIM", b"ANMF", b"ICCP"}
# `VP8X` flag bits announcing an EXIF and an XMP chunk.
_WEBP_EXIF_FLAG = 0x08
_WEBP_XMP_FLAG = 0x04


class DamagedImageError(ValueError):
    """The container does not parse: a length that runs past the end, or no end at all."""


def sniff_original(data: bytes) -> str | None:
    """`image/jpeg` or `image/png` from the leading bytes, or `None` for anything else."""
    if data.startswith(b"\xff\xd8\xff"):
        return JPEG_CONTENT_TYPE
    if data.startswith(_PNG_SIGNATURE):
        return PNG_CONTENT_TYPE
    return None


def strip_metadata(data: bytes, content_type: str) -> bytes:
    """`data` with nothing left but the image. Raises `DamagedImageError`."""
    if content_type == JPEG_CONTENT_TYPE:
        return _strip_jpeg(data)
    if content_type == PNG_CONTENT_TYPE:
        return _strip_png(data)
    if content_type == WEBP_CONTENT_TYPE:
        return _strip_webp(data)
    raise ValueError(f"no original is kept as {content_type}")


def _orientation(tiff: bytes) -> int | None:
    """The Orientation tag's value, when there is one worth keeping.

    Pillow's own EXIF reader, the same one `exif_transpose` applies the tag with, so the
    kept value is the one the rendition was drawn under. A block it cannot read keeps
    nothing, which leaves the pixels as they were stored.
    """
    exif = Image.Exif()
    try:
        exif.load(tiff)
        value = exif.get(_ORIENTATION_TAG)
    except Exception:
        return None
    return value if isinstance(value, int) and 2 <= value <= 8 else None


def _orientation_tiff(orientation: int) -> bytes:
    """A big-endian TIFF block whose one IFD holds the Orientation tag alone."""
    entry = struct.pack(">HHIHH", _ORIENTATION_TAG, 3, 1, orientation, 0)
    return b"MM\x00\x2a" + struct.pack(">I", 8) + struct.pack(">H", 1) + entry + struct.pack(">I", 0)


def _strip_jpeg(data: bytes) -> bytes:
    out = bytearray(b"\xff\xd8")
    end = len(data)
    position = 2
    exif_written = False
    while True:
        if position >= end or data[position] != 0xFF:
            raise DamagedImageError("the JPEG ends before its image does")
        # Any marker may be preceded by fill bytes.
        while position < end and data[position] == 0xFF:
            position += 1
        if position >= end:
            raise DamagedImageError("the JPEG ends before its image does")
        marker = data[position]
        position += 1
        if marker == 0xD9:
            out += b"\xff\xd9"
            return bytes(out)
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            # Markers with no payload. A restart marker outside a scan, or a second SOI, is
            # not part of any image this keeps.
            if marker == 0xD8:
                raise DamagedImageError("the JPEG starts twice")
            continue

        if position + 2 > end:
            raise DamagedImageError("a JPEG segment runs past the end of the file")
        length = int.from_bytes(data[position : position + 2], "big")
        segment_end = position + length
        if length < 2 or segment_end > end:
            raise DamagedImageError("a JPEG segment runs past the end of the file")
        payload = data[position + 2 : segment_end]

        if marker == 0xDA:
            scan_end = _entropy_coded_end(data, segment_end)
            out += b"\xff\xda" + data[position:scan_end]
            position = scan_end
            continue

        if marker == 0xE1 and payload.startswith(_EXIF_HEADER) and not exif_written:
            exif_written = True
            orientation = _orientation(payload)
            if orientation is not None:
                kept = _EXIF_HEADER + _orientation_tiff(orientation)
                out += b"\xff\xe1" + struct.pack(">H", len(kept) + 2) + kept
        elif marker in _KEPT_JPEG_MARKERS or (
            marker in _KEPT_APP_SEGMENTS and payload.startswith(_KEPT_APP_SEGMENTS[marker])
        ):
            out += bytes((0xFF, marker)) + data[position:segment_end]
        position = segment_end


def _entropy_coded_end(data: bytes, start: int) -> int:
    """Where a scan's entropy-coded data ends: the first marker that is not a stuffed zero or
    a restart. A scan that never reaches one ran off the end of the file."""
    position = start
    while True:
        found = data.find(b"\xff", position)
        if found == -1 or found + 1 >= len(data):
            raise DamagedImageError("the JPEG ends before its image does")
        following = data[found + 1]
        if following == 0x00 or 0xD0 <= following <= 0xD7 or following == 0xFF:
            position = found + 1
            continue
        return found


def _strip_png(data: bytes) -> bytes:
    out = bytearray(_PNG_SIGNATURE)
    end = len(data)
    position = len(_PNG_SIGNATURE)
    exif_written = False
    while True:
        if position + 8 > end:
            raise DamagedImageError("the PNG ends before its image does")
        length = int.from_bytes(data[position : position + 4], "big")
        chunk_type = data[position + 4 : position + 8]
        chunk_end = position + 12 + length
        if chunk_end > end:
            raise DamagedImageError("a PNG chunk runs past the end of the file")
        if chunk_type == b"eXIf" and not exif_written:
            exif_written = True
            orientation = _orientation(data[position + 8 : position + 8 + length])
            if orientation is not None:
                kept = _orientation_tiff(orientation)
                out += struct.pack(">I", len(kept)) + b"eXIf" + kept + struct.pack(">I", zlib.crc32(b"eXIf" + kept))
        elif chunk_type in _KEPT_PNG_CHUNKS:
            out += data[position:chunk_end]
        position = chunk_end
        if chunk_type == b"IEND":
            return bytes(out)


def _strip_webp(data: bytes) -> bytes:
    """The RIFF container walked chunk by chunk, keeping the image's chunks in order. The
    `VP8X` header's EXIF and XMP flags are rewritten to match what is kept, since a reader
    may trust them."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise DamagedImageError("the WebP has no RIFF header")
    end = 8 + int.from_bytes(data[4:8], "little")
    if end > len(data):
        raise DamagedImageError("the WebP ends before its image does")
    chunks: list[tuple[bytes, bytes]] = []
    position = 12
    exif_seen = False
    while position < end:
        if position + 8 > end:
            raise DamagedImageError("a WebP chunk runs past the end of the file")
        chunk_type = data[position : position + 4]
        length = int.from_bytes(data[position + 4 : position + 8], "little")
        chunk_end = position + 8 + length
        if chunk_end > end:
            raise DamagedImageError("a WebP chunk runs past the end of the file")
        payload = data[position + 8 : chunk_end]
        if chunk_type == b"EXIF" and not exif_seen:
            exif_seen = True
            orientation = _orientation(payload)
            # Only the extended format can declare an EXIF chunk.
            if orientation is not None and chunks[:1] and chunks[0][0] == b"VP8X":
                chunks.append((b"EXIF", _orientation_tiff(orientation)))
        elif chunk_type in _KEPT_WEBP_CHUNKS:
            chunks.append((chunk_type, payload))
        position = chunk_end + (length & 1)

    out = bytearray(b"WEBP")
    has_exif = any(chunk_type == b"EXIF" for chunk_type, _ in chunks)
    for chunk_type, payload in chunks:
        if chunk_type == b"VP8X" and payload:
            flags = payload[0] & ~(_WEBP_EXIF_FLAG | _WEBP_XMP_FLAG) | (_WEBP_EXIF_FLAG if has_exif else 0)
            payload = bytes((flags,)) + payload[1:]
        out += chunk_type + struct.pack("<I", len(payload)) + payload + b"\x00" * (len(payload) & 1)
    return b"RIFF" + struct.pack("<I", len(out)) + bytes(out)
