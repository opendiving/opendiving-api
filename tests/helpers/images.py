"""Image fixtures for the picture tests.

Built rather than committed as files, for the same reason `tests/helpers/fit.py` builds FIT
binaries: a fixture whose bytes are checked in is a fixture nobody can read the intent of,
and every one of these exists to exercise a specific branch of
`services/user_pictures._normalize` or of the metadata strip in
`services/picture_originals.py`.

`png_declaring` is the interesting one. Two of the branches are about images too large to
process, and the check that catches them is header-only by design - so the fixture only has
to *declare* the dimensions. Rewriting a real 4x4 PNG's IHDR gives a 74-byte file that
Pillow opens and reports as 8000x7000, where materialising one would cost 168 MB of test
memory to assert something the code never decodes.
"""

import io
import struct
import zlib

from PIL import Image

# Where a PNG's IHDR payload starts: 8-byte signature, 4-byte chunk length, 4-byte type.
_IHDR_DATA_OFFSET = 8 + 4 + 4
_IHDR_DATA_LENGTH = 13


# The orientation fixture is square, so the centre-crop is a no-op and the only thing that
# can move a pixel is `exif_transpose`. Its top-left quadrant is green against red,
# which is what makes "was the rotation applied, and in which direction" readable off two
# pixel samples.
MARKED_JPEG_SIZE = 64


