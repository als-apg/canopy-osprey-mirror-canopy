"""PNG reading and strip stitching, against a reference encoder and decoder written here."""

import zlib
import struct

import pytest

from canopy_mcp.png import PNG_SIGNATURE, png_size, png_chunk, png_parts, stitch_png


def test_png_size_reads_ihdr() -> None:
    header = PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + (300).to_bytes(4, "big") + (70).to_bytes(4, "big")
    assert png_size(header + b"\x08\x06\x00\x00\x00") == (300, 70)
    with pytest.raises(ValueError):
        png_size(b"GIF89a" + b"\x00" * 30)


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def _filter_row(kind: int, row: bytes, above: bytes, bpp: int) -> bytes:
    out = bytearray([kind])
    for i, value in enumerate(row):
        a = row[i - bpp] if i >= bpp else 0
        b = above[i]
        c = above[i - bpp] if i >= bpp else 0
        predicted = (0, a, b, (a + b) >> 1, _paeth(a, b, c))[kind]
        out.append((value - predicted) & 0xFF)
    return bytes(out)


def encode_png(rows: list[bytes], width: int, colour: int, filters: list[int]) -> bytes:
    bpp = {2: 3, 6: 4}[colour]
    above, lines = bytes(width * bpp), []
    for kind, row in zip(filters, rows, strict=True):
        lines.append(_filter_row(kind, row, above, bpp))
        above = row
    header = struct.pack(">IIBBBBB", width, len(rows), 8, colour, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(b"".join(lines)))
        + png_chunk(b"IEND", b"")
    )


def decode_png(data: bytes) -> list[bytes]:
    """The raw scanlines of an 8-bit RGB or RGBA PNG."""
    header, lines = png_parts(data)
    width, height, _depth, colour = struct.unpack(">IIBB", header[:10])
    bpp = {2: 3, 6: 4}[colour]
    stride, above, rows = 1 + width * bpp, bytes(width * bpp), []
    for n in range(height):
        line = lines[n * stride : (n + 1) * stride]
        row = bytearray(line[1:])
        for i in range(len(row)):
            a = row[i - bpp] if i >= bpp else 0
            b = above[i]
            c = above[i - bpp] if i >= bpp else 0
            row[i] = (row[i] + (0, a, b, (a + b) >> 1, _paeth(a, b, c))[line[0]]) & 0xFF
        above = bytes(row)
        rows.append(above)
    return rows


@pytest.mark.parametrize("colour", [2, 6])
@pytest.mark.parametrize("first_filter", [0, 1, 2, 3, 4])
def test_stitch_png_keeps_every_pixel_whatever_filter_opens_a_strip(colour: int, first_filter: int) -> None:
    width, bpp = 7, {2: 3, 6: 4}[colour]
    rows = [bytes((x * 37 + y * 101 + x * y) % 256 for x in range(width * bpp)) for y in range(9)]
    strips = [
        encode_png(rows[0:3], width, colour, [first_filter, 4, 3]),
        encode_png(rows[3:7], width, colour, [first_filter, 2, 1, 4]),
        encode_png(rows[7:9], width, colour, [first_filter, 3]),
    ]
    joined = stitch_png(strips)
    assert png_size(joined) == (width, 9)
    assert decode_png(joined) == rows


def test_stitch_png_of_one_strip_is_that_image() -> None:
    rows = [bytes(range(12)), bytes(range(12, 24))]
    assert decode_png(stitch_png([encode_png(rows, 4, 2, [4, 4])])) == rows


def test_stitch_png_refuses_strips_that_do_not_fit_together() -> None:
    row = bytes(12)
    with pytest.raises(ValueError):
        stitch_png([encode_png([row], 4, 2, [0]), encode_png([row], 3, 6, [0])])
    with pytest.raises(ValueError):
        stitch_png([encode_png([row], 4, 2, [0]), encode_png([row[:9]], 3, 2, [0])])
    with pytest.raises(ValueError):
        stitch_png([])
    with pytest.raises(ValueError):
        stitch_png([b"GIF89a"])
