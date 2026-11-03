"""The two things the screenshot tool does with PNG bytes: read the size, and stack strips.

Stdlib only. :func:`stitch_png` joins the bands a large screen is captured in
(see :mod:`canopy_mcp.tools.screenshot`) without decoding the pixels: each
band's scanlines are copied as they were filtered, and only its first
scanline is rewritten so it no longer depends on the row above.
"""

from __future__ import annotations

import zlib
import struct

__all__ = ["PNG_SIGNATURE", "png_chunk", "png_parts", "png_size", "stitch_png"]

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_size(data: bytes) -> tuple[int, int]:
    """``(width, height)`` from a PNG's IHDR chunk."""
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise ValueError("not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


_PNG_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}


def png_parts(data: bytes) -> tuple[bytes, bytes]:
    """``(IHDR body, filtered scanlines)`` of a PNG; ``ValueError`` when it is not one."""
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG")
    header, idat, at = b"", [], len(PNG_SIGNATURE)
    while at + 8 <= len(data):
        (length,), kind = struct.unpack(">I", data[at : at + 4]), data[at + 4 : at + 8]
        body = data[at + 8 : at + 8 + length]
        if kind == b"IHDR":
            header = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        at += 12 + length
    if len(header) != 13:
        raise ValueError("PNG without an IHDR chunk")
    return header, zlib.decompress(b"".join(idat))


def _unfilter_first_row(row: bytes, bpp: int) -> bytes:
    """A band's first scanline with filter 0, so it no longer depends on the row above.

    The band was encoded with nothing above it; joined below another band the
    same bytes would be read against that band's last row. With nothing above,
    Up is the raw bytes and Paeth is Sub.
    """
    kind, out = row[0], bytearray(row[1:])
    if kind in (1, 4):
        for i in range(bpp, len(out)):
            out[i] = (out[i] + out[i - bpp]) & 0xFF
    elif kind == 3:
        for i in range(bpp, len(out)):
            out[i] = (out[i] + (out[i - bpp] >> 1)) & 0xFF
    elif kind not in (0, 2):
        raise ValueError(f"unknown PNG filter type {kind}")
    return b"\x00" + bytes(out)


def png_chunk(kind: bytes, body: bytes) -> bytes:
    """One PNG chunk: length, ``kind``, ``body`` and its CRC."""
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def stitch_png(strips: list[bytes]) -> bytes:
    """One PNG of the given PNGs stacked top to bottom.

    The strips must agree in width, bit depth and colour type and be
    non-interlaced, 8 or 16 bit, not palette; anything else is a ``ValueError``.
    Only each strip's first scanline is rewritten, the rest is copied as it was
    filtered.
    """
    if not strips:
        raise ValueError("no PNG to stitch")
    layout, height, rows = None, 0, []
    for strip in strips:
        header, lines = png_parts(strip)
        width, strip_height, depth, colour, compression, filtering, interlace = struct.unpack(">IIBBBBB", header)
        if colour not in _PNG_CHANNELS or depth not in (8, 16) or compression or filtering or interlace:
            raise ValueError("PNG layout the stitcher does not handle")
        if layout is None:
            layout = (width, depth, colour)
        elif layout != (width, depth, colour):
            raise ValueError("PNG strips differ in width, bit depth or colour type")
        bpp = _PNG_CHANNELS[colour] * depth // 8
        stride = 1 + width * bpp
        if len(lines) != stride * strip_height:
            raise ValueError("PNG data does not match its header")
        rows.append(_unfilter_first_row(lines[:stride], bpp))
        rows.append(lines[stride:])
        height += strip_height
    width, depth, colour = layout  # type: ignore[misc]
    header = struct.pack(">IIBBBBB", width, height, depth, colour, 0, 0, 0)
    body = zlib.compress(b"".join(rows), 6)
    return PNG_SIGNATURE + png_chunk(b"IHDR", header) + png_chunk(b"IDAT", body) + png_chunk(b"IEND", b"")