def jpeg_with_exif(*, orientation: int = 6) -> bytes:
    """A JPEG carrying an orientation tag and a GPS block - a phone photo, in miniature.

    Both halves matter. The orientation has to be *applied* (the picture comes out upright)
    and the GPS has to be *gone* (a diver's picture must not carry where it was taken).
    """
    edge = MARKED_JPEG_SIZE
    image = Image.new("RGB", (edge, edge), "red")
    image.paste(Image.new("RGB", (edge // 2, edge // 2), "green"), (0, 0))

    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x8825] = {1: "N", 2: (10.0, 0.0, 0.0), 3: "E", 4: (123.0, 0.0, 0.0)}

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif, quality=95)
    return buffer.getvalue()


def png_with_alpha(*, size: tuple[int, int] = (64, 64)) -> bytes:
    """A PNG that is half transparent, for the "alpha survives" claim."""
    image = Image.new("RGBA", size, (0, 0, 255, 255))
    image.paste((0, 0, 0, 0), (0, 0, size[0], size[1] // 2))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def animated_gif(*, size: tuple[int, int] = (32, 32)) -> bytes:
    """Two frames, red then blue. Only the first should reach the stored avatar."""
    first = Image.new("P", size, 0)
    first.putpalette([255, 0, 0] + [0, 0, 255] + [0] * 762)
    second = Image.new("P", size, 1)
    second.putpalette([255, 0, 0] + [0, 0, 255] + [0] * 762)

    buffer = io.BytesIO()
    first.save(buffer, format="GIF", save_all=True, append_images=[second], duration=100, loop=0)
    return buffer.getvalue()


def png_declaring(width: int, height: int) -> bytes:
    """A tiny, otherwise valid PNG whose header claims `width` x `height`.

    The pixel data is a real 4x4 image, so `Image.open` parses it happily and reports the
    declared size; only a `load()` this code never reaches would notice the disagreement.
    """
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buffer, format="PNG")
    raw = bytearray(buffer.getvalue())

    header = raw[_IHDR_DATA_OFFSET : _IHDR_DATA_OFFSET + _IHDR_DATA_LENGTH]
    header[0:4] = struct.pack(">I", width)
    header[4:8] = struct.pack(">I", height)
    raw[_IHDR_DATA_OFFSET : _IHDR_DATA_OFFSET + _IHDR_DATA_LENGTH] = header

    # The CRC covers the chunk type and its payload, and Pillow checks it.
    chunk = bytes(raw[_IHDR_DATA_OFFSET - 4 : _IHDR_DATA_OFFSET + _IHDR_DATA_LENGTH])
    crc_at = _IHDR_DATA_OFFSET + _IHDR_DATA_LENGTH
    raw[crc_at : crc_at + 4] = struct.pack(">I", zlib.crc32(chunk))
    return bytes(raw)


def plain_png(*, size: tuple[int, int]) -> bytes:
    """A real, fully-decodable PNG of the given size, with no metadata of any kind."""
    buffer = io.BytesIO()
    Image.new("RGB", size, "teal").save(buffer, format="PNG")
    return buffer.getvalue()


def large_jpeg(*, size: tuple[int, int]) -> bytes:
    """A JPEG big enough for the decoder's reduced-scale path to have something to do.

    Gently textured rather than flat: a uniform image compresses to almost nothing, and a
    fixture whose bytes bear no relation to its dimensions is the one that makes a size
    assertion read as a coincidence.
    """
    image = Image.new("RGB", size, "teal")
    for x in range(0, size[0], 64):
        image.paste(Image.new("RGB", (32, size[1]), "orange"), (x, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def webp_with_alpha(*, size: tuple[int, int]) -> bytes:
    """A lossless RGBA WebP - the format that costs the most to decode, per byte.

    `WebPImageFile.load` materializes the whole frame as `bytes` and copies it again into a
    `BytesIO` before building the raster, which the PNG and GIF plugins do not do, so this
    outweighs every other input of the same dimensions - at 1536x1536 it is 158 bytes on
    disk and around 90 MB to decode. It is an accepted
    upload format, so it needs a fixture; two review rounds went by on figures measured
    from PNGs alone.
    """
    image = Image.new("RGBA", size, (0, 0, 255, 180))
    image.paste((255, 0, 0, 0), (0, 0, size[0] // 2, size[1] // 2))
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", lossless=True)
    return buffer.getvalue()


def bmp() -> bytes:
    """A perfectly valid image in a format the `formats=` allowlist does not name."""
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buffer, format="BMP")
    return buffer.getvalue()


# Where a phone photo's metadata lives beyond its EXIF, for the strip to remove. Each is a
# well-formed segment or chunk of its kind; none of them is anything the image needs.
XMP_PAYLOAD = b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta><exif:GPSLatitude>10,0N</exif:GPSLatitude></x:xmpmeta>"
IPTC_PAYLOAD = b"Photoshop 3.0\x008BIM\x04\x04\x00\x00\x00\x00\x00\x0c\x1c\x02\x5a\x00\x06Dahab!"
C2PA_PAYLOAD = b"JP\x00\x01\x00\x00\x00\x01jumbc2pa stds.exif GPSLatitude thumbnail"
COMMENT_PAYLOAD = b"taken at home"
MOTION_PHOTO_VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


def jpeg_segment(marker: int, payload: bytes) -> bytes:
    return bytes((0xFF, marker)) + struct.pack(">H", len(payload) + 2) + payload


def with_jpeg_segments(jpeg: bytes, *segments: bytes, trailer: bytes = b"") -> bytes:
    """`jpeg` with `segments` inserted straight after its SOI, and `trailer` after its EOI."""
    return jpeg[:2] + b"".join(segments) + jpeg[2:] + trailer


def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", zlib.crc32(chunk_type + data))


def with_png_chunks(png: bytes, *chunks: bytes, trailer: bytes = b"") -> bytes:
    """`png` with `chunks` inserted straight after its IHDR, and `trailer` after its IEND."""
    after_ihdr = 8 + 12 + _IHDR_DATA_LENGTH
    return png[:after_ihdr] + b"".join(chunks) + png[after_ihdr:] + trailer


def phone_jpeg(*, size: tuple[int, int] = (64, 48), orientation: int | None = 6) -> bytes:
    """A camera JPEG as it arrives: EXIF with an orientation and a GPS block, XMP and IPTC
    beside it, C2PA Content Credentials and a comment, and a motion photo's video after EOI.

    Stored landscape with its left half green and right half red, so under orientation 6 the
    upright picture is green on top - which is what makes "was it turned" readable off two
    pixels of the rendition.
    """
    width, height = size
    image = Image.new("RGB", size, "red")
    image.paste(Image.new("RGB", (width // 2, height), "green"), (0, 0))
    exif = Image.Exif()
    if orientation is not None:
        exif[0x0112] = orientation
    exif[0x8825] = {1: "N", 2: (10.0, 0.0, 0.0), 3: "E", 4: (123.0, 0.0, 0.0)}
    exif[0x010F] = "PhoneMaker"
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif, quality=90)
    return with_jpeg_segments(
        buffer.getvalue(),
        jpeg_segment(0xE1, XMP_PAYLOAD),
        jpeg_segment(0xED, IPTC_PAYLOAD),
        jpeg_segment(0xEB, C2PA_PAYLOAD),
        jpeg_segment(0xFE, COMMENT_PAYLOAD),
        trailer=MOTION_PHOTO_VIDEO,
    )


def screenshot_png(*, size: tuple[int, int] = (40, 30), alpha: bool = False, orientation: int | None = 8) -> bytes:
    """A PNG carrying what PNGs carry: an eXIf with an orientation and GPS, text and XMP
    chunks, an unknown ancillary chunk, and bytes after IEND."""
    image = Image.new("RGBA" if alpha else "RGB", size, (0, 0, 255, 0) if alpha else "blue")
    image.paste((255, 255, 0, 255) if alpha else (255, 255, 0), (0, 0, size[0] // 2, size[1] // 2))
    exif = Image.Exif()
    if orientation is not None:
        exif[0x0112] = orientation
    exif[0x8825] = {1: "N", 2: (10.0, 0.0, 0.0)}
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", exif=exif)
    return with_png_chunks(
        buffer.getvalue(),
        png_chunk(b"tEXt", b"Comment\x00taken at home"),
        png_chunk(b"iTXt", b"XML:com.adobe.xmp\x00\x00\x00\x00\x00" + XMP_PAYLOAD),
        png_chunk(b"prVt", b"a vendor's private chunk"),
        trailer=b"trailing bytes",
    )


def solid_png(*, size: tuple[int, int], mode: str = "RGB") -> bytes:
    """A PNG of `size` that compresses to almost nothing, for the caps' full-size cases."""
    buffer = io.BytesIO()
    Image.new(mode, size, (20, 120, 200, 255) if mode == "RGBA" else (20, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def gif() -> bytes:
    buffer = io.BytesIO()
    Image.new("P", (8, 8), 0).save(buffer, format="GIF")
    return buffer.getvalue()


def plain_jpeg(*, size: tuple[int, int] = (16, 16)) -> bytes:
    """A JPEG with no metadata of any kind."""
    buffer = io.BytesIO()
    Image.new("RGB", size, "teal").save(buffer, format="JPEG")
    return buffer.getvalue()
