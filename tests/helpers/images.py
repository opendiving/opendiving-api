"""Image fixtures for the avatar tests.

Built rather than committed as files, for the same reason `tests/helpers/fit.py` builds FIT
binaries: a fixture whose bytes are checked in is a fixture nobody can read the intent of,
and every one of these exists to exercise a specific branch of
`services/user_avatars._normalize`.

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
    and the GPS has to be *gone* (a diver's avatar must not carry where it was taken).
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
    `BytesIO` before building the raster, which the PNG and GIF plugins do not do, so a
    700-byte WebP outweighs every other input of the same dimensions. It is an accepted
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
